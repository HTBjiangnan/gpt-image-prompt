# -*- coding: utf-8 -*-
"""image_intake.py - 图片收录评估（严格闸门 + 两阶段确认 + 提交）

分工：
- 视觉判断由 Agent（多模态）完成，写成 assessment JSON
- 本模块负责：指纹去重、闸门/评分校验、暂存、两阶段确认、提交、审计
  （保持纯标准库，不做任何视觉推理）

重要：本技能不向仓库提交图片二进制。
技能沉淀物是**文字模板**（SKILL.md / references/），图片仅作为 Agent 的
判断输入，评估结论以纯文字评论写入追踪 Issue。因此仓库体积零增长，
也不依赖任何图床或图片外链。

严格判定两层：
  第一层 硬否决闸门 G1..G6 —— 任一命中直接拒绝，不进入打分
  第二层 严格打分 5 个维度，收录线 75 分（边缘档 60-74 默认不提交）

CLI:
  stage  <图片路径> <assessment.json>   生成暂存与拟提交内容（不提交）
  commit <stage_id> --confirm           用户确认后真正提交
  abort  <stage_id>                     放弃
  list                                  列出暂存项
"""

import argparse
import hashlib
import json
import os
import sys
import time

try:
    from . import gh_client as gh
    from . import github_sync as sync
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gh_client as gh
    import github_sync as sync

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAGE_DIR = os.path.join(SKILL_ROOT, ".staged")
AUDIT_LOG = gh.AUDIT_LOG  # 由数据目录统一管理（gh_client.DATA_HOME）

TRACKING_ISSUE_TITLE = "【收录候选】候选图片追踪（本技能自动创建，请勿删除）"
TRACKING_ISSUE_LABEL = "候选收录"

# ---- 判定阈值（严格）----
THRESH_STRONG = 85
THRESH_WORTH = 75
THRESH_EDGE = 60

WEIGHTS = {"novelty": 30, "reusability": 25, "quality": 20, "differentiation": 15, "coverage": 10}
DIM_LABEL = {"novelty": "版式新颖度", "reusability": "模板可复用性", "quality": "视觉质量",
             "differentiation": "差异化参照价值", "coverage": "业务场景覆盖度"}

GATES = {
    "G1": "含敏感数据（客户姓名/保单号/客户号/身份证/手机号/内部经营数据/内网地址/密钥）",
    "G2": "文字渲染有错（错字/缺字/乱码/字形崩坏）",
    "G3": "画面有硬伤（结构畸变/透视错乱/比例失真/物件崩坏）",
    "G4": "无法抽象为可复用模板",
    "G5": "已被现有 58 场景覆盖且无实质改进",
    "G6": "超出技能定位（非生图提示词范畴）",
}

RET_OK, RET_REJECT, RET_PENDING, RET_DUP, RET_SENSITIVE, RET_ERR = 0, 1, 2, 3, 40, 99


# ============================================================
# 指纹与暂存
# ============================================================

