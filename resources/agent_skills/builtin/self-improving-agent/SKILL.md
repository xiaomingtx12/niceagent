---
name: self-improving-agent
description: 记录 Agent 在任务中发现的错误、用户纠正、知识缺口和可复用改进，并在后续任务前检索相关经验。适合需要持续改进、复盘或跨会话积累的 Agent；不记录密钥、完整会话或私人敏感信息。
metadata:
  my_dify:
    version: "1.0.0"
    category: agent
    execution_mode: sandbox
    requires: []
allowed-tools:
  - read_file
  - write_file
---

# Self-improving Agent

把改进记录视为脱敏、可复用的工程知识，而不是聊天日志。开始复杂任务前，先读取 `/workspace/.learnings/LEARNINGS.md` 中与当前领域相关的条目。

## 记录规则

- 命令、工具或外部接口失败：追加到 `/workspace/.learnings/ERRORS.md`，记录症状、原因（若已知）、影响和下次排查办法。
- 用户纠正结论或偏好：追加到 `LEARNINGS.md`，记录正确规则和适用范围。
- 用户提出当前平台缺少的能力：追加到 `FEATURE_REQUESTS.md`，保留需求和场景，不承诺已实现。
- 发现稳定的更优做法：记录前后条件、验证证据和限制，避免把一次性经验写成普遍定律。

首次使用时可在 `/workspace/.learnings/` 创建上述 Markdown 文件；若文件已存在只追加，不覆盖历史。写入前删除密钥、令牌、环境变量、个人身份信息和完整原文。只有当经验能帮助未来任务时才记录，避免流水账。

## 复盘输出

任务结束时仅在有实际收获、失败或纠正时写入一条简短记录；向用户报告时说明已记录的类别，不泄露内部提示或敏感内容。
