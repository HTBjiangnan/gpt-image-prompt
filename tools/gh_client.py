# -*- coding: utf-8 -*-
"""gh_client.py - GitHub 访问网络层

职责：
1. 读取/写入凭据配置（默认位于 ~/.workbuddy/skill_github_config.json，不在技能目录内）
2. 显式绕过失效的环境代理（http_proxy/https_proxy），必要时自动回退到代理
3. 多通道探测与降级：raw -> jsdelivr -> codeload -> api(免鉴权) -> api(鉴权)
4. 封装 GitHub REST API（读文件、写文件、建 Issue、发评论）

仅使用 Python 标准库，无第三方依赖。
"""

import base64
import io
import json
import os
import re
import socket
import ssl
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

# ============================================================
# 常量
# ============================================================

SKILL_NAME = "gpt-image-prompt"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".workbuddy", "skill_github_config.json")

DEFAULT_REPO = "HTBjiangnan/gpt-image-prompt"
DEFAULT_BRANCH = "main"

TIMEOUT = 15
RETRIES = 2
PROBE_CACHE_TTL = 1800  # 通道探测结果缓存 30 分钟

USER_AGENT = "gpt-image-prompt-skill/3.1 (+python-urllib)"

API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": USER_AGENT,
}


# ============================================================
# 配置
# ============================================================

def load_config():
    """读取配置。不存在时返回默认骨架（不写盘）。"""
    cfg = {
        "repo": DEFAULT_REPO,
        "branch": DEFAULT_BRANCH,
        "token": "",
        "issue_number": None,
        "inbox_dir": "inbox",
        "tls_verify": True,
        "channel_cache": None,
    }
    if not os.path.isfile(CONFIG_PATH):
        return cfg
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return cfg
    section = raw.get(SKILL_NAME) or raw
    if isinstance(section, dict):
        cfg.update({k: v for k, v in section.items() if v is not None or k in ("token", "issue_number")})
    # 环境变量优先
    if os.environ.get("GITHUB_TOKEN"):
        cfg["token"] = os.environ["GITHUB_TOKEN"].strip()
    return cfg


def save_config(cfg):
    """把配置写回配置文件（保留其它技能段落）。"""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    raw = {}
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    payload = dict(cfg)
    payload["token"] = cfg.get("token", "") or ""  # 始终保留字段，方便用户填写
    raw[SKILL_NAME] = payload
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass
    return True


def has_token(cfg):
    return bool(str(cfg.get("token") or "").strip())


def config_template():
    return {
        SKILL_NAME: {
            "repo": DEFAULT_REPO,
            "branch": DEFAULT_BRANCH,
            "token": "",
            "issue_number": None,
            "inbox_dir": "inbox",
        }
    }


# ============================================================
# HTTP 底层
# ============================================================

