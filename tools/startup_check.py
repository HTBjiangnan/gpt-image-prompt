# -*- coding: utf-8 -*-
"""startup_check.py - 技能启动自检

每次加载技能时运行，共 3 项：
  1. 版本自检（本地 vs GitHub，可自动更新）
  2. 网络通道探测（raw / jsdelivr / codeload / api）
  3. 提交凭据状态（PAT；缺失则降级为只读）

结构对齐 数据分析/tools/startup_check.py：{all_ok, items:[{name,status,message,detail}]}
"""

import json
import os
import sys

try:
    from . import gh_client as gh
    from . import github_version_check as vc
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gh_client as gh
    import github_version_check as vc

ICON = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}


def _add(items, name, status, message, detail=""):
    items.append({"name": name, "status": status, "message": message, "detail": detail})


def run_startup_check(silent=False, auto_update=True, probe=True):
    items = []
    cfg = gh.load_config()

    # ---- 1. 版本自检（每天第一次联网；日内只做离线一致性自检）----
    try:
        ver = vc.daily_check(cfg=cfg, auto_update=auto_update)
        _add(items, "版本自检", ver.get("status", "warn"), ver.get("message", ""), ver.get("detail", ""))
    except Exception as e:
        _add(items, "版本自检", "warn", "版本自检异常，已跳过", "%s: %s" % (type(e).__name__, e))

    # ---- 2. 网络通道 ----
    if probe:
        try:
            res = gh.probe_all(cfg, force=False)
            ok_ch = [c for c, v in res.items() if v.get("ok")]
            if ok_ch:
                _add(items, "网络通道", "ok", "可用通道: %s" % ", ".join(ok_ch),
                     json.dumps(res, ensure_ascii=False))
            else:
                _add(items, "网络通道", "warn", "全部通道不可用（不影响本地提示词生成）",
                     json.dumps(res, ensure_ascii=False))
        except Exception as e:
            _add(items, "网络通道", "warn", "通道探测异常，已跳过", str(e))

    # ---- 3. 提交凭据 ----
    if gh.has_token(cfg):
        _add(items, "提交凭据", "ok", "PAT 已配置（可提交候选版式到追踪 Issue / 可发布）")
    else:
        _add(items, "提交凭据", "warn", "未配置 PAT → 只读模式（版本检测/自动更新正常，不能提交候选）",
             "如需提交，请在 %s 填入 token" % gh.CONFIG_PATH)

    all_ok = all(i["status"] == "ok" for i in items)

    if not silent:
        for it in items:
            print("%s %s: %s" % (ICON.get(it["status"], "?"), it["name"], it["message"]))
            if it["detail"] and it["status"] != "ok":
                print("     %s" % it["detail"].replace("\n", "\n     "))

    return {"all_ok": all_ok, "items": items}


if __name__ == "__main__":
    auto = "--no-update" not in sys.argv
    rep = run_startup_check(silent=False, auto_update=auto)
    sys.exit(0 if rep["all_ok"] else 1)
