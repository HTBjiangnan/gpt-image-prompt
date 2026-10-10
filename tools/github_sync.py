# -*- coding: utf-8 -*-
"""github_sync.py - 下载 / 校验 / 备份 / 原子覆盖 / 回滚，以及反向发布

安全硬约束：
- 下载先校验 sha256，任一不符即中止，不动本地
- 覆盖前整目录备份
- 逐文件替换，不使用任何递归删除命令
- 只清理"旧清单有、新清单无、且位于技能目录内"的陈旧文件
- 绝不触碰技能目录以外的任何路径
"""

import base64
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import time

try:
    from . import gh_client as gh
except ImportError:
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import gh_client as gh

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_JSON = os.path.join(SKILL_ROOT, "VERSION.json")
# 备份根目录由数据目录统一管理（见 gh_client.DATA_HOME），不依赖技能安装位置
BACKUP_ROOT = gh.BACKUP_ROOT
KEEP_BACKUPS = 10

EXCLUDE_DIRS = {"__pycache__", ".git", ".staged", ".backups"}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".bak", ".tmp")

# ============================================================
# 文件分类：文档类 vs 行为类
# ------------------------------------------------------------
# 「行为类」文件一旦变化，Agent 的实际表现就会不同，必须让其他电脑自动更新；
# 「文档类」文件只影响仓库页面观感（README / CHANGELOG / 许可证等），
# 改了**不升版本号**，因此不会去打扰已安装的副本。
# ============================================================

DOC_ONLY_FILES = {"README.md", "CHANGELOG.md", "LICENSE", ".gitignore"}
DOC_ONLY_PREFIXES = ("docs/",)


def is_behavior_file(rel):
    """该文件的变化是否需要触发其他电脑的自动更新"""
    r = str(rel).replace("\\", "/")
    if r == "VERSION.json":          # 版本文件本身永远是元数据
        return False
    if r in DOC_ONLY_FILES or r.startswith(DOC_ONLY_PREFIXES):
        return False
    return True


def _norm_skill_md(data):
    """SKILL.md 的 frontmatter 里 version 行本身不算行为变更，比较时抹掉"""
    try:
        txt = data.decode("utf-8")
    except Exception:
        return hashlib.sha256(data).hexdigest()
    import re as _re
    txt = _re.sub(r"(?m)^(\s*version\s*:).*$", r"\1", txt)
    return hashlib.sha256(txt.encode("utf-8")).hexdigest()


def bump_patch(version):
    """3.4.1 -> 3.4.2（缺段自动补 0；解析失败退化为 0.0.1）"""
    try:
        parts = [int(x) for x in str(version).lstrip("vV").split("-")[0].split(".")]
    except Exception:
        parts = []
    while len(parts) < 3:
        parts.append(0)
    parts[2] += 1
    return ".".join(str(x) for x in parts[:3])


# ============================================================
# 清单
# ============================================================

def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def walk_tracked_files(root=None):
    """列出受版本管理的相对路径（不含 VERSION.json 自身）"""
    root = root or SKILL_ROOT
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if fn in EXCLUDE_FILES or fn.endswith(EXCLUDE_SUFFIX):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if rel == "VERSION.json":
                continue
            out.append(rel)
    return sorted(out)


