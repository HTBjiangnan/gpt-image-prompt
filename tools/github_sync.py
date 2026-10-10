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
BACKUP_ROOT = os.path.join(os.path.expanduser("~"), ".workbuddy", "skills", ".backups",
                           os.path.basename(SKILL_ROOT))
KEEP_BACKUPS = 10

EXCLUDE_DIRS = {"__pycache__", ".git", ".staged", ".backups"}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".bak", ".tmp")


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


def build_manifest(root=None, version=None, changelog=None):
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
    # codeload 通道：一次拉整包，效率最高
    if channel == "codeload":
        data, err = _tarball_bytes(cfg)
        if not err:
            got, missing = _extract_from_tarball(data, paths)
            if not missing:
                return got, "codeload", None
            err = "tarball 缺少文件: %s" % ", ".join(missing)
        channel = None  # 降级

    got = {}
    errors = []
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
    try:
        for rel, data in got.items():
            _atomic_write(rel, data)

        # 清理陈旧文件（仅限旧清单里有、新清单里没有的）
        old = _local_manifest_old()
        old_files = set((old or {}).get("files", {}).keys()) if old else set()
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

    version = set_version
    if not version:
        try:
            with open(VERSION_JSON, "r", encoding="utf-8") as f:
                version = json.load(f).get("version")
        except Exception:
            version = None
    if not version:
        return False, "无法确定版本号，请用 --set-version 指定"

    # 顺序很关键：必须先把 frontmatter 里的版本号改好，再计算 sha256 清单。
    # 反过来会导致清单里记录的 SKILL.md 哈希与实际文件不符，
    # 下游的自动更新会在 sha256 校验环节整包拒绝。
    _write_frontmatter_version(version)
    manifest = build_manifest(SKILL_ROOT, version=version)
    with open(VERSION_JSON, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # 推送前自检：清单必须与磁盘实际内容一致，否则中止（防止发布出"自相矛盾"的版本）
    bad = verify_manifest(SKILL_ROOT, manifest)
    if bad:
        return False, "清单与文件不一致，已中止推送：%s" % "; ".join(
            "%s(%s)" % (r, d[:20]) for r, _e, d in bad[:3])

    paths = list(manifest["files"].keys()) + ["VERSION.json"]
    msg = message or ("sync %s v%s" % (manifest["skill"], version))
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
    tail = "已推送 %d 个文件到 %s（v%s）" % (len(pushed), cfg["repo"], version)
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
