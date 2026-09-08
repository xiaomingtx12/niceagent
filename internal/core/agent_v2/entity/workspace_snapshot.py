"""Immutable file versions shared by sandbox initialization and writeback."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkspaceFileVersion:
    """Pair a content digest with the database revision that produced it."""

    revision: int
    content_sha256: str


WorkspaceManifest = dict[str, WorkspaceFileVersion]