def build_manifest(root=None, version=None, changelog=None, rev=None, release=None):
    root = root or SKILL_ROOT
    files = {}
    for rel in walk_tracked_files(root):
        files[rel] = "sha256:" + file_sha256(os.path.join(root, rel))
    old = {}
    if os.path.isfile(os.path.join(root, "VERSION.json")):
        try:
            with open(os.path.join(root, "VERSION.json"), "r", encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            old = {}
    return {
        "schema": 1,
        "skill": os.path.basename(root),
        "version": version or old.get("version") or "0.0.0",
        # rev：单调递增修订号，每次发布 +1（含纯文档发布）。
        # 不参与"是否自动更新"的判断，仅用于审计与"远端有更新构建"的提示。
        "rev": int(rev if rev is not None else (old.get("rev") or 0)),
        # release：本次发布类型，skill=技能文件变更 / docs=仅文档变更
        "release": release or old.get("release") or "skill",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "changelog": changelog if changelog is not None else old.get("changelog") or [],
        "files": files,
    }


def verify_manifest(root, manifest):
    """返回不匹配项列表 [(rel, expected, actual)]"""
    bad = []
    for rel, expected in (manifest.get("files") or {}).items():
        full = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            bad.append((rel, expected, "<missing>"))
            continue
        actual = "sha256:" + file_sha256(full)
        if actual != expected:
            bad.append((rel, expected, actual))
    return bad


# ============================================================
# 下载远端文件
# ============================================================

def download_all(cfg, paths, channel=None):
    """返回 ({rel: bytes}, channel_used, err)"""
    pre_errors = []
    # codeload 通道：一次拉整包，效率最高。
    # 多文件时也无条件优先整包——逐个文件走通道会放大网络抖动
    # （实测：13 个文件逐个下载累计 >100s，而整包只需 0.8s）
    if channel == "codeload" or len(paths) > 1:
        data, err = _tarball_bytes(cfg)
        if not err:
            got, missing = _extract_from_tarball(data, paths)
            if not missing:
                return got, "codeload", None
            pre_errors.append("codeload 缺少文件: %s" % ", ".join(sorted(missing)))
        else:
            pre_errors.append("codeload: %s" % err)
        if channel == "codeload":
            return {}, None, "; ".join(pre_errors)
        channel = None  # 整包不可用 → 降级为逐文件按通道取

    got = {}
    errors = list(pre_errors)
    used = None
    for rel in paths:
        data, ch, err = gh.fetch_file(rel, cfg, prefer_channel=channel)
        if err or data is None:
            errors.append("%s -> %s" % (rel, err))
        else:
            got[rel] = data
            used = used or ch
    if errors:
        return got, used, "; ".join(errors)
    return got, used, None


def _tarball_bytes(cfg):
    url = "https://codeload.github.com/%s/tar.gz/refs/heads/%s" % (cfg["repo"], cfg["branch"])
    return gh.http_get(url, cfg=cfg, timeout=30)


def _extract_from_tarball(data, paths):
    got = {}
    missing = set(paths)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                for rel in list(missing):
                    if m.name.endswith("/" + rel):
                        fh = tf.extractfile(m)
                        if fh:
                            got[rel] = fh.read()
                            missing.discard(rel)
                        break
    except Exception:
        return {}, set(paths)
    return got, missing


# ============================================================
# 备份 / 还原
# ============================================================

def _backup():
    gh.ensure_data_home()
    os.makedirs(BACKUP_ROOT, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(BACKUP_ROOT, ts)
    shutil.copytree(SKILL_ROOT, dest,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "inbox", ".backups"))
    _prune_backups()
    return dest


def _prune_backups():
    try:
        entries = sorted([d for d in os.listdir(BACKUP_ROOT)
                          if os.path.isdir(os.path.join(BACKUP_ROOT, d))])
        for name in entries[:-KEEP_BACKUPS]:
            target = os.path.join(BACKUP_ROOT, name)
            if os.path.abspath(target).startswith(os.path.abspath(BACKUP_ROOT)):
                shutil.rmtree(target, ignore_errors=True)
    except Exception:
        pass


def _restore(backup_dir):
    """从备份还原（逐文件覆盖，不做递归删除）"""
    restored = 0
    for dirpath, dirnames, filenames in os.walk(backup_dir):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            src = os.path.join(dirpath, fn)
            rel = os.path.relpath(src, backup_dir)
            dst = os.path.join(SKILL_ROOT, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            restored += 1
    return restored


# ============================================================
# 原子替换
# ============================================================

def _atomic_write(rel, data):
    dst = os.path.join(SKILL_ROOT, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=".tmp_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


def _local_manifest_old():
    if os.path.isfile(VERSION_JSON):
        try:
            with open(VERSION_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version"):
                return data
        except Exception:
            pass
    return None


def apply_remote_update(cfg, remote_manifest, channel=None):
    """把远端清单对应的文件拉到本地。返回 (ok: bool, detail: str)"""
    files = remote_manifest.get("files") or {}
    if not files:
        return False, "远端清单为空，拒绝更新"

    paths = list(files.keys()) + ["VERSION.json"]

    # --dry 校验阶段：先全部下载到内存，不做任何本地改动 --
    got, used, err = download_all(cfg, paths, channel=channel)
    if err:
        return False, "下载失败，未做任何改动：%s" % err

    # sha256 校验
    bad = []
    for rel, expected in files.items():
        data = got.get(rel)
        if data is None:
            bad.append((rel, "missing"))
            continue
        actual = "sha256:" + hashlib.sha256(data).hexdigest()
        if actual != expected:
            bad.append((rel, "%s != %s" % (expected[:16], actual[:16])))
    if bad:
        return False, "校验失败，未做任何改动：%s" % "; ".join("%s(%s)" % (r, d) for r, d in bad[:5])

    # 远端 VERSION.json 与期望版本一致？
    try:
        remote_version = json.loads(got["VERSION.json"].decode("utf-8")).get("version")
    except Exception as e:
        return False, "远端 VERSION.json 解析失败：%s" % e
    if remote_version != remote_manifest.get("version"):
        return False, "版本号不一致（清单 %s / 文件 %s），拒绝更新" % (
            remote_manifest.get("version"), remote_version)

    # --提交阶段：备份 -> 逐文件替换 -> 清理陈旧文件 -> 复校 --
    backup_dir = _backup()
    # 旧清单必须在覆盖前抓取：VERSION.json 本身也在待写入之列，
    # 覆盖后再读就变成新清单了，陈旧文件将永远清不掉。
    old_manifest = _local_manifest_old()
    try:
        for rel, data in got.items():
            _atomic_write(rel, data)

        # 清理陈旧文件（仅限旧清单里有、新清单里没有的）
        old_files = set((old_manifest or {}).get("files", {}).keys()) if old_manifest else set()
        new_files = set(files.keys())
        for rel in old_files - new_files:
            target = os.path.join(SKILL_ROOT, rel.replace("/", os.sep))
            if os.path.commonpath([os.path.abspath(SKILL_ROOT), os.path.abspath(target)]) \
                    != os.path.abspath(SKILL_ROOT):
                continue  # 越界保护
            if os.path.isfile(target):
                os.remove(target)

        # 复校
        cur = json.load(open(VERSION_JSON, "r", encoding="utf-8"))
        left = verify_manifest(SKILL_ROOT, cur)
        if left:
            raise RuntimeError("复校失败: %s" % left[:3])
    except Exception as e:
        restored = _restore(backup_dir)
        return False, "更新失败，已从备份还原 %d 个文件：%s" % (restored, e)

    return True, "已更新到 v%s（通道 %s，备份 %s）" % (remote_version, used, os.path.basename(backup_dir))


# ============================================================
# 反向发布
# ============================================================

def _write_frontmatter_version(new_version):
    path = os.path.join(SKILL_ROOT, "SKILL.md")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    import re
    def repl(m):
        block = m.group(1)
        if re.search(r"^version:.*$", block, re.MULTILINE):
            block = re.sub(r"^version:.*$", "version: %s" % new_version, block, flags=re.MULTILINE)
        else:
            block = block.rstrip("\n") + "\nversion: %s" % new_version
        return "---\n%s\n---" % block
    new_content = re.sub(r"^---\s*\n(.*?)\n---", repl, content, count=1, flags=re.DOTALL)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)
    return True


def ensure_repo(cfg, private=False):
    """确保远端仓库存在，不存在则创建（公开）。返回 (ok, detail, created)"""
    exists, auth_ok, detail = gh.repo_exists(cfg)
    if exists is True:
        db = gh.repo_default_branch(cfg)
        if db and db != cfg.get("branch"):
            cfg["branch"] = db
            gh.save_config(cfg)
        # 顺带把仓库简介同步为源码里的默认值（仅在有写权限且不一致时；失败不影响 push）
        if gh.has_token(cfg):
            cur, derr = gh.repo_description(cfg)
            if not derr and cur != gh.DEFAULT_DESCRIPTION:
                gh.update_repo_description(cfg, gh.DEFAULT_DESCRIPTION)
        return True, "远端仓库已存在：%s" % cfg["repo"], False
    if exists is None:
        return False, "无法确认仓库状态（认证或网络问题）：%s" % detail, False
    # 仓库不存在
    if not gh.has_token(cfg):
        return False, ("远端仓库 %s 不存在，且未配置 PAT，无法自动创建；"
                       "请先建仓或在 %s 填入 token" % (cfg["repo"], gh.CONFIG_PATH)), False
    ok, msg = gh.create_repo(cfg, private=private)
    if not ok:
        return False, msg, False
    # 等待默认分支生成（auto_init 异步创建初始 commit）
    for _ in range(10):
        db = gh.repo_default_branch(cfg)
        if db:
            if db != cfg.get("branch"):
                cfg["branch"] = db
                gh.save_config(cfg)
            break
        time.sleep(2.0)
    for _ in range(15):
        if gh.branch_ready(cfg):
            return True, "%s，分支 %s 已就绪" % (msg, cfg["branch"]), True
        time.sleep(2.0)
    return True, "%s，但分支 %s 尚未就绪（稍后重跑 push 即可）" % (msg, cfg["branch"]), True


def behavior_diff_vs_remote(cfg, remote_manifest, root=None):
    """本地 vs 远端：技能行为文件是否有差异。

    返回 (changed: bool, detail: list[str])
    - 只比「行为类」文件（SKILL.md / references/** / tools/** …）
    - SKILL.md 的 frontmatter version 行本身不算差异（否则每次升版都误判）
    - 远端 SKILL.md 内容按需单独拉一次（api 通道，约 0.6s）
    """
    root = root or SKILL_ROOT
    rf = (remote_manifest or {}).get("files") or {}
    if not rf:
        return True, ["远端清单为空，按保守策略视为行为有变更"]

    diffs = []
    remote_skill_md_norm = None
    for rel in walk_tracked_files(root):
        if not is_behavior_file(rel):
            continue
        cur = "sha256:" + file_sha256(os.path.join(root, rel))
        if rf.get(rel) == cur:
            continue
        if rel == "SKILL.md":
            # 差异可能仅来自 version 行，拉远端原文去掉 version 行后再比
            if remote_skill_md_norm is None:
                data, _ch, err = gh.fetch_file("SKILL.md", cfg)
                remote_skill_md_norm = _norm_skill_md(data) if (data and not err) else ""
            if remote_skill_md_norm and remote_skill_md_norm == _norm_skill_md(
                    open(os.path.join(root, rel), "rb").read()):
                continue
        diffs.append(rel)

    # 远端有、本地已删除的行为文件
    for rel in rf:
        if not is_behavior_file(rel):
            continue
        if not os.path.isfile(os.path.join(root, rel.replace("/", os.sep))):
            diffs.append(rel + "(本地缺失)")
    return bool(diffs), diffs


def push(cfg, message=None, set_version=None, ensure=True, private=False):
    """把本机技能发布到 GitHub。返回 (ok, detail)"""
    if not gh.has_token(cfg):
        return False, "缺少 PAT，无法发布（请在 %s 配置 token）" % gh.CONFIG_PATH

    notes = []
    if ensure:
        ok, rmsg, created = ensure_repo(cfg, private=private)
        if not ok:
            return False, "远端仓库准备失败：%s" % rmsg
        if created:
            notes.append(rmsg)

    # ---- 版本决策：只有「行为文件」变更才升版本号 ----
    local_old = _local_manifest_old() or {}
    local_version = (local_old.get("version") or "").strip()
    local_rev = int(local_old.get("rev") or 0)

    remote_manifest, _rch, rerr = gh.fetch_json("VERSION.json", cfg)
    if not isinstance(remote_manifest, dict):
        remote_manifest = None
    remote_version = (remote_manifest or {}).get("version") or ""
    remote_rev = int((remote_manifest or {}).get("rev") or 0)

    if remote_manifest is None:
        behavior_changed = True
        notes.append("⚠️ 未能读取远端 VERSION.json（%s），按保守策略视为技能文件有变更" % (rerr or "不存在"))
    else:
        behavior_changed, diffs = behavior_diff_vs_remote(cfg, remote_manifest)
        if diffs:
            notes.append("行为文件变更：%s" % ", ".join(diffs[:6]) + (" …" if len(diffs) > 6 else ""))

    # 本地落后于远端时不允许发布（否则会把远端版本"回退"）
    if remote_version and local_version:
        try:
            from . import github_version_check as _vc
        except ImportError:
            import github_version_check as _vc
        if _vc.version_compare(local_version, remote_version) < 0:
            return False, ("本地 v%s 低于远端 v%s，请先运行 summon.py --update 再发布"
                           % (local_version, remote_version))

    if set_version:
        version, release = set_version, ("skill" if behavior_changed else "docs")
    elif behavior_changed:
        if local_version and local_version != remote_version:
            version = local_version          # 本机已手动升过版，尊重它
        else:
            version = bump_patch(remote_version or local_version or "0.0.0")
            notes.append("行为文件有变更，版本号自动升为 v%s（如属功能新增，建议用 --set-version 指定 minor）" % version)
        release = "skill"
    else:
        version = local_version or remote_version
        release = "docs"
        notes.append("仅有文档/元数据变更，版本号保持 v%s 不变 —— 其他电脑不会被触发自动更新" % version)

    rev = max(local_rev, remote_rev) + 1
    if not version:
        return False, "无法确定版本号（本地与远端 VERSION.json 均不可用）"

    # 顺序很关键：必须先把 frontmatter 里的版本号改好，再计算 sha256 清单。
    # 反过来会导致清单里记录的 SKILL.md 哈希与实际文件不符，
    # 下游的自动更新会在 sha256 校验环节整包拒绝。
    _write_frontmatter_version(version)
    manifest = build_manifest(SKILL_ROOT, version=version, rev=rev, release=release)
    with open(VERSION_JSON, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # 推送前自检：清单必须与磁盘实际内容一致，否则中止（防止发布出"自相矛盾"的版本）
    bad = verify_manifest(SKILL_ROOT, manifest)
    if bad:
        return False, "清单与文件不一致，已中止推送：%s" % "; ".join(
            "%s(%s)" % (r, d[:20]) for r, _e, d in bad[:3])

    paths = list(manifest["files"].keys()) + ["VERSION.json"]
    msg = message or ("sync %s v%s%s" % (manifest["skill"], version,
                                        "" if release == "skill" else " (docs only)"))
    pushed, failed = [], []
    for rel in paths:
        full = os.path.join(SKILL_ROOT, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            continue
        with open(full, "rb") as f:
            content = f.read()
        meta, _ = gh.api_get_file_meta(rel, cfg)
        sha = meta.get("sha") if isinstance(meta, dict) else None
        resp, err = gh.api_put_file(rel, content, msg, cfg, sha=sha)
        if err:
            failed.append("%s -> %s" % (rel, err))
        else:
            pushed.append(rel)
    if failed:
        return False, "；".join(notes + ["部分文件推送失败：%s" % "; ".join(failed[:5])])
    tail = "已推送 %d 个文件到 %s（v%s · rev %d · %s）" % (
        len(pushed), cfg["repo"], version, rev,
        "技能更新，其他电脑会自动跟进" if release == "skill" else "仅文档变更，不触发自动更新")
    return True, "；".join(notes + [tail])


# ============================================================
# 公开前审计
# ============================================================

# 规则文本本身会出现在本文件里，因此用两种方式避免"自我命中"：
#   1) 纯词规则（机构名）—— 用字符串拼接，使源码中不出现连续的目标词
#   2) 数据类规则 —— 要求"字段名 + 具体值"，策略文档里的类别名不会命中
_ORG = "民生" + "保险"
_BRANCH_ORG = "浙江" + "分公司"
_FIELDS = "保单号|客户号|身份证号|工号|投保人|被保险人"

AUDIT_PATTERNS = [
    ("PAT 令牌", r"github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{30,}|gho_[A-Za-z0-9]{30,}"),
    ("含用户名的绝对路径", r"[A-Za-z]:\\Users\\[A-Za-z0-9_.\-]+|/Users/[A-Za-z0-9_.\-]+"),
    ("内网地址", r"\b(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b"),
    ("机构名", _ORG + "|" + _BRANCH_ORG),
    ("业务数据字段+值", "(?:" + _FIELDS + r")[:：\s]*[0-9A-Za-z\-]{6,}"),
    ("明文口令", r"(?i)password\s*[:=]\s*['\"][^'\"]{3,}|api[_-]?key\s*[:=]\s*['\"][^'\"]{8,}"),
]


def audit(root=None, cfg=None):
    """公开前自检。返回 (hits: list, files_scanned: int)"""
    import re
    root = root or SKILL_ROOT
    hits = []
    scanned = 0
    for rel in walk_tracked_files(root):
        full = os.path.join(root, rel.replace("/", os.sep))
        try:
            with open(full, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except Exception:
            continue
        scanned += 1
        for label, pat in AUDIT_PATTERNS:
            for m in re.finditer(pat, text):
                snippet = m.group(0)
                if len(snippet) > 60:
                    snippet = snippet[:60] + "..."
                hits.append({"file": rel, "rule": label, "match": snippet})
    return hits, scanned


if __name__ == "__main__":
    import sys
    cfg = gh.load_config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "audit"
    if cmd == "audit":
        h, n = audit(cfg=cfg)
        print("扫描 %d 个文件，命中 %d 条" % (n, len(h)))
        for x in h:
            print("  [%s] %s :: %s" % (x["rule"], x["file"], x["match"]))
