---
name: ontology
description: 用实体和关系构建可验证的轻量知识图谱，适合管理人员、项目、任务、文档及依赖关系。用户要求关联对象、查询依赖或结构化记忆时使用；不存储凭据原文。
metadata:
  my_dify:
    version: "1.0.0"
    category: knowledge
    execution_mode: sandbox
    requires: []
allowed-tools:
  - read_file
  - write_file
  - execute
---

# 轻量本体图

图谱存储在 `/workspace/memory/ontology/graph.jsonl`，每行是一条追加事件；约束存储在 `/workspace/memory/ontology/schema.yaml`。实体包含 `id`、`type`、`properties` 和时间字段，关系包含 `from`、`rel`、`to`。

## 核心类型

优先使用 `Person`、`Organization`、`Project`、`Task`、`Goal`、`Event`、`Document`、`Note` 和 `Action`。不确定类型时先选最接近的类型并在属性中说明，不凭空扩展复杂本体。

## 操作流程

1. 创建或更新前读取现有图谱，按稳定业务键（如项目名或文档路径）去重；不要覆盖 JSONL 历史。
2. 每次变更写入一条结构化 JSON 事件，再执行查询或校验确认实体和关系存在。
3. 查询“某项目的任务”“谁负责什么”“依赖链”时先定位实体，再沿关系遍历；回答中区分直接记录和推断路径。
4. 任务关系使用 `blocks`、`depends_on` 等明确动词；发现循环依赖、缺少必填属性或关系端点不存在时拒绝提交并说明原因。

## 凭据与隐私

只允许保存 `credential_ref` 等引用，不保存密码、Token、Cookie 或完整个人敏感资料。图谱文件路径必须位于 `/workspace/`，外发或删除历史记录前需要确认。
