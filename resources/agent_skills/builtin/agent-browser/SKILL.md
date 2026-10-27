---
name: agent-browser
description: 使用无障碍树和稳定元素引用完成网页导航、表单填写、信息提取和多步骤浏览器任务。用户要求操作网页、登录后流程或结构化抓取时使用；不适用于需要人工视觉判断的像素级操作。
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

# 浏览器自动化

优先使用平台实际提供的浏览器工具。若环境已安装 `agent-browser` CLI，可通过 `execute` 使用；先运行 `agent-browser --version` 检查，不要自动安装 npm 包或下载浏览器。

## 稳定操作流程

1. 打开目标 URL，等待页面加载完成，再获取交互元素的无障碍快照（优先 JSON、交互元素和较浅深度）。
2. 使用快照返回的元素引用执行点击、填写、选择、滚动和切换标签；每次页面跳转或提交后重新获取快照，不复用失效引用。
3. 提取信息时优先读取文本、链接和表格结构，记录来源 URL；需要截图或 PDF 时确认目标和输出格式后再执行。
4. 登录、付款、发布、删除或提交外部数据前暂停并请求用户确认；认证状态只保存在用户指定的 `/workspace/` 文件中，不输出 Cookie 和令牌。

## CLI 示例

```text
agent-browser open https://example.com
agent-browser snapshot -i --json
agent-browser click @e2
agent-browser fill @e3 "查询内容"
agent-browser press Enter
agent-browser wait --load networkidle
agent-browser snapshot -i --json
```

CLI 不存在、站点需要验证码或快照无法表达目标时，说明阻碍并改用可用的网页工具或请用户完成该步；不要声称已经完成操作。
