from dataclasses import dataclass
from uuid import UUID

from flask import request
from flask_login import current_user, login_required
from injector import inject

from internal.schema.mcp_schema import (
    CreateMCPServerReq,
    DebugMCPToolReq,
    DebugMCPToolResp,
    GetMCPServerResp,
    GetMCPServersWithPageReq,
    GetMCPServersWithPageResp,
    GetMCPToolResp,
    UpdateMCPServerReq,
)
from internal.service import MCPService
from pkg.paginator import PageModel
from pkg.response import success_json, success_message, validate_error_json


@inject
@dataclass
class MCPHandler:
    """HTTP handler for MCP server management APIs."""

    mcp_service: MCPService

    @login_required
    def get_mcp_servers_with_page(self):
        req = GetMCPServersWithPageReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)

        servers, paginator = self.mcp_service.get_mcp_servers_with_page(req, current_user)
        resp = GetMCPServersWithPageResp(many=True)
        return success_json(PageModel(list=resp.dump(servers), paginator=paginator))

    @login_required
    def create_mcp_server(self):
        req = CreateMCPServerReq()
        if not req.validate():
            return validate_error_json(req.errors)
        self.mcp_service.create_mcp_server(req, current_user)
        return success_message("创建 MCP 服务成功")

    @login_required
    def get_mcp_server(self, provider_id: UUID):
        server = self.mcp_service.get_mcp_server(provider_id, current_user)
        resp = GetMCPServerResp()
        return success_json(resp.dump(server))

    @login_required
    def update_mcp_server(self, provider_id: UUID):
        req = UpdateMCPServerReq()
        if not req.validate():
            return validate_error_json(req.errors)
        self.mcp_service.update_mcp_server(provider_id, req, current_user)
        return success_message("更新 MCP 服务成功")

    @login_required
    def refresh_mcp_server(self, provider_id: UUID):
        self.mcp_service.refresh_mcp_server(provider_id, current_user)
        return success_message("刷新 MCP 工具目录成功")

    @login_required
    def get_mcp_tool(self, provider_id: UUID, tool_name: str):
        tool = self.mcp_service.get_mcp_tool(provider_id, tool_name, current_user)
        resp = GetMCPToolResp()
        return success_json(resp.dump(tool))

    @login_required
    def debug_mcp_tool(self, provider_id: UUID, tool_name: str):
        req = DebugMCPToolReq()
        if not req.validate():
            return validate_error_json(req.errors)
        result = self.mcp_service.debug_mcp_tool(
            provider_id, tool_name, req.parameters.data, current_user
        )
        resp = DebugMCPToolResp()
        return success_json(resp.dump(result))

    @login_required
    def delete_mcp_server(self, provider_id: UUID):
        self.mcp_service.delete_mcp_server(provider_id, current_user)
        return success_message("删除 MCP 服务成功")
