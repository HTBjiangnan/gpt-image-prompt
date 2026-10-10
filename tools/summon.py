# -*- coding: utf-8 -*-
"""summon.py - 统一 CLI 入口

用法：
  python tools/summon.py --check             只比对版本，不修改
  python tools/summon.py --update            比对并自动更新（只升不降，先备份）
  python tools/summon.py --status            打印版本/通道/配置状态
  python tools/summon.py --probe             探测网络通道并缓存结果
  python tools/summon.py --audit             公开前自检（扫 token/绝对路径/内网/业务敏感词）
  python tools/summon.py --push -m "msg"     本机技能发布到 GitHub（需 PAT）
  python tools/summon.py --intake <图片> <评估json>   图片收录评估（第一阶段）
  python tools/summon.py --commit <stage_id> --confirm  提交候选（第二阶段）

返回码：
  0  一致 / 成功
  10 本地落后（已更新 或 提示可更新）
  11 本地领先（只告警，未改动）
  20 网络不可用
  30 配置缺失
  40 审计命中 / 敏感内容否决
  1  不收录 / 其他业务性拒绝
  2  待确认
  3  重复
  99 异常
"""

import argparse
import json
import os
import sys

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import gh_client as gh          # noqa: E402
import github_sync as sync      # noqa: E402
import github_version_check as vc  # noqa: E402

RET_OK = 0
RET_BEHIND = 10
RET_AHEAD = 11
RET_NET = 20
RET_CFG = 30
RET_AUDIT = 40
RET_PENDING = 2
RET_ERR = 99


# ============================================================
# 各子命令
# ============================================================

def cmd_check(args):
    """每天第一次联网核对；日内重复调用只做离线一致性自检。"""
    cfg = gh.load_config()
    r = vc.daily_check(cfg=cfg, force=args.force_check, auto_update=args.auto_update)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    else:
        print(vc.format_line(r))
        if args.verbose and r.get("detail"):
            print("     " + r["detail"])
    if r.get("action") in ("daily_skipped", "daily_failed_cached"):
        return RET_OK if (r.get("consistency") or {}).get("ok", True) else RET_ERR
    return _rc_from_action(r.get("action"))


def _rc_from_action(action):
    return {
        "match": RET_OK,
        "updated": RET_BEHIND,
        "update_available": RET_BEHIND,
        "local_ahead": RET_AHEAD,
        "network_unavailable": RET_NET,
        "config_missing": RET_CFG,
        "parse_error": RET_ERR,
        "update_failed": RET_ERR,
    }.get(action, RET_OK)


def cmd_status(args):
    cfg = gh.load_config()
    local_v, source, err = vc.read_local_version()
    print("技能        : %s" % os.path.basename(sync.SKILL_ROOT))
    print("本地版本    : %s   (来源: %s)" % (local_v or "未知", source or "-"))
    fm, _ = vc.read_frontmatter_version()
    print("frontmatter : %s%s" % (fm or "-", "  ⚠️ 与 VERSION.json 不一致" if fm and fm != local_v else ""))
    print("仓库        : %s@%s" % (cfg["repo"], cfg["branch"]))
    print("凭据        : %s" % ("PAT 已配置" if gh.has_token(cfg) else "未配置（只读模式）"))
    print("配置文件    : %s%s" % (gh.CONFIG_PATH, "" if os.path.isfile(gh.CONFIG_PATH) else "  (尚不存在)"))

    remote, channel, rerr = vc.fetch_remote_manifest(cfg)
    if rerr:
        print("远端版本    : 不可用 —— %s" % rerr)
    else:
        print("远端版本    : %s   (通道: %s)" % (remote.get("version"), channel))
        cmp = vc.version_compare(local_v, remote.get("version"))
        print("比较结果    : %s" % {1: "本地领先", 0: "一致", -1: "远端更新"}[cmp])

    if args.probe_channels:
        print("\n通道探测:")
        for ch, info in gh.probe_all(cfg, force=True).items():
            print("  %-9s %s  %s" % (ch, "✅" if info.get("ok") else "❌", info.get("detail", "")))
    return RET_OK


def cmd_probe(args):
    cfg = gh.load_config()
    res = gh.probe_all(cfg, force=True)
    for ch, info in res.items():
        print("%-9s %s  %s" % (ch, "✅" if info.get("ok") else "❌", info.get("detail", "")))
    ok = [c for c, v in res.items() if v.get("ok")]
    print("\n结论: %s" % ("可用通道 %s" % ", ".join(ok) if ok else "全部不可用"))
    return RET_OK if ok else RET_NET


def cmd_audit(args):
    hits, n = sync.audit()
    print("扫描 %d 个受版本管理的文件，命中 %d 条" % (n, len(hits)))
    for h in hits:
        print("  [%s] %s :: %s" % (h["rule"], h["file"], h["match"]))
    if hits:
        print("\n⛔ 存在敏感命中，禁止 push 到公开仓库。")
        return RET_AUDIT
    print("\n✅ 未发现敏感信息，可安全公开。")
    return RET_OK


