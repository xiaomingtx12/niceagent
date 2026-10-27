---
name: diagram-builder
description: 将需求、代码或长文本整理为可读的流程图、架构图、时序图、状态图或 ER 图。用户要求画图、梳理系统关系或把文字可视化时使用；不用于统计图表、插画和像素级排版。
metadata:
  my_dify:
    version: "1.0.0"
    category: data
    execution_mode: sandbox
    requires: []
allowed-tools:
  - read_file
  - write_file
---

# 流程与架构图

先确定图要回答的一个核心问题，再决定层级和粒度。节点必须有职责或状态，连线必须能复述为明确关系；复杂内容拆成多个视图，单图通常不超过 20 个节点。

## 工作步骤

1. 读取用户指定的文件或代码，提取参与者、模块、动作、输入输出和异常分支；不要把未解释的术语直接画成节点。
2. 选择 Mermaid 语法：`flowchart` 表示流程/架构，`sequenceDiagram` 表示时序，`stateDiagram-v2` 表示状态机，`erDiagram` 表示数据关系。
3. 先输出可编辑 Mermaid 代码，再按用户要求写入 `/workspace/` 的 `.md` 或 `.mmd` 文件；中文标签使用引号并避免特殊字符破坏语法。
4. 检查方向、节点职责、连线标签、异常路径和图例。若用户需要 SVG/PNG，只有在运行时存在对应渲染器时才执行并核验文件非空。

## 交付模板

```markdown
图示重点：一句话说明本图回答的问题

```mermaid
flowchart LR
  A[用户请求] --> B[处理模块]
  B --> C[(数据存储)]
```

假设与限制：列出未确认的关系、缺失信息和渲染条件。
```

不支持的图形类型应主动说明原因并给出 Mermaid 或现有图表技能的替代方案，不伪造已经生成的图片文件。
