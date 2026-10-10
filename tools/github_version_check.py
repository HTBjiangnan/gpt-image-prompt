# -*- coding: utf-8 -*-
"""github_version_check.py - 技能版本核对（GitHub 为权威源）

每次加载技能时运行：
1. 读本地 VERSION.json（权威）+ SKILL.md frontmatter，做一致性自检
2. 从 GitHub 拉取远端 VERSION.json（公开仓库免鉴权，多通道降级）
3. 比较版本号，四场景处理：
   - 远端更新  -> 交给 github_sync 自动覆盖（只升不降）
   - 版本一致  -> OK
   - 本地领先  -> 只告警，绝不降级
   - 通道失败  -> 告警，不阻断主流程

结构对齐 数据分析/tools/version_check.py：返回 {status, action, local_version, remote_version, message, detail}
"""

import json
import os
import re
import time

try:
    from . import gh_client as gh
except ImportError:  # 直接以脚本方式运行
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import gh_client as gh

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_MD_PATH = os.path.join(SKILL_ROOT, "SKILL.md")
VERSION_JSON_PATH = os.path.join(SKILL_ROOT, "VERSION.json")
VERSION_REMOTE_PATH = "VERSION.json"

# 每日一次联网核对的打卡文件（放在技能目录外，跨会话持久）
STAMP_PATH = gh.STAMP_PATH  # 由数据目录统一管理（gh_client.DATA_HOME）
STAMP_HOUR = 0  # 每天 0 点后视为新的一天

ICON = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}


# ============================================================
# 每日频次控制
# ============================================================

def today_key():
    import datetime
    return datetime.date.today().isoformat()


