"""Case 备份 / 恢复 / 迁移。

* 备份是单个 zip，带清单（每个文件的 sha256）与应用版本，便于校验完整性。
* 恢复时做 zip-slip 防护（拒绝绝对路径、``..`` 与盘符），并自动跑迁移。
* 迁移是幂等的：可以重复执行，版本已经是最新的就什么都不做。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .resources import backups_dir
from .workspace import BugCompassError

BACKUP_SCHEMA_VERSION = 2
PROJECT_SCHEMA_VERSION = 2
CASE_SCHEMA_VERSION = 2

#: 备份中排除的噪音/临时文件（不影响案件内容）。
EXCLUDE_NAMES = {".codex-run.lock", "investigation.next.json", ".DS_Store", "Thumbs.db"}
EXCLUDE_SUFFIXES = {".pyc", ".tmp", ".swp"}
EXCLUDE_DIRS = {"__pycache__", ".cache", "build", ".git", "node_modules"}

MANIFEST_NAME = "backup-manifest.json"


class BackupError(BugCompassError):
    """可安全展示给用户的备份/恢复错误。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _should_skip(path: Path, root: Path) -> bool:
    if path.name in EXCLUDE_NAMES or path.suffix in EXCLUDE_SUFFIXES:
        return True
    relative = path.relative_to(root)
    return any(part in EXCLUDE_DIRS for part in relative.parts)


def create_backup(workspace: str | Path, dest: str | Path | None = None, *, note: str = "") -> Path:
    """把整个工作区（含所有 Case）打包成一个 zip。"""
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise BackupError(f"工作区不存在：{root}")
    if not (root / "project.json").is_file():
        raise BackupError(f"不是有效的 BugCompass 工作区（缺少 project.json）：{root}")

    target = Path(dest).expanduser().resolve() if dest else backups_dir() / f"bugcompass-{root.name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)

    entries: list[dict[str, Any]] = []
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir() or _should_skip(path, root):
            continue
        if path.name == MANIFEST_NAME:
            continue
        files.append(path)

    manifest: dict[str, Any] = {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "app_version": __version__,
        "created_at": _now(),
        "workspace_name": root.name,
        "note": note,
        "file_count": len(files),
        "total_bytes": 0,
        "files": entries,
    }

    handle = tempfile.NamedTemporaryFile("wb", delete=False, dir=str(target.parent), suffix=".zip.tmp")
    handle.close()
    try:
        with zipfile.ZipFile(handle.name, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                relative = path.relative_to(root).as_posix()
                archive.write(path, relative)
                entries.append(
                    {
                        "path": relative,
                        "bytes": path.stat().st_size,
                        "sha256": _sha256(path),
                    }
                )
                manifest["total_bytes"] += path.stat().st_size
            archive.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        os.replace(handle.name, target)
    finally:
        if os.path.exists(handle.name):  # pragma: no cover
            os.unlink(handle.name)
    return target


def inspect_backup(archive: str | Path) -> dict[str, Any]:
    path = Path(archive).expanduser()
    if not path.is_file():
        raise BackupError(f"备份文件不存在：{path}")
    with zipfile.ZipFile(path) as zf:
        if MANIFEST_NAME not in zf.namelist():
            raise BackupError("备份缺少 backup-manifest.json，可能不是 BugCompass 备份。")
        try:
            manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BackupError(f"备份清单无法解析：{exc}") from exc
    if not isinstance(manifest, dict):
        raise BackupError("备份清单格式无效。")
    return manifest


def _safe_target(target_root: Path, name: str) -> Path:
    """zip-slip 防护：只允许写到 target_root 内部。"""
    if not name or name.startswith(("/", "\\")) or ":" in name.split("/")[0]:
        raise BackupError(f"备份包含非法路径：{name!r}")
    destination = (target_root / name).resolve()
    root = target_root.resolve()
    if destination != root and root not in destination.parents:
        raise BackupError(f"备份包含越界路径：{name!r}")
    return destination


def restore_backup(archive: str | Path, target_dir: str | Path, *, overwrite: bool = False) -> Path:
    """把备份恢复到 ``target_dir``（内部目录名为备份时的 workspace_name）。"""
    manifest = inspect_backup(archive)
    version = int(manifest.get("schema_version", 0))
    if version > BACKUP_SCHEMA_VERSION:
        raise BackupError(
            f"备份由更新版本的 BugCompass 创建（schema {version} > {BACKUP_SCHEMA_VERSION}），请先升级。"
        )

    root = Path(target_dir).expanduser().resolve()
    name = str(manifest.get("workspace_name") or "restored-workspace")
    destination = (root / name).resolve()
    if root not in destination.parents and destination != root:
        raise BackupError(f"非法的恢复目标：{name}")
    if destination.exists() and any(destination.iterdir()) and not overwrite:
        raise BackupError(f"目标目录已存在且不为空：{destination}（如需覆盖请显式允许）")
    destination.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(Path(archive).expanduser()) as zf:
        for member in zf.namelist():
            if member == MANIFEST_NAME:
                continue
            out = _safe_target(destination, member)
            if member.endswith("/"):
                out.mkdir(parents=True, exist_ok=True)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as source, out.open("wb") as sink:
                shutil.copyfileobj(source, sink)

    applied = migrate_workspace(destination)
    manifest["restored_at"] = _now()
    manifest["migrations_applied"] = applied
    (destination / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destination


# --------------------------------------------------------------------- 迁移
def migrate_workspace(workspace: str | Path) -> list[str]:
    """把工作区与其中所有 Case 迁移到当前 schema。返回应用的迁移名列表。"""
    root = Path(workspace).expanduser().resolve()
    applied: list[str] = []

    project_path = root / "project.json"
    if project_path.is_file():
        data = _read_json(project_path)
        if isinstance(data, dict):
            current = int(data.get("schema_version", 1) or 1)
            if current < PROJECT_SCHEMA_VERSION:
                data["schema_version"] = PROJECT_SCHEMA_VERSION
                data.setdefault("backup_schema", BACKUP_SCHEMA_VERSION)
                data.setdefault("migrated_at", _now())
                _write_json(project_path, data)
                applied.append(f"project:{current}->{PROJECT_SCHEMA_VERSION}")

    cases_dir = root / "cases"
    if cases_dir.is_dir():
        for case_dir in sorted(cases_dir.iterdir()):
            if not case_dir.is_dir():
                continue
            case_path = case_dir / "case.json"
            if not case_path.is_file():
                continue
            data = _read_json(case_path)
            if not isinstance(data, dict):
                continue
            current = int(data.get("schema_version", 1) or 1)
            if current < CASE_SCHEMA_VERSION:
                data["schema_version"] = CASE_SCHEMA_VERSION
                data.setdefault("metrics_file", "metrics.json")
                _write_json(case_path, data)
                applied.append(f"case:{case_dir.name}:{current}->{CASE_SCHEMA_VERSION}")
    return applied


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent), suffix=".tmp")
    try:
        with handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(handle.name, path)
    finally:
        if os.path.exists(handle.name):  # pragma: no cover
            os.unlink(handle.name)