def cmd_push(args):
    cfg = gh.load_config()
    if not gh.has_token(cfg):
        print("⚠️  尚未配置 PAT（token 为空），当前为只读模式，无法发布。")
        print("    请在 GitHub 生成 token 后写入：")
        print("      %s" % gh.CONFIG_PATH)
        print("    · 想让本工具自动建仓 → classic token，勾选 repo scope")
        print("    · 仓库已手建好 → fine-grained token，权限 Contents:RW + Issues:RW")
        print("    填好后重跑：python tools/summon.py --push")
        return RET_CFG
    hits, n = sync.audit()
    if hits and not args.force:
        print("⛔ 公开前审计命中 %d 条，已阻止 push（如确认无碍可加 --force）：" % len(hits))
        for h in hits[:10]:
            print("  [%s] %s :: %s" % (h["rule"], h["file"], h["match"]))
        return RET_AUDIT
    ok, msg = sync.push(cfg, message=args.message, set_version=args.set_version)
    print(("✅ " if ok else "❌ ") + msg)
    return RET_OK if ok else RET_CFG


def cmd_intake(args):
    import image_intake
    if args.intake_cmd == "commit":
        r = image_intake.commit(args.stage_id, confirm=args.confirm, force=args.force)
        print(r["message"])
        if r.get("detail"):
            print(r["detail"])
        return r["return_code"]
    if args.intake_cmd == "abort":
        ok, msg = image_intake.abort(args.stage_id)
        print(msg)
        return RET_OK if ok else RET_ERR
    if args.intake_cmd == "list":
        for x in image_intake.list_staged():
            print("%s  %s  %d分  %s%s" % (x["stage_id"], x["title"], x["total"], x["band"],
                                          "  [已提交]" if x["submitted"] else ""))
        return RET_OK
    # stage
    r = image_intake.stage(args.image, args.assessment)
    print(r.get("preview") or r["message"])
    if r.get("return_code") not in (0, 2, 3):
        print("（%s）" % r.get("detail", ""))
    return r["return_code"]


# ============================================================
# 参数解析
# ============================================================

def build_parser():
    p = argparse.ArgumentParser(
        prog="summon.py",
        description="gpt-image-prompt 技能：GitHub 版本自更新 + 图片收录评估",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)

    g = p.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="只比对版本，不修改")
    g.add_argument("--update", action="store_true", help="比对并自动更新（只升不降）")
    g.add_argument("--status", action="store_true", help="打印版本/通道/配置状态")
    g.add_argument("--probe", action="store_true", help="探测网络通道")
    g.add_argument("--audit", action="store_true", help="公开前自检")
    g.add_argument("--push", action="store_true", help="发布本机技能到 GitHub（需 PAT）")
    g.add_argument("--intake", dest="intake_cmd", metavar="CMD",
                   choices=["stage", "commit", "abort", "list"],
                   help="图片收录评估：stage/commit/abort/list")

    p.add_argument("-m", "--message", default=None, help="push 的提交信息")
    p.add_argument("--set-version", default=None, help="push 时指定版本号")
    p.add_argument("--auto-update", dest="auto_update", action="store_true", default=True,
                   help="远端版本更新时自动覆盖（默认开启，只升不降、先备份）")
    p.add_argument("--no-auto-update", dest="auto_update", action="store_false",
                   help="只提示有新版本，不自动更新")
    p.add_argument("--force", action="store_true", help="强制（跳过审计/边缘候选限制）")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("-v", "--verbose", action="store_true", help="输出详情")
    p.add_argument("--probe-channels", dest="probe_channels", action="store_true",
                   help="status 时同时探测通道")
    p.add_argument("--quiet", action="store_true", help="静默（无变更时不输出）")
    p.add_argument("--force-check", action="store_true",
                   help="忽略「每天一次」限制，强制立即联网核对")
    # intake 的位置参数
    p.add_argument("image", nargs="?", help="intake stage 的图片路径")
    p.add_argument("assessment", nargs="?", help="intake stage 的评估 JSON 路径")
    p.add_argument("--stage-id", default=None, help="intake commit/abort 的 stage_id")
    p.add_argument("--confirm", action="store_true", help="intake commit 的确认标记")
    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)

    # intake 的 stage_id 从位置参数兜底取
    if args.intake_cmd in ("commit", "abort") and not args.stage_id:
        args.stage_id = args.image

    try:
        if args.update:
            cfg = gh.load_config()
            r = vc.daily_check(cfg=cfg, force=args.force_check, auto_update=True)
            if args.json:
                print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
            elif not (args.quiet and r.get("action") == "match"):
                print(vc.format_line(r))
            if r.get("action") in ("daily_skipped", "daily_failed_cached"):
                return RET_OK
            return _rc_from_action(r.get("action"))

        if args.check:
            return cmd_check(args)

        if args.status:
            return cmd_status(args)
        if args.probe:
            return cmd_probe(args)
        if args.audit:
            return cmd_audit(args)
        if args.push:
            return cmd_push(args)
        if args.intake_cmd:
            return cmd_intake(args)

        p.print_help()
        return RET_OK
    except Exception as e:
        print("❌ 异常: %s: %s" % (type(e).__name__, e))
        return RET_ERR


if __name__ == "__main__":
    sys.exit(main())
