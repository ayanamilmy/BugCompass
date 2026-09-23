"""统计上报：**默认完全关闭**，且默认不联网。

设计原则（对应"默认本地保存，统计上报仅在用户主动选择后启用"）：

1. 默认关闭。``telemetry_enabled`` 只有用户主动打开才会变成 True。
2. 即使打开，也只写本地 outbox（``~/.bugcompass/telemetry-outbox/``），
   **本模块内不存在任何网络请求代码**。
3. 真正外发必须由用户再执行一次显式命令（``bugcompass telemetry send``），
   并且只有配置了 endpoint 才会发送；没有 endpoint 就等于永远不出本机。
4. 事件体只含计数（模型、耗时、工具轮次、token、成本），
   不含问题描述、证据、源码路径、文件内容或任何可识别到项目正文的数据。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .resources import user_root
from .settings import get as get_setting


def outbox_dir() -> Path:
    return user_root() / "telemetry-outbox"


def is_enabled() -> bool:
    return bool(get_setting("telemetry_enabled", False))


def set_enabled(enabled: bool) -> bool:
    from .settings import set_value

    set_value("telemetry_enabled", bool(enabled))
    return bool(enabled)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def record(event: dict[str, Any]) -> Path | None:
    """记录一条统计事件。未启用时直接返回 None（连目录都不会创建）。"""
    if not is_enabled():
        return None
    directory = outbox_dir()
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "recorded_at": _now(),
        "app_version": __version__,
        "event": event,
    }
    name = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{os.getpid()}.json"
    target = directory / name
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(directory), suffix=".tmp")
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(handle.name, target)
    finally:
        if os.path.exists(handle.name):  # pragma: no cover
            os.unlink(handle.name)
    return target


def record_run_metrics(
    *,
    case_id: str,
    model: str,
    duration_seconds: float,
    turns: int,
    tool_calls: int,
    input_tokens: int,
    output_tokens: int,
    estimated_cost_usd: float | None,
    status: str,
) -> Path | None:
    """只上报聚合计数，绝不包含案件正文。"""
    return record(
        {
            "type": "codex_run",
            "case_id": case_id,
            "model": model,
            "duration_seconds": round(float(duration_seconds or 0.0), 3),
            "turns": int(turns),
            "tool_calls": int(tool_calls),
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "estimated_cost_usd": estimated_cost_usd,
            "status": status,
        }
    )


def pending() -> list[Path]:
    directory = outbox_dir()
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"))


def export_pending(dest_dir: str | Path) -> Path | None:
    """把待发事件打包成一个 JSON 文件（供用户自行检查/外发）。"""
    events: list[dict[str, Any]] = []
    for path in pending():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            events.append(data)
    if not events:
        return None
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f"bugcompass-telemetry-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    target.write_text(json.dumps({"events": events}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def clear_pending() -> int:
    count = 0
    for path in pending():
        try:
            path.unlink()
            count += 1
        except OSError:  # pragma: no cover
            continue
    return count
