from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server import FastMCP

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8765
DEMO_TOOL_NAMES = [
    "ping",
    "add_numbers",
    "echo_text",
    "inspect_json",
    "list_project_files",
    "read_project_file",
    "wait_seconds",
]


def _resolve_project_path(relative_path: str) -> Path:
    candidate = (PROJECT_ROOT / relative_path).resolve()
    try:
        candidate.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ValueError("only project-relative paths are allowed") from exc
    return candidate


def _build_server(host: str, port: int) -> FastMCP:
    server = FastMCP(
        name="my-dify-local-demo",
        instructions=(
            "A local MCP demo server for testing my-dify MCP integration. "
            "It exposes simple text, math, file, and delay tools."
        ),
        host=host,
        port=port,
    )

    @server.tool(
        name="ping",
        description="Return a small structured response to verify the MCP link is working.",
        structured_output=True,
    )
    def ping(name: str = "world") -> dict[str, Any]:
        return {
            "message": f"pong, {name}",
            "server": "my-dify-local-demo",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }

    @server.tool(
        name="add_numbers",
        description="Add two numbers and return the calculation result.",
        structured_output=True,
    )
    def add_numbers(a: float, b: float) -> dict[str, Any]:
        return {
            "a": a,
            "b": b,
            "operation": "add",
            "result": a + b,
        }

    @server.tool(
        name="echo_text",
        description="Echo text back with optional uppercase conversion for parameter tests.",
        structured_output=True,
    )
    def echo_text(text: str, uppercase: bool = False, repeat: int = 1) -> dict[str, Any]:
        repeat = max(1, min(repeat, 5))
        output = text.upper() if uppercase else text
        return {
            "input": text,
            "uppercase": uppercase,
            "repeat": repeat,
            "output": " ".join([output] * repeat),
        }

    @server.tool(
        name="inspect_json",
        description="Parse a JSON string and return summary info about its structure.",
        structured_output=True,
    )
    def inspect_json(payload: str) -> dict[str, Any]:
        data = json.loads(payload)
        if isinstance(data, dict):
            preview = list(data.keys())[:10]
            data_type = "object"
            size = len(data)
        elif isinstance(data, list):
            preview = data[:5]
            data_type = "array"
            size = len(data)
        else:
            preview = data
            data_type = type(data).__name__
            size = 1

        return {
            "type": data_type,
            "size": size,
            "preview": preview,
        }

    @server.tool(
        name="list_project_files",
        description="List files under a project-relative directory for local file access tests.",
        structured_output=True,
    )
    def list_project_files(relative_dir: str = "docs", limit: int = 20) -> dict[str, Any]:
        root = _resolve_project_path(relative_dir)
        if not root.exists():
            raise ValueError(f"path does not exist: {relative_dir}")
        if not root.is_dir():
            raise ValueError(f"path is not a directory: {relative_dir}")

        limit = max(1, min(limit, 50))
        files = sorted(
            str(path.relative_to(PROJECT_ROOT))
            for path in root.rglob("*")
            if path.is_file()
        )[:limit]
        return {
            "relative_dir": relative_dir,
            "count": len(files),
            "files": files,
        }

    @server.tool(
        name="read_project_file",
        description="Read a UTF-8 text file inside the project with a size limit.",
        structured_output=True,
    )
    def read_project_file(relative_path: str, max_chars: int = 2000) -> dict[str, Any]:
        file_path = _resolve_project_path(relative_path)
        if not file_path.exists():
            raise ValueError(f"file does not exist: {relative_path}")
        if not file_path.is_file():
            raise ValueError(f"path is not a file: {relative_path}")

        max_chars = max(100, min(max_chars, 10000))
        content = file_path.read_text(encoding="utf-8")
        truncated = len(content) > max_chars
        return {
            "relative_path": relative_path,
            "chars": min(len(content), max_chars),
            "truncated": truncated,
            "content": content[:max_chars],
        }

    @server.tool(
        name="wait_seconds",
        description="Sleep for a few seconds to help test timeout handling and debug feedback.",
        structured_output=True,
    )
    def wait_seconds(seconds: int = 1) -> dict[str, Any]:
        seconds = max(0, min(seconds, 8))
        started_at = datetime.now().isoformat(timespec="seconds")
        time.sleep(seconds)
        return {
            "seconds": seconds,
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        }

    return server


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local FastMCP demo server for my-dify.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport used by the MCP server.",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HTTP_HOST,
        help="Host for streamable-http mode.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help="Port for streamable-http mode.",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Print demo tool names and exit without starting the server.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    server = _build_server(host=args.host, port=args.port)

    if args.list_tools:
        for tool_name in DEMO_TOOL_NAMES:
            print(tool_name)
        return

    print(
        f"Starting my-dify local demo MCP server with transport={args.transport}",
        flush=True,
    )
    if args.transport == "streamable-http":
        print(f"URL: http://{args.host}:{args.port}/mcp", flush=True)

    server.run(args.transport)


if __name__ == "__main__":
    main()
