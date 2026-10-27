---
name: meeting-and-brief
description: 将会议记录、访谈转写、周报素材或零散工作笔记整理成结构化纪要、决策摘要、行动项和简报。用户要求总结会议、提取待办、跟进负责人/截止日期或生成周报时使用；不把推测内容写成已确认事实。
metadata:
  my_dify:
    version: "1.0.0"
    category: office
    execution_mode: inline
    requires: []
allowed-tools:
  - read_file
  - write_file
  - edit_file
---

# 会议纪要与工作简报

把原始记录变成能执行、能追踪的结果。输入可以是 `.txt`、`.md`、`.docx` 导出的文本或用户直接粘贴的内容；音频/视频需要先由外部 ASR 转写。

## 提取规则

1. 区分**事实、决定、观点、风险和待确认项**。没有录音或原文证据时，不补写“大家同意”或虚构负责人。
2. 识别参与人、组织、项目、客户、日期和金额；同名实体先标注歧义，必要时保留原称呼。
3. 行动项必须包含：任务、负责人（未知写“待确认”）、截止时间（未知写“未设定”）、依赖/验收标准和来源片段。
4. 决策记录说明结论、理由、未解决问题和后续复盘点；将不同意见保留为上下文。

## 默认交付

```markdown
# 会议/简报标题
日期与参与人：

## 一句话结论

## 关键讨论
- 主题：事实与证据

## 已确认决策
| 决策 | 依据 | 生效时间 | 记录来源 |

## 行动项
| 优先级 | 任务 | 负责人 | 截止时间 | 验收标准 | 状态 |

## 风险与待确认

## 下一步
```

用户只要快速摘要时，输出 3~7 条结论和行动项，不强制生成完整模板；要求归档时写入 `/workspace/`，文件名包含日期和主题，不覆盖原始记录。

## 质量检查

- 每个行动项能在原文找到依据，或明确标注为建议。
- 截止日期统一为 `YYYY-MM-DD`，相对时间（“下周五”）保留原文并标注待确认。
- 说话人不确定时使用“未识别发言者”，不要猜姓名。
- 周报/简报引用历史记录时只使用当前可读文件，不能声称访问了未提供的日历、邮箱或数据库。

需要更细的字段或模板时按需读取：[meeting-template.md](references/meeting-template.md)、[action-tracker-spec.md](references/action-tracker-spec.md)、[deliverable-mining-rules.md](references/deliverable-mining-rules.md)、[meeting-types.md](references/meeting-types.md)。