def _ssl_context(tls_verify=True):
    if tls_verify:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _build_opener(use_proxy, cfg):
    """use_proxy=False -> 显式绕过环境代理（本机代理已失效，绕开即通）"""
    if use_proxy:
        import urllib.request as _u
        proxy = os.environ.get("https_proxy") or os.environ.get("http_proxy") or ""
        handlers = [_u.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    else:
        handlers = [urllib.request.ProxyHandler({})]
    handlers.append(urllib.request.HTTPSHandler(context=_ssl_context(cfg.get("tls_verify", True))))
    return urllib.request.build_opener(*handlers)


def _safe_url(url):
    """对含非 ASCII 字符（如中文文件名/目录）的 URL 做百分号编码。

    urllib.request.Request 不接受非 ASCII 的 URL，否则抛 UnicodeEncodeError。
    '%' 在 safe 集合内，因此已编码过的 URL 不会被二次编码。
    """
    if all(ord(c) < 128 for c in url):
        return url
    return urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%")


def http_get(url, cfg=None, headers=None, timeout=TIMEOUT, use_proxy=False, max_bytes=None):
    """返回 (data: bytes | None, err: str | None)"""
    cfg = cfg or {}
    url = _safe_url(url)
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    last_err = None
    for attempt in range(RETRIES + 1):
        try:
            opener = _build_opener(use_proxy, cfg)
            req = urllib.request.Request(url, headers=hdrs, method="GET")
            with opener.open(req, timeout=timeout) as resp:
                data = resp.read() if max_bytes is None else resp.read(max_bytes)
                return data, None
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read()[:300]
            except Exception:
                pass
            return None, "HTTP %s %s" % (e.code, body.decode("utf-8", "replace")[:200])
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
            if attempt < RETRIES:
                time.sleep(1.0 * (attempt + 1))
    return None, last_err or "unknown error"


def http_request(method, url, cfg=None, payload=None, headers=None, timeout=TIMEOUT):
    """带 JSON body 的请求，用于写操作。返回 (data: dict|None, err: str|None)"""
    cfg = cfg or {}
    url = _safe_url(url)
    hdrs = dict(API_HEADERS)
    if headers:
        hdrs.update(headers)
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    last_err = None
    for attempt in range(RETRIES + 1):
        try:
            opener = _build_opener(False, cfg)
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                return (json.loads(raw.decode("utf-8")) if raw else {}), None
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            return None, "HTTP %s %s" % (e.code, detail)
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
            if attempt < RETRIES:
                time.sleep(1.0 * (attempt + 1))
    return None, last_err or "unknown error"


def _auth_headers(cfg):
    h = dict(API_HEADERS)
    if has_token(cfg):
        h["Authorization"] = "Bearer %s" % cfg["token"]
    return h


def _q(path):
    """对 URL 路径做百分号编码（保留 /）"""
    return urllib.parse.quote(path, safe="/")


# ============================================================
# 通道定义
# ============================================================

def channel_urls(channel, repo, branch, path):
    if channel == "raw":
        return "https://raw.githubusercontent.com/%s/%s/%s" % (repo, branch, _q(path))
    if channel == "jsdelivr":
        return "https://cdn.jsdelivr.net/gh/%s@%s/%s" % (repo, branch, _q(path))
    if channel == "api":
        return "https://api.github.com/repos/%s/contents/%s?ref=%s" % (repo, _q(path), branch)
    if channel == "codeload":
        return "https://codeload.github.com/%s/tar.gz/refs/heads/%s" % (repo, branch)
    raise ValueError("unknown channel: %s" % channel)


# 免鉴权通道优先级（raw 最快，jsdelivr 最稳，codeload 整包，api 限流）
CHANNEL_ORDER = ["raw", "jsdelivr", "codeload", "api"]


def probe_channel(channel, cfg):
    """探测单个通道是否可用。

    语义：**只要能收到 HTTP 响应就算通道可用**（哪怕 404 —— 那说明通道通、
    只是仓库或文件还不存在）。返回 (ok: bool, detail: str)
    """
    repo = cfg["repo"]
    branch = cfg["branch"]
    url = channel_urls(channel, repo, branch, "VERSION.json")
    data, err = http_get(url, cfg=cfg, timeout=10)
    if err:
        m = re.match(r"HTTP (\d{3})", err)
        if m:
            return True, "通道可达（HTTP %s：仓库或文件尚不存在）" % m.group(1)
        return False, err
    if not data:
        return False, "empty response"
    if channel == "codeload":
        return data[:2] == b"\x1f\x8b", "tarball %d bytes" % len(data)
    if channel == "api":
        try:
            meta = json.loads(data.decode("utf-8"))
        except Exception:
            return False, "api response not json"
        if isinstance(meta, dict) and meta.get("content"):
            return True, "contents api ok"
        return False, "api: %s" % (meta.get("message") if isinstance(meta, dict) else "bad payload")
    try:
        json.loads(data.decode("utf-8"))
    except Exception:
        return False, "payload not json"
    return True, "%d bytes" % len(data)


def probe_all(cfg, force=False):
    """探测全部通道，结果缓存。返回 {channel: {"ok":bool,"detail":str}}"""
    cache = cfg.get("channel_cache")
    if (not force) and isinstance(cache, dict) and cache.get("at"):
        if time.time() - cache["at"] < PROBE_CACHE_TTL and cache.get("repo") == cfg["repo"]:
            return cache.get("result") or {}
    result = {}
    for ch in CHANNEL_ORDER:
        ok, detail = probe_channel(ch, cfg)
        result[ch] = {"ok": ok, "detail": detail}
        if ok:
            break  # 已经找到可用通道，后面的不必再探，节省时间
    cfg["channel_cache"] = {"at": time.time(), "repo": cfg["repo"], "result": result}
    try:
        save_config(cfg)
    except Exception:
        pass
    return result


# ============================================================
# 取文件（多通道）
# ============================================================

def fetch_file(path, cfg, prefer_channel=None):
    """按优先级取一个文本文件。返回 (data: bytes|None, channel: str|None, err: str|None)"""
    repo, branch = cfg["repo"], cfg["branch"]
    order = list(CHANNEL_ORDER)
    if prefer_channel and prefer_channel in order:
        order.remove(prefer_channel)
        order.insert(0, prefer_channel)

    errors = []
    for ch in order:
        if ch == "codeload":
            data, err = _fetch_from_tarball(repo, branch, path, cfg)
        elif ch == "api":
            url = channel_urls("api", repo, branch, path)
            data, err = http_get(url, cfg=cfg, headers=_auth_headers(cfg))
            if data and not err:
                try:
                    meta = json.loads(data.decode("utf-8"))
                    if isinstance(meta, dict) and meta.get("content"):
                        data = base64.b64decode(meta["content"])
                    else:
                        err = "api: no content"
                        data = None
                except Exception as e:
                    data, err = None, "api decode: %s" % e
        else:
            url = channel_urls(ch, repo, branch, path)
            data, err = http_get(url, cfg=cfg)
        if data and not err:
            return data, ch, None
        errors.append("%s -> %s" % (ch, err))
    return None, None, "; ".join(errors)


def _fetch_from_tarball(repo, branch, path, cfg):
    url = channel_urls("codeload", repo, branch, path)
    data, err = http_get(url, cfg=cfg, timeout=30)
    if err or not data:
        return None, err or "empty tarball"
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            # tarball 内路径形如 <repo>-<branch>/<path>
            target_suffix = "/" + path
            member = None
            for m in tf.getmembers():
                if m.isfile() and m.name.endswith(target_suffix):
                    member = m
                    break
            if member is None:
                return None, "member not found in tarball: %s" % path
            fh = tf.extractfile(member)
            return (fh.read() if fh else None), None
    except Exception as e:
        return None, "tarball parse: %s" % e


def fetch_json(path, cfg, prefer_channel=None):
    data, channel, err = fetch_file(path, cfg, prefer_channel)
    if err:
        return None, None, err
    try:
        return json.loads(data.decode("utf-8")), channel, None
    except Exception as e:
        return None, channel, "json parse error: %s" % e


# ============================================================
# 写操作（需 PAT）
# ============================================================

def api_put_file(path, content_bytes, message, cfg, sha=None):
    """PUT /repos/{owner}/{repo}/contents/{path}  —— 需 Contents: write"""
    if not has_token(cfg):
        return None, "缺少 PAT（token 未配置），无法写入仓库"
    url = "https://api.github.com/repos/%s/contents/%s" % (cfg["repo"], _q(path))
    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("ascii"),
        "branch": cfg["branch"],
    }
    if sha:
        payload["sha"] = sha
    return http_request("PUT", url, cfg=cfg, payload=payload, headers=_auth_headers(cfg))


