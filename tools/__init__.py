# -*- coding: utf-8 -*-
"""gpt-image-prompt 技能工具包

- gh_client              网络层：代理绕过 + 多通道探测 + GitHub API 封装
- github_version_check   版本核对（GitHub 为权威源）
- github_sync            下载 / 校验 / 备份 / 原子覆盖 / 回滚 / 反向发布 / 公开前审计
- image_intake           图片收录评估（严格闸门 + 两阶段确认 + 提交）
- startup_check          启动自检
- summon                 统一 CLI 入口
"""

__all__ = [
    "gh_client",
    "github_version_check",
    "github_sync",
    "image_intake",
    "startup_check",
    "summon",
]

__version__ = "3.1.0"
