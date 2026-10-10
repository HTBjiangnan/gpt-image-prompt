# gpt-image-prompt

GPT Image 2 / 2.5 提示词写作决策体系。输入内容特征，自动匹配版式，产出结构化的生图提示词。

适用于 Claude Code / WorkBuddy 等支持 Skill 机制的 Agent 环境。

---

## 它解决什么问题

写生图提示词最耗时的是"选版式"和"组织约束"。本技能把这件事变成查表：

1. 解析内容特征（信息密度、情绪基调、有无参考图）
2. 查「版式决策总表」→ 匹配版式编号
3. 调取对应场景模板 → 组装提示词
4. 有参考图时走双列编辑逻辑（保留什么 / 改什么 / 禁止什么）

**使用者不需要自己挑版式。**

---

## 内容

| 路径 | 说明 |
|------|------|
| `SKILL.md` | 技能主文件：调度层 / 结构层 / 逻辑层 / 场景速查表 |
| `references/GPT-Image-2-提示词完整指南.md` | 完整指南：八段式结构、版式决策总表、58 场景模板、风格关键词、通用参数、实战案例 |
| `references/收录评估标准.md` | 候选图片「是否值得收录」的严格评分标准 |
| `tools/` | 版本自更新与图片收录工具（纯 Python 标准库） |
| `VERSION.json` | 版本权威文件（含各文件 sha256 清单） |

---

## 安装

把本目录放到 Agent 的技能目录下即可，例如：

```
~/.workbuddy/skills/gpt-image-prompt/
```

或直接 clone：

```bash
git clone https://github.com/HTBjiangnan/gpt-image-prompt.git \
  ~/.workbuddy/skills/gpt-image-prompt
```

---

## 版本自更新

技能每次加载时会先执行启动自检：

```bash
python tools/summon.py --check
```

- **每天第一次**加载时联网核对版本；当日后续加载只做离线一致性自检（不联网、不卡顿）
- 远端版本更新 → 自动下载覆盖（**只升不降**，覆盖前自动备份，sha256 校验，失败回滚）
- 公开仓库读取**不需要任何凭证**，换机零配置即可用

其他命令：

```bash
python tools/summon.py --status          # 版本 / 通道 / 配置状态
python tools/summon.py --probe           # 探测可用网络通道
python tools/summon.py --update          # 立即核对并更新
python tools/summon.py --force-check     # 忽略「每天一次」限制
```

网络偶发不通时会自动在多个通道间降级（`raw` → `jsDelivr` → `codeload` → `api`），
全部失败也只是跳过核对，不影响提示词生成。

---

## 图片收录评估（可选）

把生成的图片交给技能，它会按 `references/收录评估标准.md` 严格评分，
通过后作为候选提交到本仓库的追踪 Issue，由维护者决定是否收录。

> 提交内容**只有文字**（评分明细、判定、拟新增的模板骨架与补充说明）。
> 本技能的沉淀物是文字模板，图片仅作为评估输入，因此不向仓库提交图片二进制——
> 仓库体积零增长，也不依赖任何图床或图片外链。

评估分两层：**硬否决闸门**（敏感数据 / 文字错字 / 画面硬伤 / 不可抽象为模板 / 已被覆盖 / 超出定位）
与 **严格打分**，收录线 **75 分**。

提交需要 PAT（写权限），未配置时自动降级为只读。

```bash
python tools/summon.py --intake stage <图片路径> <评估JSON>
python tools/summon.py --intake commit <stage_id> --confirm
```

---

## 许可

未授予开源许可，保留所有权利。仅供学习参考。