def image_fingerprint(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ============================================================
# 第一层：硬否决闸门
# ============================================================

def check_gates(assessment):
    """返回失败闸门列表 [{gate, reason, evidence}]"""
    failed = []
    gates = assessment.get("gates") or {}
    for code, desc in GATES.items():
        item = gates.get(code)
        if not isinstance(item, dict):
            failed.append({"gate": code, "reason": "未提供闸门判定", "evidence": desc})
            continue
        if not item.get("passed", False):
            ev = str(item.get("evidence") or "").strip()
            if not ev:
                failed.append({"gate": code, "reason": "否决但未提供具体证据", "evidence": desc})
            else:
                failed.append({"gate": code, "reason": desc, "evidence": ev})
    return failed


# ============================================================
# 第二层：严格打分校验
# ============================================================

def validate_scores(assessment):
    """返回 (scores, total, err)。总分与分项和不符即报错（防自评抬分）"""
    raw = assessment.get("scores") or {}
    scores = {}
    for key, cap in WEIGHTS.items():
        v = raw.get(key)
        if v is None:
            return None, None, "缺少评分维度: %s(%s)" % (key, DIM_LABEL[key])
        try:
            v = int(v)
        except Exception:
            return None, None, "评分非整数: %s=%r" % (key, raw.get(key))
        if v < 0 or v > cap:
            return None, None, "评分越界: %s=%s（上限 %s）" % (key, v, cap)
        if not str((raw.get("reasons") or {}).get(key) or "").strip():
            return None, None, "缺少扣分理由: %s（每维度必须写明理由）" % DIM_LABEL[key]
        scores[key] = v
    total = sum(scores.values())
    declared = assessment.get("total")
    if declared is not None:
        try:
            if int(declared) != total:
                return None, None, "总分与分项和不符（声明 %s / 实算 %s），拒绝采信" % (declared, total)
        except Exception:
            return None, None, "total 字段非法: %r" % declared
    return scores, total, None


def verdict_of(total):
    if total >= THRESH_STRONG:
        return "strong", "强烈推荐"
    if total >= THRESH_WORTH:
        return "worth", "值得收录"
    if total >= THRESH_EDGE:
        return "borderline", "边缘候选（默认不提交）"
    return "reject", "不收录"


def _sensitive_hits(*texts):
    import re
    hits = []
    for t in texts:
        t = str(t or "")
        for label, pat in sync.AUDIT_PATTERNS:
            for m in re.finditer(pat, t):
                hits.append({"rule": label, "match": m.group(0)[:60]})
    return hits


# ============================================================
# 暂存（第一阶段：只出结论，不提交）
# ============================================================

def stage(image_path, assessment_path, cfg=None):
    result = {"stage_id": None, "verdict": None, "total": None, "return_code": RET_ERR, "message": "", "detail": ""}

    if not os.path.isfile(image_path):
        result["message"] = "图片不存在: %s" % image_path
        return result
    try:
        with open(assessment_path, "r", encoding="utf-8") as f:
            assessment = json.load(f)
    except Exception as e:
        result["message"] = "评估文件解析失败: %s" % e
        return result

    sha = image_fingerprint(image_path)
    title = str(assessment.get("title") or "候选版式").strip()
    result["sha256"] = sha

    # ---- 第一层闸门 ----
    failed = check_gates(assessment)
    if failed:
        sensitive = any(f["gate"] == "G1" for f in failed)
        result.update(verdict="reject", total=0,
                      return_code=RET_SENSITIVE if sensitive else RET_REJECT,
                      message="硬否决：%s" % "；".join("%s %s" % (f["gate"], f["reason"]) for f in failed),
                      detail=json.dumps(failed, ensure_ascii=False))
        _audit("stage_reject", sha, 0, "reject", None, failed)
        return result

    # ---- 第二层打分 ----
    scores, total, err = validate_scores(assessment)
    if err:
        result["message"] = "评分校验不通过: %s" % err
        return result
    band, band_label = verdict_of(total)
    result["total"] = total

    # ---- 提交正文脱敏 ----
    proposed_body = str(assessment.get("proposed_body") or "").strip()
    proposed_template = str(assessment.get("proposed_template") or "").strip()
    sens = _sensitive_hits(proposed_body, proposed_template, title)
    if sens:
        result.update(verdict="reject", return_code=RET_SENSITIVE,
                      message="拟提交内容含敏感信息，已阻断",
                      detail=json.dumps(sens, ensure_ascii=False))
        _audit("stage_sensitive_block", sha, total, "reject", None, sens)
        return result

    if not proposed_template:
        result.update(verdict="reject", return_code=RET_REJECT,
                      message="未提供可参数化模板骨架 → 按 G4 否决")
        _audit("stage_reject_g4", sha, total, "reject", None, "no template")
        return result

    stage_id = "%s-%s" % (time.strftime("%Y%m%d%H%M%S"), sha[:8])
    record = {
        "stage_id": stage_id,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "image_path": os.path.abspath(image_path),
        "sha256": sha,
        "title": title,
        "scores": scores,
        "reasons": (assessment.get("scores") or {}).get("reasons") or {},
        "total": total,
        "band": band,
        "band_label": band_label,
        "benchmark": assessment.get("benchmark") or "",          # ① 对标的现有场景
        "gap": assessment.get("gap") or "",                      # ② 差在哪
        "proposed_template_number": assessment.get("proposed_template_number") or "",  # ③ 拟编号
        "proposed_template": proposed_template,
        "proposed_body": proposed_body,
        "submitted": False,
    }
    os.makedirs(STAGE_DIR, exist_ok=True)
    with open(os.path.join(STAGE_DIR, stage_id + ".json"), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)

    # 去重检查（本地暂存 + 远端追踪 Issue 评论）
    cfg = cfg or gh.load_config()
    dup_remote, dup_err = remote_has_fingerprint(sha, cfg)
    dup_local = _local_dup(sha, exclude=stage_id)

    result.update(stage_id=stage_id, verdict=band, return_code=RET_PENDING,
                  message="评估完成：%s（%d/100）" % (band_label, total),
                  detail="拟提交内容：文字结论（评分 + 判定 + 模板骨架），不含图片")
    if dup_local or dup_remote:
        result.update(return_code=RET_DUP,
                      message="重复图片：%s（跳过提交）" % ("远端追踪 Issue 已收录" if dup_remote else "本地暂存已存在"),
                      detail=dup_err or "")
    result["preview"] = render_preview(record)
    _audit("stage_ok", sha, total, band, None, None)
    return result


def _local_dup(sha, exclude=None):
    if not os.path.isdir(STAGE_DIR):
        return False
    for fn in os.listdir(STAGE_DIR):
        if not fn.endswith(".json") or (exclude and fn.startswith(exclude)):
            continue
        try:
            with open(os.path.join(STAGE_DIR, fn), "r", encoding="utf-8") as f:
                if json.load(f).get("sha256") == sha:
                    return True
        except Exception:
            continue
    return False


def remote_has_fingerprint(sha, cfg):
    """扫描追踪 Issue 的评论，判断该指纹是否已提交过。

    本技能不向仓库提交图片二进制（技能沉淀物是文字模板），
    因此去重依据是"评论正文里的指纹"，而非仓库文件。
    返回 (bool, err)
    """
    issue_no = cfg.get("issue_number")
    if not issue_no:
        return False, None
    comments, err = gh.api_list_comments(issue_no, cfg)
    if err:
        return False, err
    prefix = sha[:16]
    for c in comments or []:
        if prefix in (c.get("body") or ""):
            return True, None
    return False, None


def render_preview(record):
    lines = []
    lines.append("【收录评估结论】%s  —  %d/100" % (record["band_label"], record["total"]))
    lines.append("")
    for key, cap in WEIGHTS.items():
        lines.append("  %s：%d/%d  —— %s" % (DIM_LABEL[key], record["scores"][key], cap,
                                              (record.get("reasons") or {}).get(key, "")))
    lines.append("")
    lines.append("① 对标现有场景：%s" % (record["benchmark"] or "（未填）"))
    lines.append("② 差距/改进点：%s" % (record["gap"] or "（未填）"))
    lines.append("③ 拟新增模板：%s" % (record["proposed_template_number"] or "（未填）"))
    lines.append("")
    lines.append("拟提交内容：追踪 Issue 中的一条文字评论（评分 + 判定 + 模板骨架）")
    lines.append("图片指纹：%s" % record["sha256"][:16])
    lines.append("拟提交模板骨架：")
    lines.append(record["proposed_template"])
    if record.get("band") == "borderline":
        lines.append("")
        lines.append("⚠️ 该图属「边缘候选」，按严格标准默认不提交。如确要提交，请显式回复「强制提交」。")
    return "\n".join(lines)


# ============================================================
# 提交（第二阶段：用户确认后）
# ============================================================

def commit(stage_id, confirm=False, force=False, cfg=None):
    cfg = cfg or gh.load_config()
    result = {"return_code": RET_ERR, "message": "", "detail": ""}

    path = os.path.join(STAGE_DIR, stage_id + ".json")
    if not os.path.isfile(path):
        result["message"] = "暂存项不存在: %s" % stage_id
        return result
    with open(path, "r", encoding="utf-8") as f:
        rec = json.load(f)

    if not confirm:
        result.update(return_code=RET_PENDING, message="未确认，不做任何提交",
                      detail="请用户确认后加 --confirm 重新执行")
        return result

    if rec["band"] == "borderline" and not force:
        result.update(return_code=RET_PENDING,
                      message="边缘候选默认不提交",
                      detail="如确要提交，请显式使用 --force")
        return result

    if not gh.has_token(cfg):
        result["message"] = "缺少 PAT，无法提交（只读模式）。请在 %s 配置 token。" % gh.CONFIG_PATH
        return result

    # 去重复检（依据：追踪 Issue 评论中已有的指纹）
    dup, _ = remote_has_fingerprint(rec["sha256"], cfg)
    if dup:
        result.update(return_code=RET_DUP,
                      message="该图已提交过（追踪 Issue 中已有同指纹候选），跳过")
        return result

    # 1) 固定追踪 Issue（不存在则创建）
    issue_no = cfg.get("issue_number")
    if not issue_no:
        issue, err = gh.api_create_issue(
            TRACKING_ISSUE_TITLE,
            "本 Issue 用于追踪经 `gpt-image-prompt` 技能评估通过的候选版式。\n\n"
            "每条评论 = 一条候选：评分明细、判定、以及**拟新增的模板骨架**。\n"
            "技能沉淀物是文字模板（SKILL.md / references/），"
            "因此评论只保留文字结论，不附带图片二进制。\n\n"
            "评论由技能自动追加，请勿删除。",
            cfg, labels=[TRACKING_ISSUE_LABEL])
        if err:
            result["message"] = "创建追踪 Issue 失败: %s" % err
            return result
        issue_no = issue.get("number")
        cfg["issue_number"] = issue_no
        try:
            gh.save_config(cfg)
        except Exception:
            pass

    # 2) 追加纯文字评论（不上传图片）
    rec["submitted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    body = render_comment(rec)
    comment, err = gh.api_create_comment(issue_no, body, cfg)
    if err:
        result["message"] = "评论失败: %s" % err
        return result

    rec["submitted"] = True
    rec["issue_number"] = issue_no
    rec["comment_url"] = comment.get("html_url", "")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)

    _audit("commit_ok", rec["sha256"], rec["total"], rec["band"], issue_no, rec["comment_url"])
    result.update(return_code=RET_OK,
                  message="已提交候选（%d/100）" % rec["total"],
                  detail="追踪 Issue #%s 评论: %s" % (issue_no, rec["comment_url"]))
    return result


def render_comment(rec):
    lines = []
    lines.append("### 收录候选 · %s（%d/100）" % (rec["title"], rec["total"]))
    lines.append("")
    lines.append("| 维度 | 得分 |")
    lines.append("|---|---|")
    for key, cap in WEIGHTS.items():
        lines.append("| %s | %d / %d |" % (DIM_LABEL[key], rec["scores"][key], cap))
    lines.append("| **合计** | **%d / 100** |" % rec["total"])
    lines.append("")
    lines.append("- 判定：**%s**" % rec["band_label"])
    lines.append("- ① 对标现有场景：%s" % (rec["benchmark"] or "—"))
    lines.append("- ② 差距/改进点：%s" % (rec["gap"] or "—"))
    lines.append("- ③ 拟新增模板：%s" % (rec["proposed_template_number"] or "—"))
    lines.append("- 图片指纹：`%s`" % rec["sha256"][:16])
    lines.append("- 提交时间：%s" % (rec.get("submitted_at") or time.strftime("%Y-%m-%d %H:%M:%S")))
    lines.append("")
    lines.append("**拟新增模板骨架**")
    lines.append("")
    lines.append("```")
    lines.append(rec["proposed_template"])
    lines.append("```")
    if rec.get("proposed_body"):
        lines.append("")
        lines.append("**补充说明**")
        lines.append("")
        lines.append(rec["proposed_body"])
    return "\n".join(lines)


def abort(stage_id):
    path = os.path.join(STAGE_DIR, stage_id + ".json")
    if not os.path.isfile(path):
        return False, "暂存项不存在: %s" % stage_id
    os.remove(path)
    return True, "已放弃暂存项 %s" % stage_id


def list_staged():
    out = []
    if os.path.isdir(STAGE_DIR):
        for fn in sorted(os.listdir(STAGE_DIR)):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(STAGE_DIR, fn), "r", encoding="utf-8") as f:
                        r = json.load(f)
                    out.append({"stage_id": r["stage_id"], "title": r["title"],
                                "total": r["total"], "band": r["band"],
                                "submitted": r.get("submitted", False)})
                except Exception:
                    continue
    return out


