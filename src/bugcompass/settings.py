"""本地设置（用户目录下，纯本地 JSON，不上传）。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .resources import settings_path

SETTINGS_SCHEMA_VERSION = 1

DEFAULTS: dict[str, Any] = {
    "schema_version": SETTINGS_SCHEMA_VERSION,
    "ui_scale_percent": 100,
    # 统计上报默认关闭；只有用户主动打开才会写本地 outbox。
    "telemetry_enabled": False,
    "telemetry_endpoint": "",
    "last_workspace": "",
}


def load_settings() -> dict[str, Any]:
    data = dict(DEFAULTS)
    path = settings_path()
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update(raw)
        except (OSError, json.JSONDecodeError):
            return dict(DEFAULTS)
    data["schema_version"] = SETTINGS_SCHEMA_VERSION
    return data


def save_settings(data: dict[str, Any]) -> dict[str, Any]:
    payload = dict(DEFAULTS)
    payload.update(data)
    payload["schema_version"] = SETTINGS_SCHEMA_VERSION
    _atomic_write_json(settings_path(), payload)
    return payload


def get(key: str, default: Any = None) -> Any:
    return load_settings().get(key, default)


def set_value(key: str, value: Any) -> dict[str, Any]:
    data = load_settings()
    data[key] = value
    return save_settings(data)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """先写临时文件再替换，避免崩溃时留下半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=str(path.parent), prefix=".tmp-", suffix=".json"
    )
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    finally:
        if os.path.exists(handle.name):  # pragma: no cover - 仅在 replace 失败时
            os.unlink(handle.name)
