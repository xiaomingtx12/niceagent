---
name: web-research
description: 进行有来源、可复核的网页检索与资料整理。用户要求查新闻、事实、产品资料、政策、竞品或最新信息时使用；没有可用联网工具时明确说明限制，不把模型记忆当作实时搜索结果。
metadata:
  my_dify:
    version: "1.0.0"
    category: knowledge
    execution_mode: sandbox
    requires: []
allowed-tools:
  - read_file
  - execute
---

# 网页研究

先明确问题、时间范围、地区、语言和可信度要求，再检索。研究结果必须让用户能区分来源事实、推断和未解决问题。

## 研究流程

1. 将问题拆成 2~5 个可检索子问题，优先官方文档、政府/机构、论文、原始公告和一手数据。
2. 对每个关键结论至少保留一个来源：标题、发布者、日期、URL 和支持结论的摘录。不要只给搜索摘要。
3. 交叉验证时比较发布时间、定义、样本、地区和利益关系；冲突信息并列呈现，不强行平均或选取迎合结论的一条。
4. 时间敏感内容标注“检索日期”，并说明页面可能变化；无法访问、需要登录或付费的来源要如实标注。
5. 输出简洁结论、证据表、方法/限制和下一步。用户只要链接时不扩写长报告，但仍保证链接与标题对应。

## 默认输出

```markdown
## 结论

## 证据
| 结论 | 来源 | 日期 | 原文依据 |

## 不确定性与冲突

## 检索范围与限制
检索日期：
覆盖范围：
无法访问/待核实：
```

`web_search`、`web_fetch`、浏览器或其他网络工具只有在运行时实际提供时才能调用。不要安装或执行 SkillHub/OpenClaw 的联网脚本，不要泄露 Cookie、API Key 或用户私密信息。可按需读取 [references/opencli-guide.md](references/opencli-guide.md) 和 [references/well-known-sites.json](references/well-known-sites.json) 了解来源选择；配置缺失时读取 [references/web-search-config.md](references/web-search-config.md)。
