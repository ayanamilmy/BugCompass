"""Windows 高 DPI 支持。

背景：Windows 上的 Tk 进程默认不是 DPI aware，系统会对整个窗口做位图拉伸。
在 150% / 200% 缩放下这既会发虚，也会在滚动、拖动时留下残影（拖影）和错位（断层）。
必须在创建 Tk 根窗口**之前**调用 :func:`enable_windows_dpi_awareness`。

默认使用「系统级 DPI aware」：在 100/150/200% 下都由 Tk 自己按真实 DPI 绘制，
不再被拉伸。跨不同 DPI 显示器拖窗口时 Tk 8.6 不会重新布局（需要重启应用），
这是为了稳定性做的取舍；如果确认 Tk >= 8.6.12 且接受该风险，
可设置环境变量 ``BUGCOMPASS_DPI_MODE=permonitor`` 启用 per-monitor v2。
"""

from __future__ import annotations

import os
import sys
from typing import Any

#: 失败时也不允许 GUI 起不来：所有异常都会被吞掉并记录到返回信息里。
DPI_MODE_SYSTEM = "system"
DPI_MODE_PER_MONITOR = "permonitor"
DPI_MODE_NONE = "unavailable"


def enable_windows_dpi_awareness() -> str:
    """把当前进程声明为 DPI aware。必须在创建 Tk 根窗口前调用。

    返回实际生效的模式，便于写进诊断日志。非 Windows 平台返回 ``unavailable``。
    """
    if sys.platform != "win32":
        return DPI_MODE_NONE

    preferred = (os.environ.get("BUGCOMPASS_DPI_MODE") or DPI_MODE_SYSTEM).strip().lower()
    try:
        import ctypes

        if preferred == DPI_MODE_PER_MONITOR:
            # Windows 10 1703+：DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4
            if ctypes.windll.user32.SetProcessDpiAwarenessContext(-4):  # type: ignore[attr-defined]
                return DPI_MODE_PER_MONITOR
            # Win8.1+：PROCESS_PER_MONITOR_DPI_AWARE == 2
            if _set_process_dpi_awareness(ctypes, 2):
                return DPI_MODE_PER_MONITOR
        if _set_process_dpi_awareness(ctypes, 1):  # PROCESS_SYSTEM_DPI_AWARE
            return DPI_MODE_SYSTEM
        if ctypes.windll.user32.SetProcessDPIAware():  # type: ignore[attr-defined]
            return DPI_MODE_SYSTEM
    except (OSError, AttributeError, ValueError):
        return DPI_MODE_NONE
    return DPI_MODE_NONE


def _set_process_dpi_awareness(ctypes: Any, value: int) -> bool:
    try:
        shcore = ctypes.windll.shcore  # type: ignore[attr-defined]
    except AttributeError:
        return False
    try:
        return bool(shcore.SetProcessDpiAwareness(value) == 0)
    except (OSError, AttributeError, ValueError):
        return False


def system_dpi(root: Any) -> float:
    """返回当前 Tk 认为的每英寸像素数（96 表示 100% 缩放）。"""
    try:
        value = float(root.tk.call("tk", "scaling"))
    except Exception:  # pragma: no cover - 只在 Tk 异常时触发
        return 96.0
    # Tk 的 scaling 是「每点多少像素」，1 点 = 1/72 英寸。
    return value * 72.0


def scale_percent(root: Any) -> int:
    return int(round(system_dpi(root) / 96.0 * 100))


def apply_scaling(root: Any, extra: float = 1.0) -> float:
    """把 Tk 的缩放系数同步到真实 DPI（乘以用户的额外缩放 ``extra``）。

    返回最终使用的 scaling 值。
    """
    if extra <= 0:
        extra = 1.0
    try:
        base_dpi = float(root.winfo_fpixels("1i"))
    except Exception:  # pragma: no cover
        base_dpi = 96.0
    if base_dpi <= 0:
        base_dpi = 96.0
    scaling = max(0.5, min(6.0, base_dpi / 72.0 * extra))
    try:
        root.tk.call("tk", "scaling", scaling)
    except Exception:  # pragma: no cover
        pass
    return scaling


def describe(root: Any) -> dict[str, Any]:
    """诊断信息：缩放百分比、Tk 版本、DPI aware 模式。"""
    return {
        "tk_scaling": round(_safe_scaling(root), 3),
        "scale_percent": scale_percent(root),
        "tk_version": str(getattr(root, "tk", None) and root.tk.eval("info patchlevel")),
    }


def _safe_scaling(root: Any) -> float:
    try:
        return float(root.tk.call("tk", "scaling"))
    except Exception:  # pragma: no cover
        return -1.0
