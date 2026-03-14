from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    UUID,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from internal.extension.database_extension import db


class MCPServer(db.Model):
    """MCP server provider model."""

    __tablename__ = "mcp_server"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_mcp_server_id"),
        Index("mcp_server_account_id_idx", "account_id"),
        Index("mcp_server_account_id_name_idx", "account_id", "name", unique=True),
    )

    id = Column(UUID, nullable=False, server_default=text("uuid_generate_v4()"))
    account_id = Column(UUID, nullable=False)
    name = Column(
        String(255), nullable=False, server_default=text("''::character varying")
    )
    icon = Column(
        String(255), nullable=False, server_default=text("''::character varying")
    )
    description = Column(Text, nullable=False, server_default=text("''::text"))
    transport = Column(
        String(50), nullable=False, server_default=text("'stdio'::character varying")
    )
    enabled = Column(Boolean, nullable=False, server_default=text("true"))
    url = Column(
        String(1024), nullable=False, server_default=text("''::character varying")
    )
    command = Column(
        String(255), nullable=False, server_default=text("''::character varying")
    )
    args = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    env = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    headers = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    timeout = Column(Integer, nullable=False, server_default=text("30"))
    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(0)"),
        server_onupdate=text("CURRENT_TIMESTAMP(0)"),
    )
    created_at = Column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP(0)")
    )

    @property
    def tools(self) -> list["MCPTool"]:
        return db.session.query(MCPTool).filter_by(server_id=self.id).all()


class MCPTool(db.Model):
    """Discovered MCP tool catalog model."""

    __tablename__ = "mcp_tool"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_mcp_tool_id"),
        Index("mcp_tool_account_id_idx", "account_id"),
        Index("mcp_tool_server_id_name_idx", "server_id", "name", unique=True),
    )

    id = Column(UUID, nullable=False, server_default=text("uuid_generate_v4()"))
    account_id = Column(UUID, nullable=False)
    server_id = Column(UUID, nullable=False)
    name = Column(
        String(255), nullable=False, server_default=text("''::character varying")
    )
    description = Column(Text, nullable=False, server_default=text("''::text"))
    input_schema = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    raw_definition = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    discovered_at = Column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP(0)")
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(0)"),
        server_onupdate=text("CURRENT_TIMESTAMP(0)"),
    )
    created_at = Column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP(0)")
    )

    @property
    def server(self) -> "MCPServer":
        return db.session.query(MCPServer).get(self.server_id)
