from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from injector import inject
from sqlalchemy import desc

from internal.core.mcp import MCPRuntimeManager, MCPSecretCipher
from internal.exception import NotFoundException, ValidateErrorException
from internal.model import Account, MCPServer, MCPTool
from internal.schema.tool.mcp_schema import (
    CreateMCPServerReq,
    GetMCPServersWithPageReq,
    UpdateMCPServerReq,
)
from pkg.paginator import Paginator
from pkg.sqlalchemy import SQLAlchemy
from internal.service.common.base_service import BaseService


@inject
@dataclass
class MCPService(BaseService):
    """CRUD and runtime orchestration for MCP server providers."""

    db: SQLAlchemy
    mcp_runtime_manager: MCPRuntimeManager

    def _get_server_or_raise(self, provider_id: UUID, account: Account) -> MCPServer:
        server = self.get(MCPServer, provider_id)
        if server is None or server.account_id != account.id:
            raise NotFoundException("该 MCP 服务不存在")
        return server

    @classmethod
    def _normalize_payload(
        cls, req: CreateMCPServerReq | UpdateMCPServerReq, existing: MCPServer | None = None
    ) -> dict[str, Any]:
        env = req.env.data or {}
        headers = req.headers.data or []
        if existing is not None:
            env = {
                key: existing.env.get(key, "") if value == "********" else value
                for key, value in env.items()
            }
            headers = [
                {
                    "key": item["key"],
                    "value": next(
                        (old.get("value", "") for old in (existing.headers or [])
                         if old.get("key") == item["key"]),
                        "",
                    ) if item["value"] == "********" else item["value"],
                }
                for item in headers
            ]
        return {
            "name": req.name.data.strip(),
            "icon": (req.icon.data or "").strip(),
            "description": (req.description.data or "").strip(),
            "transport": req.transport.data,
            "enabled": bool(req.enabled.data),
            "url": (req.url.data or "").strip(),
            "command": (req.command.data or "").strip(),
            "args": req.args.data or [],
            "env": MCPSecretCipher.encrypt_env(env),
            "headers": MCPSecretCipher.encrypt_headers(headers),
            "timeout": int(req.timeout.data or 30),
        }

    def _check_duplicate_name(
        self, account_id: UUID, name: str, provider_id: UUID | None = None
    ) -> None:
        query = self.db.session.query(MCPServer).filter(
            MCPServer.account_id == account_id,
            MCPServer.name == name,
        )
        if provider_id is not None:
            query = query.filter(MCPServer.id != provider_id)
        if query.one_or_none():
            raise ValidateErrorException(f"MCP 服务名 {name} 已存在")

    def _replace_server_tools(
        self, server: MCPServer, tool_catalogs: list
    ) -> None:
        self.db.session.query(MCPTool).filter(MCPTool.server_id == server.id).delete()
        for tool_catalog in tool_catalogs:
            self.db.session.add(
                MCPTool(
                    account_id=server.account_id,
                    server_id=server.id,
                    name=tool_catalog.name,
                    description=tool_catalog.description,
                    input_schema=tool_catalog.input_schema,
                    raw_definition=tool_catalog.raw_definition,
                    discovered_at=datetime.now(),
                )
            )

    def get_mcp_servers_with_page(
        self, req: GetMCPServersWithPageReq, account: Account
    ) -> tuple[list[MCPServer], Paginator]:
        paginator = Paginator(db=self.db, req=req)
        filters = [MCPServer.account_id == account.id]
        if req.search_word.data:
            filters.append(MCPServer.name.ilike(f"%{req.search_word.data}%"))
        servers = paginator.paginate(
            self.db.session.query(MCPServer)
            .filter(*filters)
            .order_by(desc("created_at"))
        )
        return servers, paginator

    def create_mcp_server(self, req: CreateMCPServerReq, account: Account) -> MCPServer:
        payload = self._normalize_payload(req)
        self._check_duplicate_name(account.id, payload["name"])

        tool_catalogs = []
        if payload["enabled"]:
            candidate = MCPServer(account_id=account.id, **payload)
            tool_catalogs = self.mcp_runtime_manager.discover_tools(candidate, force_refresh=True)

        with self.db.auto_commit():
            server = MCPServer(account_id=account.id, **payload)
            self.db.session.add(server)
            self.db.session.flush()
            if payload["enabled"]:
                self._replace_server_tools(server, tool_catalogs)

        self.mcp_runtime_manager.invalidate(str(server.id))
        return server

    def update_mcp_server(
        self, provider_id: UUID, req: UpdateMCPServerReq, account: Account
    ) -> MCPServer:
        server = self._get_server_or_raise(provider_id, account)
        payload = self._normalize_payload(req, existing=server)
        self._check_duplicate_name(account.id, payload["name"], provider_id=server.id)

        tool_catalogs = []
        if payload["enabled"]:
            candidate = MCPServer(
                id=server.id,
                account_id=server.account_id,
                updated_at=datetime.now(),
                **payload,
            )
            tool_catalogs = self.mcp_runtime_manager.discover_tools(candidate, force_refresh=True)

        with self.db.auto_commit():
            for field, value in payload.items():
                setattr(server, field, value)
            self.db.session.query(MCPTool).filter(MCPTool.server_id == server.id).delete()
            self.db.session.flush()
            if payload["enabled"]:
                self._replace_server_tools(server, tool_catalogs)

        self.mcp_runtime_manager.invalidate(str(server.id))
        return server

    def get_mcp_server(self, provider_id: UUID, account: Account) -> MCPServer:
        return self._get_server_or_raise(provider_id, account)

    def refresh_mcp_server(self, provider_id: UUID, account: Account) -> MCPServer:
        server = self._get_server_or_raise(provider_id, account)
        if not server.enabled:
            raise ValidateErrorException("禁用状态的 MCP 服务不能刷新工具目录")

        tool_catalogs = self.mcp_runtime_manager.discover_tools(server, force_refresh=True)
        with self.db.auto_commit():
            self._replace_server_tools(server, tool_catalogs)

        self.mcp_runtime_manager.invalidate(str(server.id))
        return server

    def get_mcp_tool(self, provider_id: UUID, tool_name: str, account: Account) -> MCPTool:
        server = self._get_server_or_raise(provider_id, account)
        tool = (
            self.db.session.query(MCPTool)
            .filter(
                MCPTool.server_id == server.id,
                MCPTool.name == tool_name,
                MCPTool.account_id == account.id,
            )
            .one_or_none()
        )
        if tool is None:
            raise NotFoundException("该 MCP 工具不存在")
        return tool

    def debug_mcp_tool(
        self,
        provider_id: UUID,
        tool_name: str,
        parameters: dict[str, Any],
        account: Account,
    ) -> dict[str, Any]:
        server = self._get_server_or_raise(provider_id, account)
        self.get_mcp_tool(provider_id, tool_name, account)
        if not server.enabled:
            raise ValidateErrorException("该 MCP 服务已禁用，无法调试")

        try:
            result = self.mcp_runtime_manager.execute_tool(
                server_config=self.mcp_runtime_manager.build_server_config(server),
                tool_name=tool_name,
                parameters=parameters or {},
            )
            return {"success": True, "result": result, "error": None}
        except Exception as exc:
            return {"success": False, "result": None, "error": str(exc)}

    def delete_mcp_server(self, provider_id: UUID, account: Account) -> None:
        server = self._get_server_or_raise(provider_id, account)
        with self.db.auto_commit():
            self.db.session.query(MCPTool).filter(MCPTool.server_id == server.id).delete()
            self.db.session.delete(server)
        self.mcp_runtime_manager.invalidate(str(provider_id))
