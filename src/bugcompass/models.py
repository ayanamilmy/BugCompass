from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Project:
    schema_version: int
    project: str
    pack_version: str
    repo_path: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Workspace:
    path: Path
    project: Project

    @property
    def repo_path(self) -> Path:
        return Path(self.project.repo_path)
