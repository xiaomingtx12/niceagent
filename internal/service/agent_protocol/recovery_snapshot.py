"""把已提交协议事件折叠成可直接展示、可继续接收增量的恢复材料。"""

from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from internal.schema.agent.agent_protocol_schema import AgentProtocolEvent


def compact_execution_events(events: Iterable[AgentProtocolEvent]) -> list[AgentProtocolEvent]:
    """保留调用身份和边界，仅合并同一内容块的增量；不修改落库原始事件。"""
    # 1.按序号保存输出，按调用与内容块索引维护合并状态，隔离并行子 Agent。
    kept: dict[int, dict[str, Any]] = {}
    blocks: dict[tuple[Any, ...], dict[str, Any]] = {}
    block_types: dict[tuple[Any, ...], str | None] = {}
    unsupported: set[tuple[Any, ...]] = set()
    for event in events:
        item = event.model_dump(mode="json")
        params = item["params"]
        data = params["data"]
        namespace = tuple(params["namespace"])
        if not isinstance(data, dict):
            kept[event.seq] = item
            continue
        if event.method == "values":
            # 2.正文由 messages 通道完整保留，不再复制图状态中的聊天历史。
            item["params"]["data"] = {key: value for key, value in data.items() if key != "messages"}
        elif event.method == "messages":
            metadata = (params.get("metadata") or {}).get("my_dify") or {}
            call_key = (namespace, params.get("node"), metadata.get("model_run_id"))
            event_name = data.get("event")
            if event_name == "message-start":
                # 3.新消息重置当前调用的合并上下文，兼容没有调用 ID 的旧事件。
                blocks = {key: value for key, value in blocks.items() if key[:3] != call_key}
                block_types = {key: value for key, value in block_types.items() if key[:3] != call_key}
                unsupported = {key for key in unsupported if key[:3] != call_key}
            elif event_name in {"content-block-start", "content-block-delta", "content-block-finish"}:
                # 4.仅在同一类型内容块内合并，无法识别的分片及后续事件保持原样。
                index_key = (*call_key, data.get("index", 0))
                content_type = _content_type(data, block_types.get(index_key))
                block_types[index_key] = content_type
                key = (*index_key, content_type)
                if key in unsupported:
                    kept[event.seq] = item
                    continue
                previous = blocks.get(key)
                previous_content = previous["params"]["data"]["content"] if previous else None
                content = _merge_content(previous_content, data)
                if content is not None:
                    if previous is None:
                        seed = deepcopy(item)
                        seed["params"]["data"] = {
                            "event": "content-block-start",
                            "index": data.get("index", 0),
                            "content": content,
                        }
                        blocks[key] = seed
                        kept[event.seq] = seed
                        if event_name == "content-block-finish":
                            kept[event.seq] = item
                        continue
                    previous["params"]["data"]["content"] = content
                    if event_name != "content-block-finish":
                        continue
                else:
                    # 未知分片保持原样，避免未来新增协议类型被错误合并。
                    blocks.pop(key, None)
                    unsupported.add(key)
        kept[event.seq] = item
    # 5.合并正文保留原起点序号和结束边界，恢复后仍能衔接水位之后的实时事件。
    return [AgentProtocolEvent.model_validate(kept[sequence]) for sequence in sorted(kept)]


def _content_type(data: Mapping[str, Any], previous_type: str | None) -> str | None:
    """完整内容优先，其次识别标准增量；缺少类型的分片沿用当前内容块。"""
    content = data.get("content") or {}
    delta = data.get("delta") or {}
    delta_content_type = {"text-delta": "text", "reasoning-delta": "reasoning"}.get(delta.get("type"))
    return content.get("type") or delta_content_type or previous_type


def _merge_content(current: Mapping[str, Any] | None, data: Mapping[str, Any]) -> dict[str, Any] | None:
    """按 SDK 内容块语义合并；未知增量返回 None，由调用方保留原始事件。"""
    # 1.完整起止内容直接替换；旧格式 content 增量仅拼接文本字段并保留调用身份。
    content = data.get("content")
    if isinstance(content, dict):
        same_type = current and current.get("type") == content.get("type")
        if data.get("event") != "content-block-delta" or not same_type:
            return deepcopy(content)
        merged = {**current, **content}
        field = {
            "text": "text", "reasoning": "reasoning",
            "tool_call_chunk": "args", "server_tool_call_chunk": "args",
        }.get(content.get("type"))
        if field:
            merged[field] = str(current.get(field) or "") + str(content.get(field) or "")
        for field in ("id", "name"):
            if content.get(field) is None and field in current:
                merged[field] = current[field]
        return merged
    # 2.标准 delta 只拼接其声明的字段，不混合正文、思考和数据内容。
    delta = data.get("delta")
    if not isinstance(delta, dict):
        return None
    kind = delta.get("type")
    if kind in {"text-delta", "reasoning-delta", "data-delta"}:
        field = {"text-delta": "text", "reasoning-delta": "reasoning", "data-delta": "data"}[kind]
        merged = dict(current or {"type": field})
        merged[field] = str(merged.get(field) or "") + str(delta.get(field) or "")
        if "encoding" in delta:
            merged["encoding"] = delta["encoding"]
        return merged
    if kind == "block-delta" and isinstance(delta.get("fields"), dict):
        return {**(current or {}), **delta["fields"]}
    # 3.未知协议留给调用方原样保存，不推测其合并语义。
    return None
