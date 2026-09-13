"""按需加载持久化文件，并显式交付本轮工作副本中的结果。"""

from hashlib import sha256
from pathlib import PurePosixPath
from threading import RLock

from deepagents.backends.protocol import BackendProtocol
from langchain_core.tools import BaseTool, StructuredTool, ToolException

from internal.core.agent_v2.entity.workspace_path import AgentWorkspacePathPolicy
from internal.core.agent_v2.persistence.workspace_store import WorkspaceDeliveryError, WorkspaceStore


class WorkspaceDelivery:
    """持久化入口与普通文件工具分离，保存版本只来自真实加载或成功交付。"""

    def __init__(
        self, *, backend: BackendProtocol, store: WorkspaceStore, paths: AgentWorkspacePathPolicy,
    ) -> None:
        """绑定本轮工作副本和已授权存储，锁保护重复加载与交付版本。"""
        self._backend = backend
        self._store = store
        self._paths = paths
        self._versions: dict[tuple[str, str], int] = {}
        self._loaded: dict[str, str] = {}
        self._lock = RLock()

    def tools(self) -> list[BaseTool]:
        """交付工具采用独立名称，不替换框架文件工具；异步调用由原生工具线程适配。"""
        return [
            StructuredTool.from_function(function, name=name, handle_tool_error=True)
            for name, function in (
                ("list_saved_files", self.list_saved_files),
                ("load_saved_file", self.load_saved_file),
                ("deliver_file", self.deliver_file),
            )
        ]

    def list_saved_files(self, prefix: str = "/workspace") -> dict:
        """查看当前账号和应用已保存的文件目录；只查询元数据，不加载文件正文。"""
        try:
            self._paths.resolve_prefix(prefix)
            return {"files": self._store.list_files(prefix)}
        except (ValueError, WorkspaceDeliveryError) as exc:
            raise ToolException(str(exc)) from exc

    def load_saved_file(self, path: str, destination: str | None = None) -> dict:
        """把一个已保存文件加载到本轮 /workspace/ 副本。destination 默认同路径；不覆盖已有工作文件。"""
        with self._lock:
            try:
                source = self._file_path(path)
                target = self._file_path(destination or path)
                # 1.重复加载复用工作副本，绝不从 COS 覆盖模型已经编辑的内容。
                if self._exists(target):
                    if self._loaded.get(target) == source:
                        return {"path": target, "source": source, "status": "already_loaded"}
                    raise WorkspaceDeliveryError("工作副本中已存在同名文件，请使用不同的 destination")
                # 2.只下载选定文件，上传成功后才记录覆盖原文件所需的版本。
                content, revision = self._store.load_file(source)
                responses = self._backend.upload_files([(target, content)])
                if len(responses) != 1 or responses[0].error:
                    raise WorkspaceDeliveryError("文件写入本轮工作副本失败，请重试")
                # 版本绑定具体副本，加载到另一目录不能使旧副本获得覆盖新版本的权限。
                self._versions[(target, source)] = revision
                self._loaded[target] = source
                return {"path": target, "source": source, "revision": revision, "status": "loaded"}
            except (ValueError, WorkspaceDeliveryError) as exc:
                raise ToolException(str(exc)) from exc

    def deliver_file(self, file_path: str, destination: str | None = None) -> dict:
        """把本轮文件保存为可下载的交付结果。仅在用户要求保存或交付时调用；默认同路径，覆盖目标必须先加载其版本。"""
        with self._lock:
            try:
                source = self._file_path(file_path)
                target = self._file_path(destination or file_path)
                # 1.只读取明确指定的产物，不扫描目录或推断哪些文件应持久化。
                responses = self._backend.download_files([source])
                if len(responses) != 1 or responses[0].error or responses[0].content is None:
                    raise WorkspaceDeliveryError("交付文件不存在或无法读取，请先在本轮工作副本中生成文件")
                content = responses[0].content
                # 2.未加载的目标仅允许新增；已加载目标使用其真实版本防止并发覆盖。
                revision = self._store.save_file(
                    target, content, expected_revision=self._versions.get((source, target), 0),
                )
                self._versions[(source, target)] = revision
                return {
                    "path": target, "revision": revision, "size": len(content),
                    "sha256": sha256(content).hexdigest(), "status": "saved",
                    "message": "文件已保存，可通过平台文件预览和下载入口访问",
                }
            except (ValueError, WorkspaceDeliveryError) as exc:
                raise ToolException(str(exc)) from exc

    def _file_path(self, path: str) -> str:
        """交付只能访问授权 Workspace，不能借自定义工具绕过 Skill/Memory 路由权限。"""
        storage_path = self._paths.resolve_file(path)
        return self._paths.virtual_path(storage_path)

    def _exists(self, path: str) -> bool:
        """只查询目标目录元数据，检查加载不会覆盖本轮已有工作文件。"""
        result = self._backend.ls(str(PurePosixPath(path).parent))
        if result.error:
            if result.error.endswith(": path_not_found"):
                return False
            raise WorkspaceDeliveryError("工作副本目录读取失败，请重试")
        return any(item["path"].rstrip("/") == path for item in result.entries or [])
