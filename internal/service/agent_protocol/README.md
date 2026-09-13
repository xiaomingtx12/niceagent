# 工作台协议适配

本目录只负责工作台业务数据与前端协议之间的映射。框架事件解析在 `internal/core/agent_v2/execution/event_normalizer.py`；内部异步子 Agent 的 SDK 接口在相邻的 `internal_agent_protocol` 目录。

## 阅读顺序

1. `command_adapter.py`：把工作台命令分派给启动、回答和重新填写入口。
2. `event_projector.py`：接收 Core 规范化的 `AgentEvent`，绑定业务消息 ID、分配持久化序号并生成 `WorkbenchEvent`。
3. `state_projector.py`：业务消息标识、公开状态、表单和历史水合。已经保存的真实终态优先；缺失时才合成。
4. `message_projector.py`：业务消息历史转换与活动 Run 消息补齐。

## 一次事件交付

```text
LangGraph 原始流
  → Core 审核与规范化：消息分片、子图边界、摘要过滤、JSON 兼容载荷
  → AgentEvent
  → 工作台投影：业务 ID、公开通道与持久化序号
  → WorkbenchEvent
  → 批量提交、通知与 SSE
```

`AgentProtocolEventProjector.project` 一次最多返回一个公开事件。这里不导入 LangChain 消息类，不判断供应商 finish_reason，不累积文本或补造消息结束分片。Core 在成功、失败和取消退出时收尾已交付文本。

`generated` 与 `generated_state_transition` 用于业务主动产生的状态和表单事件。终态批次按“状态 → 表单 → 生命周期”排列，由 Run 服务在同一事务保存。Core 的图结束不代表平台数据已经提交，不能直接作为业务完成通知。

`to_public_event` 只解析数据库历史 JSON；实时路径直接构造最终协议对象，不经过历史兼容入口。公开 custom 数据仍按工作台响应 Schema 过滤。

## 历史与重连

历史水合不分配新持久化序号。完整视图复用真实终态中的公开错误、错误码和时间；compact 视图保留 terminal-hydration 标记供前端过滤旧分片，同时沿用真实终态载荷。

SSE 先读取运行状态，再扫描该 Run 的事件，两者都在同一次 Session 清理范围内。观察到终态后仍排空已提交事件，避免查询间的提交让尾批丢失。通知版本只负责唤醒，不存储事件正文。
