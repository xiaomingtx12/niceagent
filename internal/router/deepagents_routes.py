"""DeepAgents 技能、Workspace 和 Memory 资源路由注册。"""

from flask import Blueprint

from internal.handler import MemoryFileHandler, SkillHandler, WorkspaceFileHandler


def register_deepagents_routes(
    blueprint: Blueprint,
    *,
    skill_handler: SkillHandler,
    workspace_file_handler: WorkspaceFileHandler,
    memory_file_handler: MemoryFileHandler,
) -> None:
    """注册创建者与 WebApp 的 DeepAgents 资源路由。"""
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/memory/entries", methods=["GET"],
        view_func=memory_file_handler.list_app_memory_entries,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/memory/entries", methods=["POST"],
        view_func=memory_file_handler.change_app_memory_entry,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/memory/entries", methods=["GET"],
        view_func=memory_file_handler.list_web_app_memory_entries,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/memory/entries", methods=["POST"],
        view_func=memory_file_handler.change_web_app_memory_entry,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/skills",
        methods=["GET"],
        view_func=skill_handler.list_skills,
    )
    blueprint.add_url_rule(
        "/skills", methods=["GET"], view_func=skill_handler.list_catalog
    )
    blueprint.add_url_rule(
        "/skills/<string:skill_name>",
        methods=["GET"],
        view_func=skill_handler.get_catalog_skill,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/workspace/files",
        methods=["GET"],
        view_func=workspace_file_handler.list_app_files,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/workspace/files",
        methods=["POST"],
        view_func=workspace_file_handler.write_app_file,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/workspace/file",
        methods=["GET"],
        view_func=workspace_file_handler.read_app_file,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/workspace/file",
        methods=["DELETE"],
        view_func=workspace_file_handler.delete_app_file,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/workspace/file/download",
        methods=["GET"],
        view_func=workspace_file_handler.download_app_file,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/memory/files",
        methods=["GET"],
        view_func=memory_file_handler.list_app_memory_files,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/memory/file",
        methods=["GET"],
        view_func=memory_file_handler.read_app_memory_file,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/memory/file/download",
        methods=["GET"],
        view_func=memory_file_handler.download_app_memory_file,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/memory/file",
        methods=["POST"],
        view_func=memory_file_handler.write_app_memory_file,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/memory/file",
        methods=["DELETE"],
        view_func=memory_file_handler.delete_app_memory_file,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/workspace/files",
        methods=["GET"],
        view_func=workspace_file_handler.list_all_web_app_files,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/workspace/file",
        methods=["GET"],
        view_func=workspace_file_handler.read_web_app_file,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/workspace/file/download",
        methods=["GET"],
        view_func=workspace_file_handler.download_web_app_file,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/conversations/<uuid:conversation_id>/file",
        methods=["GET"],
        view_func=workspace_file_handler.read_conversation_file,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/conversations/<uuid:conversation_id>/file/download",
        methods=["GET"],
        view_func=workspace_file_handler.download_conversation_file,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/memory/files",
        methods=["GET"],
        view_func=memory_file_handler.list_web_app_memory_files,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/memory/file",
        methods=["GET"],
        view_func=memory_file_handler.read_web_app_memory_file,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/memory/file/download",
        methods=["GET"],
        view_func=memory_file_handler.download_web_app_memory_file,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/memory/file",
        methods=["POST"],
        view_func=memory_file_handler.write_web_app_memory_file,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/memory/file",
        methods=["DELETE"],
        view_func=memory_file_handler.delete_web_app_memory_file,
    )


__all__ = ["register_deepagents_routes"]