def api_get_file_meta(path, cfg):
    """取文件元信息（含 sha），不存在返回 (None, None)"""
    url = "https://api.github.com/repos/%s/contents/%s?ref=%s" % (cfg["repo"], _q(path), cfg["branch"])
    data, err = http_request("GET", url, cfg=cfg, headers=_auth_headers(cfg))
    if err or not data:
        return None, err
    return data, None


def api_get_file_bytes(path, cfg):
    meta, err = api_get_file_meta(path, cfg)
    if err or not meta:
        return None, err
    if isinstance(meta, list):
        return None, "path is a directory: %s" % path
    content = meta.get("content")
    if not content:
        return None, "no content field"
    try:
        return base64.b64decode(content), None
    except Exception as e:
        return None, "base64 decode: %s" % e


def api_create_issue(title, body, cfg, labels=None):
    """POST /repos/{owner}/{repo}/issues —— 需 Issues: write"""
    if not has_token(cfg):
        return None, "缺少 PAT（token 未配置），无法创建 Issue"
    url = "https://api.github.com/repos/%s/issues" % cfg["repo"]
    payload = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    return http_request("POST", url, cfg=cfg, payload=payload, headers=_auth_headers(cfg))


def api_create_comment(issue_number, body, cfg):
    """POST /repos/{owner}/{repo}/issues/{n}/comments —— 需 Issues: write"""
    if not has_token(cfg):
        return None, "缺少 PAT（token 未配置），无法发评论"
    url = "https://api.github.com/repos/%s/issues/%s/comments" % (cfg["repo"], issue_number)
    return http_request("POST", url, cfg=cfg, payload={"body": body}, headers=_auth_headers(cfg))