def _audit(action, sha, total, band, issue_no, extra):
    try:
        gh.ensure_data_home()
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action,
               "sha256": sha, "total": total, "band": band,
               "issue": issue_no, "extra": extra}
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ============================================================
# CLI
# ============================================================

def main(argv=None):
    p = argparse.ArgumentParser(description="图片收录评估")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("stage", help="生成暂存与拟提交内容（不提交）")
    s.add_argument("image")
    s.add_argument("assessment")

    c = sub.add_parser("commit", help="用户确认后提交")
    c.add_argument("stage_id")
    c.add_argument("--confirm", action="store_true")
    c.add_argument("--force", action="store_true", help="边缘候选强制提交")

    a = sub.add_parser("abort", help="放弃暂存项")
    a.add_argument("stage_id")

    sub.add_parser("list", help="列出暂存项")

    args = p.parse_args(argv)

    if args.cmd == "stage":
        r = stage(args.image, args.assessment)
        print(r.get("preview") or r["message"])
        return r["return_code"]
    if args.cmd == "commit":
        r = commit(args.stage_id, confirm=args.confirm, force=args.force)
        print(r["message"])
        if r.get("detail"):
            print(r["detail"])
        return r["return_code"]
    if args.cmd == "abort":
        ok, msg = abort(args.stage_id)
        print(msg)
        return 0 if ok else RET_ERR
    if args.cmd == "list":
        for x in list_staged():
            print("%s  %s  %d分  %s%s" % (x["stage_id"], x["title"], x["total"], x["band"],
                                          "  [已提交]" if x["submitted"] else ""))
        return 0
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
