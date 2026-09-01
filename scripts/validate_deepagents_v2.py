"""Read-only production readiness checks for DeepAgents V2."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import stat
import sys
from typing import Callable

from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.config import Config
from internal.service.skill_service import SkillService

EXPECTED_HEAD = "d_v2_008_agent_trace_fields"


@dataclass(frozen=True)
class ValidationCheck:
    """One sanitized readiness result."""

    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        """Serialize one check for CLI JSON output."""
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
        }


def check_migration_graph() -> ValidationCheck:
    """Validate the real Alembic revision map and expected single head."""
    try:
        script = ScriptDirectory(PROJECT_ROOT / "internal" / "migration")
        heads = script.get_heads()
        branch = script.get_revision("deepagents_v2")
    except Exception as exc:
        return ValidationCheck(
            "migration_graph",
            "fail",
            f"Alembic graph failed to load ({type(exc).__name__})",
        )
    if heads != [EXPECTED_HEAD] or branch is None:
        return ValidationCheck(
            "migration_graph",
            "fail",
            f"Expected one head {EXPECTED_HEAD}; found {heads}",
        )
    return ValidationCheck(
        "migration_graph",
        "pass",
        f"Single Alembic head: {EXPECTED_HEAD}",
    )


def check_workspace_config(*, production: bool) -> ValidationCheck:
    """Validate positive limits and explicit production configuration."""
    if production:
        missing = [
            key
            for key in ("WORKSPACE_MAX_BYTES", "WORKSPACE_MAX_FILES")
            if os.getenv(key) is None
        ]
        if missing:
            return ValidationCheck(
                "workspace_config",
                "fail",
                f"Missing explicit production settings: {', '.join(missing)}",
            )
    try:
        config = Config()
    except Exception as exc:
        return ValidationCheck(
            "workspace_config",
            "fail",
            f"Workspace config invalid ({type(exc).__name__})",
        )
    return ValidationCheck(
        "workspace_config",
        "pass",
        (
            f"max_bytes={config.WORKSPACE_MAX_BYTES}, "
            f"max_files={config.WORKSPACE_MAX_FILES}"
        ),
    )


def check_builtin_skills(*, production: bool = False) -> ValidationCheck:
    """Validate the configured builtin Skill directory and package presence."""
    try:
        config = Config()
    except Exception as exc:
        return ValidationCheck(
            "builtin_skills",
            "fail",
            f"Builtin Skill config invalid ({type(exc).__name__})",
        )
    root = Path(str(config.BUILTIN_SKILLS_ROOT))
    if production and not root.is_absolute():
        return ValidationCheck(
            "builtin_skills",
            "fail",
            "Production builtin Skill root must be absolute",
        )
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    resolved = root.resolve()
    if not resolved.is_dir():
        return ValidationCheck(
            "builtin_skills",
            "fail",
            "Configured builtin Skill root is not a directory",
        )
    if production and resolved.stat().st_mode & (
        stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    ):
        return ValidationCheck(
            "builtin_skills",
            "fail",
            "Production builtin Skill root must be mounted read-only",
        )
    try:
        catalog = SkillService().list_catalog()
    except Exception as exc:
        return ValidationCheck(
            "builtin_skills",
            "fail",
            f"Builtin Skill catalog validation failed ({type(exc).__name__})",
        )
    if not catalog:
        return ValidationCheck(
            "builtin_skills",
            "fail",
            "Builtin Skill root contains no SKILL.md packages",
        )
    return ValidationCheck(
        "builtin_skills",
        "pass",
        f"Validated {len(catalog)} builtin Skill package(s)",
    )


def check_sandbox_provider(*, required: bool) -> ValidationCheck:
    """Validate the configured Sandbox provider without creating one."""
    provider = os.getenv("AGENT_SANDBOX_PROVIDER", "disabled").lower()
    if provider in {"disabled", "none", "off", "false"}:
        return ValidationCheck(
            "sandbox_provider",
            "fail" if required else "skip",
            "AGENT_SANDBOX_PROVIDER is disabled",
        )
    if provider in {"bubblewrap", "bwrap"}:
        if not shutil.which("bwrap"):
            return ValidationCheck(
                "sandbox_provider",
                "fail",
                "Bubblewrap provider requires the bwrap executable",
            )
        sandbox = None
        try:
            from internal.core.agent_v2.backends.bubblewrap_sandbox_backend import (
                BubblewrapSandbox,
            )

            sandbox = BubblewrapSandbox(timeout=10, max_output_bytes=4096)
            result = sandbox.execute("printf sandbox-smoke-ok", timeout=10)
            if result.exit_code != 0 or "sandbox-smoke-ok" not in result.output:
                return ValidationCheck(
                    "sandbox_provider",
                    "fail",
                    "Bubblewrap namespace smoke check failed",
                )
        except Exception as exc:
            return ValidationCheck(
                "sandbox_provider",
                "fail",
                f"Bubblewrap namespace smoke check failed ({type(exc).__name__})",
            )
        finally:
            if sandbox is not None:
                sandbox.close()
        return ValidationCheck(
            "sandbox_provider",
            "pass",
            "Bubblewrap executable and namespace smoke check passed",
        )
    if provider == "daytona":
        try:
            from daytona import Daytona, DaytonaConfig
            from langchain_daytona import DaytonaSandbox
        except ImportError:
            return ValidationCheck(
                "sandbox_provider",
                "fail" if required else "skip",
                "Daytona Sandbox dependencies are not installed",
            )
        del Daytona, DaytonaConfig, DaytonaSandbox
        if required and not os.getenv("DAYTONA_API_KEY"):
            return ValidationCheck(
                "sandbox_provider",
                "fail",
                "DAYTONA_API_KEY is required for Daytona Sandbox",
            )
        return ValidationCheck(
            "sandbox_provider",
            "pass",
            "Daytona Sandbox dependencies are available",
        )
    if provider != "langsmith":
        return ValidationCheck(
            "sandbox_provider",
            "fail",
            "Unsupported AGENT_SANDBOX_PROVIDER",
        )
    try:
        from deepagents.backends import LangSmithSandbox
        from langsmith.sandbox import SandboxClient
    except ImportError:
        return ValidationCheck(
            "sandbox_provider",
            "fail" if required else "skip",
            "LangSmith Sandbox dependencies are not installed",
        )
    del LangSmithSandbox, SandboxClient
    if required and not os.getenv("LANGSMITH_API_KEY"):
        return ValidationCheck(
            "sandbox_provider",
            "fail",
            "LANGSMITH_API_KEY is required for LangSmith Sandbox",
        )
    return ValidationCheck(
        "sandbox_provider",
        "pass",
        "LangSmith Sandbox dependencies are available",
    )


def check_database_schema(
    database_url: str | None,
    *,
    required: bool,
) -> ValidationCheck:
    """Inspect deployed PostgreSQL tables, columns, indexes, and revision."""
    if not database_url:
        return ValidationCheck(
            "postgres_schema",
            "fail" if required else "skip",
            "Database validation was not explicitly enabled",
        )
    engine = None
    try:
        engine = create_engine(
            database_url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5},
        )
        if engine.dialect.name != "postgresql":
            return ValidationCheck(
                "postgres_schema",
                "fail",
                "Production schema validation requires PostgreSQL",
            )
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            inspector = inspect(connection)
            _require_columns(
                inspector,
                "workspace_files",
                {
                    "account_id",
                    "app_id",
                    "conversation_id",
                    "path",
                    "version",
                    "size_bytes",
                    "content_sha256",
                },
            )
            _require_columns(
                inspector,
                "message_agent_thought",
                {
                    "agent_id",
                    "agent_type",
                    "parent_agent_id",
                    "delegation_id",
                    "task_id",
                    "todos",
                },
            )
            versions = set(
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalars()
            )
            if versions != {EXPECTED_HEAD}:
                raise RuntimeError("deployed Alembic head does not match")
    except Exception as exc:
        return ValidationCheck(
            "postgres_schema",
            "fail",
            f"Read-only schema validation failed ({type(exc).__name__})",
        )
    finally:
        if engine is not None:
            engine.dispose()
    return ValidationCheck(
        "postgres_schema",
        "pass",
        f"Required schema is present at {EXPECTED_HEAD}",
    )


def _require_columns(inspector, table: str, required: set[str]) -> None:
    """Raise when a required table or column is absent."""
    if not inspector.has_table(table):
        raise RuntimeError(f"required table missing: {table}")
    available = {column["name"] for column in inspector.get_columns(table)}
    missing = required - available
    if missing:
        raise RuntimeError(f"required columns missing from {table}")


def run_checks(
    *,
    production: bool = False,
    database_url: str | None = None,
    require_database: bool = False,
    require_sandbox: bool = False,
) -> list[ValidationCheck]:
    """Run readiness checks without changing database or filesystem state."""
    checks: list[Callable[[], ValidationCheck]] = [
        check_migration_graph,
        lambda: check_workspace_config(production=production),
        lambda: check_builtin_skills(production=production),
        lambda: check_database_schema(
            database_url,
            required=require_database,
        ),
        lambda: check_sandbox_provider(required=require_sandbox),
    ]
    return [check() for check in checks]


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--use-env-database", action="store_true")
    parser.add_argument("--require-database", action="store_true")
    parser.add_argument("--require-sandbox", action="store_true")
    args = parser.parse_args(argv)
    database_url = (
        os.getenv("SQLALCHEMY_DATABASE_URI")
        if args.use_env_database
        else None
    )
    results = run_checks(
        production=args.production,
        database_url=database_url,
        require_database=args.require_database,
        require_sandbox=args.require_sandbox,
    )
    print(json.dumps([result.to_dict() for result in results], indent=2))
    return int(any(result.status == "fail" for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