def _read_stamp():
    try:
        gh.ensure_data_home()
        with open(STAMP_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def should_check_today(retry_after=3600):
    """今天是否还需要联网核对。

    - 跨天 -> 需要
    - 今天已核对成功 -> 不需要
    - 今天只遇到网络失败 -> 距上次失败超过 retry_after 秒才重试，避免每次加载都卡在超时上
    """
    d = _read_stamp()
    if d.get("date") != today_key():
        return True
    fail_ts = d.get("fail_ts")
    if fail_ts and (time.time() - float(fail_ts)) > retry_after:
        return True
    return False


def mark_checked_today(version=None, network_failed=False):
    try:
        gh.ensure_data_home()
        os.makedirs(os.path.dirname(STAMP_PATH), exist_ok=True)
        rec = {"date": today_key(), "at": time.strftime("%Y-%m-%d %H:%M:%S"), "version": version}
        if network_failed:
            rec["fail_ts"] = time.time()
        else:
            prev = _read_stamp()
            if prev.get("date") == today_key() and prev.get("fail_ts"):
                rec["fail_ts"] = prev["fail_ts"]
        with open(STAMP_PATH, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ============================================================
# 版本解析（复用 数据分析 的元组比较法）
# ============================================================

def parse_version(v):
    """支持 '1.0' / '4.2' / 'v1.0.1' / '1.0.0-beta'"""
    v = str(v).lstrip("vV").split("-")[0]
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, TypeError):
        return (0,)


def version_compare(a, b):
    ta, tb = parse_version(a), parse_version(b)
    return (ta > tb) - (ta < tb)


# ============================================================
# 本地版本读取
# ============================================================

def read_local_version():
    """返回 (version, source, err)。优先 VERSION.json。"""
    if os.path.isfile(VERSION_JSON_PATH):
        try:
            with open(VERSION_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            v = (data.get("version") or "").strip()
            if v:
                return v, "VERSION.json", None
        except Exception as e:
            return None, None, "VERSION.json 解析失败: %s" % e
    v, err = read_frontmatter_version()
    if err:
        return None, None, err
    return v, "SKILL.md", None


def read_frontmatter_version():
    """从 SKILL.md frontmatter 解析 version / name（零依赖，不用 pyyaml）"""
    try:
        with open(SKILL_MD_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return None, "SKILL.md 未找到: %s" % SKILL_MD_PATH
    m = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not m:
        return None, "SKILL.md 中未找到 YAML frontmatter"
    version = None
    for line in m.group(1).split("\n"):
        line = line.strip()
        if line.startswith("version:"):
            version = line.split(":", 1)[1].strip().strip("'\"")
    if not version:
        return None, "SKILL.md frontmatter 中未找到 version 字段"
    return version, None


def read_frontmatter_repo():
    """读取 frontmatter 中声明的 github_repo / github_branch（用于配置缺失时的兜底）"""
    try:
        with open(SKILL_MD_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return None, None
    m = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not m:
        return None, None
    repo = branch = None
    for line in m.group(1).split("\n"):
        line = line.strip()
        if line.startswith("github_repo:"):
            repo = line.split(":", 1)[1].strip().strip("'\"")
        elif line.startswith("github_branch:"):
            branch = line.split(":", 1)[1].strip().strip("'\"")
    return repo, branch


def read_local_manifest():
    if not os.path.isfile(VERSION_JSON_PATH):
        return None, None
    try:
        with open(VERSION_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:
        return None, str(e)


# ============================================================
# 远端版本读取
# ============================================================

def fetch_remote_manifest(cfg, prefer_channel=None, only_channel=False):
    """返回 (manifest: dict|None, channel: str|None, err: str|None)"""
    data, channel, err = gh.fetch_json(VERSION_REMOTE_PATH, cfg,
                                       prefer_channel=prefer_channel,
                                       only_channel=only_channel)
    if err:
        return None, channel, err
    if not isinstance(data, dict) or not data.get("version"):
        return None, channel, "远端 VERSION.json 缺少 version 字段"
    return data, channel, None


# 存在 CDN 缓存、可能读到旧版本的通道（raw ≈ 5 分钟，jsdelivr ≈ 12 小时）
CACHED_CHANNELS = ("raw", "jsdelivr")
# 实时、无缓存，用于交叉复核
FRESH_CHANNELS = ("api", "codeload")


# ============================================================
# 主函数
# ============================================================

def check_version(cfg=None, auto_update=False, force_channel=None, verbose=False):
    """版本自检。返回结构对齐 数据分析/tools/version_check.py"""
    result = {
        "status": "ok",
        "action": "unknown",
        "local_version": "未知",
        "remote_version": None,
        "channel": None,
        "message": "",
        "detail": "",
    }

    # ---- 1. 本地版本 ----
    local_version, source, err = read_local_version()
    if err:
        result.update(status="fail", action="parse_error",
                      message="无法解析本地版本", detail=err)
        return result
    result["local_version"] = local_version

    # 本地一致性自检：VERSION.json 与 frontmatter 必须一致
    fm_version, _ = read_frontmatter_version()
    if fm_version and fm_version != local_version:
        result["detail"] = ("本地不一致：VERSION.json=v%s，SKILL.md frontmatter=v%s "
                            "（以 VERSION.json 为准）" % (local_version, fm_version))

    # ---- 2. 配置（允许从 frontmatter 兜底 repo）----
    cfg = cfg or gh.load_config()
    if not cfg.get("repo"):
        fm_repo, fm_branch = read_frontmatter_repo()
        if fm_repo:
            cfg["repo"] = fm_repo
            cfg["branch"] = fm_branch or gh.DEFAULT_BRANCH
    if not cfg.get("repo"):
        result.update(status="warn", action="config_missing",
                      message="未配置 GitHub 仓库地址",
                      detail="请设置 %s 中的 repo 字段" % gh.CONFIG_PATH)
        return result

    # ---- 3. 拉远端版本 ----
    remote, channel, err = fetch_remote_manifest(cfg, prefer_channel=force_channel)
    if err:
        result.update(status="warn", action="network_unavailable",
                      message="远端不可用，跳过版本核对（不阻断）",
                      detail="全部通道失败：%s" % err)
        return result
    remote_version = remote["version"]
    result["remote_version"] = remote_version
    result["channel"] = channel

    # ---- 4. 比较 ----
    cmp = version_compare(local_version, remote_version)

    # 缓存通道（raw ≈ 5 分钟 / jsdelivr ≈ 12 小时）在刚发布后可能读到旧版本，
    # 会把"其实已一致"误判成"本地领先"。因此仅在判定本地领先时，
    # 用实时通道（api / codeload，仅单通道不降级）复核一次。
    if cmp > 0 and channel in CACHED_CHANNELS:
        for ch in FRESH_CHANNELS:
            m2, ch2, err2 = fetch_remote_manifest(cfg, prefer_channel=ch, only_channel=True)
            if err2 or not m2:
                continue
            cmp2 = version_compare(local_version, m2["version"])
            if cmp2 <= 0:
                remote, channel = m2, ch2
                cmp = cmp2
                result["remote_version"] = m2["version"]
                result["channel"] = ch2
            break

    if cmp == 0:
        result.update(status="ok", action="match",
                      message="版本一致: v%s" % local_version,
                      detail="本地与远端（%s）一致，无需操作%s" % (
                          channel, ("；" + result["detail"]) if result["detail"] else ""))
        return result

    if cmp > 0:
        result.update(status="warn", action="local_ahead",
                      message="本地 v%s > 远端 v%s（只升不降，未改动）" % (local_version, remote_version),
                      detail="若本机改动确为最新，请运行 summon.py --push 发布到 GitHub")
        return result

    # cmp < 0 —— 远端更新
    result.update(status="warn", action="remote_newer",
                  message="发现新版本: 本地 v%s → 远端 v%s" % (local_version, remote_version),
                  detail="通道=%s；%s" % (channel, remote.get("changelog") or ""))

    if not auto_update:
        result["action"] = "update_available"
        result["detail"] += "（未自动更新，运行 summon.py --update 应用）"
        return result

    return _apply_update(cfg, remote, channel, result)


def _apply_update(cfg, remote, channel, result):
    try:
        from . import github_sync as sync
    except ImportError:
        import github_sync as sync
    try:
        ok, detail = sync.apply_remote_update(cfg, remote, channel)
    except Exception as e:
        result.update(status="fail", action="update_failed",
                      message="自动更新失败", detail="%s: %s" % (type(e).__name__, e))
        return result
    if ok:
        result.update(status="ok", action="updated", message="已自动更新到 v%s" % remote["version"],
                      detail=detail)
    else:
        result.update(status="warn", action="update_failed", message="自动更新未完成", detail=detail)
    return result


def local_consistency():
    """离线本地一致性自检（0 网络开销）"""
    local_version, source, err = read_local_version()
    if err:
        return {"ok": False, "local_version": None, "detail": err}
    fm_version, _ = read_frontmatter_version()
    if fm_version and fm_version != local_version:
        return {"ok": False, "local_version": local_version,
                "detail": "本地不一致：VERSION.json=v%s / SKILL.md frontmatter=v%s（以 VERSION.json 为准）"
                          % (local_version, fm_version)}
    return {"ok": True, "local_version": local_version,
            "detail": "本地一致 v%s（来源 %s）" % (local_version, source)}


def daily_check(cfg=None, force=False, auto_update=True):
    """推荐的入口：**每天第一次**联网核对，日内其余调用只做离线一致性自检。

    返回结构与 check_version 一致，另加 skipped / consistency 字段。
    """
    cons = local_consistency()

    if not force and not should_check_today():
        stamp = _read_stamp()
        failed = bool(stamp.get("fail_ts"))
        return {
            "status": "ok" if cons["ok"] else "warn",
            "action": "daily_failed_cached" if failed else "daily_skipped",
            "skipped": True,
            "local_version": cons.get("local_version") or "未知",
            "remote_version": None,
            "channel": None,
            "consistency": cons,
            "message": ("今日已完成版本核对（%s），跳过联网" % stamp.get("date", today_key()))
                       if not failed else "今日联网核对失败，暂不重试（1 小时后自动重试）",
            "detail": cons["detail"],
        }

    r = check_version(cfg=cfg, auto_update=auto_update)
    r["skipped"] = False
    r["consistency"] = cons
    action = r.get("action")
    if action in ("network_unavailable", "config_missing", "parse_error", "update_failed"):
        mark_checked_today(r.get("local_version"), network_failed=True)
    else:
        mark_checked_today(r.get("local_version"))
    return r


def format_line(result):
    """单行输出，供启动自检使用"""
    return "%s %s" % (ICON.get(result.get("status", "warn"), "?"), result.get("message", ""))


if __name__ == "__main__":
    import sys
    auto = "--auto-update" in sys.argv
    r = check_version(auto_update=auto, verbose=True)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