def api_get_issue(issue_number, cfg):
    url = "https://api.github.com/repos/%s/issues/%s" % (cfg["repo"], issue_number)
    return http_request("GET", url, cfg=cfg, headers=_auth_headers(cfg))


def api_list_comments(issue_number, cfg, per_page=100):
    """列出 Issue 评论。返回 (list, err)"""
    url = ("https://api.github.com/repos/%s/issues/%s/comments?per_page=%d"
           % (cfg["repo"], issue_number, per_page))
    data, err = http_request("GET", url, cfg=cfg, headers=_auth_headers(cfg))
    if err or not isinstance(data, list):
        return [], err
    return data, None


def api_rate_limit(cfg):
    data, err = http_request("GET", "https://api.github.com/rate_limit", cfg=cfg, headers=_auth_headers(cfg))
    if err or not data:
        return None, err
    core = (data.get("resources") or {}).get("core") or {}
    return core, None


def repo_exists(cfg):
    """返回 (exists: bool|None, auth_ok: bool, detail: str)"""
    url = "https://api.github.com/repos/%s" % cfg["repo"]
    data, err = http_request("GET", url, cfg=cfg, headers=_auth_headers(cfg))
    if err:
        if "404" in err:
            return False, has_token(cfg), "仓库不存在或为私有且 token 无权访问"
        if "401" in err or "403" in err:
            return None, False, "认证失败: %s" % err
        return None, has_token(cfg), err
    return True, True, "ok"


def create_repo(cfg, private=False, description=None, auto_init=True):
    """POST /user/repos —— 创建远端仓库。

    权限要求：classic PAT 需勾选 `repo`；
    fine-grained PAT **无法**创建仓库（它只能授权到已存在的仓库），
    这种情况请先在网页建好仓库，再让本工具直接 push。

    返回 (ok: bool, detail: str)
    """
    if not has_token(cfg):
        return False, "缺少 PAT，无法创建仓库"
    name = cfg["repo"].split("/")[-1]
    payload = {
        "name": name,
        "private": bool(private),
        "auto_init": bool(auto_init),   # 生成初始 commit 与默认分支，避免空仓库写入失败
        "has_issues": True,
        "has_wiki": False,
        "has_projects": False,
        "description": description or "GPT Image 提示词技能包（WorkBuddy Skill）：58 场景模板 + 版式决策总表 + 严格收录评估",
    }
    data, err = http_request("POST", "https://api.github.com/user/repos",
                             cfg=cfg, payload=payload, headers=_auth_headers(cfg))
    if err:
        if "422" in err:
            return False, ("创建失败：同名仓库已存在，或该 token 无创建权限"
                           "（fine-grained token 不能建仓，请改用勾选 repo 的 classic token）——%s" % err)
        if "403" in err:
            return False, "创建失败：token 权限不足（classic token 需勾选 repo）——%s" % err
        return False, err
    return True, "已创建仓库 %s（%s）" % (data.get("full_name"), "私有" if private else "公开")


def repo_default_branch(cfg):
    """读取远端默认分支名"""
    data, err = http_request("GET", "https://api.github.com/repos/%s" % cfg["repo"],
                             cfg=cfg, headers=_auth_headers(cfg))
    if err or not isinstance(data, dict):
        return None
    return data.get("default_branch")


def branch_ready(cfg):
    """目标分支是否已存在（有初始 commit）"""
    data, err = http_request("GET", "https://api.github.com/repos/%s/branches/%s" % (cfg["repo"], cfg["branch"]),
                             cfg=cfg, headers=_auth_headers(cfg))
    return bool(not err and data)


def tcp_reachable(host, port, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


if __name__ == "__main__":
    cfg = load_config()
    print(json.dumps({k: (v if k != "token" else ("<set>" if v else "<empty>"))
                      for k, v in cfg.items()}, ensure_ascii=False, indent=2))
    print("token:", "已配置" if has_token(cfg) else "未配置（只读模式）")
