---
name: ima-skill
description: 通过 IMA OpenAPI 管理个人笔记和知识库：搜索、读取、创建、追加笔记，上传文件/网页并检索知识库。用户提到 IMA、知识库、资料库、笔记、收藏网页或“帮我记一下”时使用；没有配置 IMA 凭据或对应 API 工具时只提供操作方案，不虚构已完成同步。
metadata:
  my_dify:
    version: "1.0.0"
    category: knowledge
    execution_mode: sandbox
    requires:
      - IMA_OPENAPI_CLIENTID
      - IMA_OPENAPI_APIKEY
allowed-tools:
  - read_file
  - execute
---

# IMA 知识库与笔记

这是一个需要用户自行配置凭据的外部服务技能。凭据只发送到 IMA 官方 API；不要写入 Skill 文件、日志或交付内容，也不要把凭据传给其他域名。

## 路由

- 笔记搜索、列表、读取、创建、追加：先读取 [notes/SKILL.md](notes/SKILL.md)；追加必须由用户明确指定目标笔记。
- 知识库搜索、浏览、上传文件、导入网页、查看原文：先读取 [knowledge-base/SKILL.md](knowledge-base/SKILL.md)。
- 完整参数与错误码：按需读取对应目录下的 `references/api.md`。

## 安全规则

1. 执行写入或上传前确认目标、文件名、内容范围和是否覆盖/追加；读取操作也要遵守用户隐私边界。
2. 上传二进制文件保持原始字节，不擅自改名、转码或覆盖同名内容；先检查重复名称。
3. 笔记正文属于隐私。在群聊或多人上下文只展示标题与必要摘要，不展示全文。
4. 无凭据、API 调用失败或返回不确定时，明确报告状态和下一步配置方式；不声称“已保存/已同步”。
5. 调用脚本时使用当前 Skill 包中的路径（例如 `/skills/ima-skill/ima_api.cjs`），用户文件统一使用 `/workspace/` 虚拟路径。
