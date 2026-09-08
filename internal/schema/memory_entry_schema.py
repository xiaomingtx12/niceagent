"""结构化记忆管理接口 DTO。"""

from marshmallow import Schema, fields
from pydantic import Field

from internal.core.agent_v2.entity.memory_entry import MemoryChange


class MemoryEntryChangeReq(MemoryChange):
    """管理操作必须携带可解释的原因，身份不接受客户端指定。"""

    evidence: str = Field(min_length=1, max_length=1000)


class MemorySourceResp(Schema):
    """公开依据和会话来源，隐藏内部幂等操作标识。"""

    kind = fields.String()
    evidence = fields.String()
    conversation_id = fields.String()
    message_id = fields.String()


class MemoryRevisionResp(Schema):
    """最近版本的完整内容与状态，供用户比较和恢复。"""

    version = fields.Integer()
    action = fields.String()
    content = fields.String()
    status = fields.String()
    source = fields.Nested(MemorySourceResp)
    updated_at = fields.String()


class MemoryEntryResp(Schema):
    """只公开记忆管理所需字段。"""

    id = fields.String()
    topic = fields.String()
    scope = fields.String()
    category = fields.String()
    content = fields.String()
    status = fields.String()
    version = fields.Integer()
    source = fields.Nested(MemorySourceResp)
    created_at = fields.String()
    updated_at = fields.String()
    revisions = fields.List(fields.Nested(MemoryRevisionResp))


class ListMemoryEntriesResp(Schema):
    """有界目录不采用普通业务列表的无限分页。"""

    items = fields.List(fields.Nested(MemoryEntryResp))


class MemoryEntryChangeResp(Schema):
    """幂等或去重命中时明确返回未发生变更。"""

    changed = fields.Boolean()
    entry = fields.Nested(MemoryEntryResp)
