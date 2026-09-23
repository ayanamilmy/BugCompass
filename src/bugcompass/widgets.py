"""GUI 基础控件：跨平台的滚动容器、滚轮路由、缩放与自适应换行。

Windows 上 Tk 的常见毛病与本模块的对策：

* **滚轮滚不动页面**：Tk 只把 ``<MouseWheel>`` 发给焦点控件，鼠标停在子控件上时
  事件不会到达外层 Canvas。这里用「指针命中测试 + 向上查找可滚动容器」统一路由。
* **拖动滚动条/滚动时拖影**：嵌入式子窗口落在非整数像素上，Windows 会留下残影。
  这里把 ``yscrollincrement`` 固定为 1（按整像素滚动），并在内容变化后立刻同步
  ``scrollregion``，避免出现半屏空白或被裁掉的一截（断层）。
* **长文本被裁切**：Tk 的 ``wraplength`` 是写死的像素值，窗口变窄或 DPI 变大时文字
  会溢出卡片。这里登记所有需要换行的标签，在每次尺寸变化时重算换行宽度。
"""

from __future__ import annotations

from typing import Any, Callable

# 自身就能滚动、应当优先使用原生行为的控件类型名。
_NATIVE_SCROLLERS = ("Text", "Listbox", "Treeview", "TCombobox", "Spinbox", "Entry", "TEntry")


class MouseWheelRouter:
    """把滚轮事件路由到指针下方的可滚动容器。

    直接在根窗口上 ``bind_all``，再用 ``winfo_containing`` 做命中测试，
    这样无论鼠标停在容器里的哪个子控件上都能滚动，也不会影响 Text/Listbox 自身的滚动。
    """

    def __init__(self, root: Any) -> None:
        self.root = root
        self._targets: dict[str, "ScrollableFrame"] = {}
        root.bind_all("<MouseWheel>", self._on_mouse_wheel, add="+")
        root.bind_all("<Button-4>", self._on_button_4, add="+")
        root.bind_all("<Button-5>", self._on_button_5, add="+")

    def register(self, frame: "ScrollableFrame") -> None:
        self._targets[str(frame.canvas)] = frame

    def unregister(self, frame: "ScrollableFrame") -> None:
        self._targets.pop(str(frame.canvas), None)

    # -- 事件处理 ---------------------------------------------------------
    def _on_mouse_wheel(self, event: Any) -> str | None:
        delta = int(getattr(event, "delta", 0) or 0)
        if delta == 0:
            return None
        target = self._target_under_pointer()
        if target is None:
            return None
        if bool(event.state & 0x0004):  # Ctrl：缩放
            target.handle_zoom_wheel(delta)
            return "break"
        target.scroll_pixels(-delta)
        return "break"

    def _on_button_4(self, event: Any) -> str | None:
        return self._scroll_by_button(event, -1)

    def _on_button_5(self, event: Any) -> str | None:
        return self._scroll_by_button(event, 1)

    def _scroll_by_button(self, event: Any, direction: int) -> str | None:
        target = self._target_under_pointer()
        if target is None:
            return None
        target.scroll_units(direction * 3)
        return "break"

    def _target_under_pointer(self) -> "ScrollableFrame | None":
        try:
            x = self.root.winfo_pointerx()
            y = self.root.winfo_pointery()
            widget = self.root.winfo_containing(x, y)
        except Exception:  # pragma: no cover - 窗口已销毁
            return None
        while widget is not None:
            widget_class = widget.winfo_class()
            if widget_class in _NATIVE_SCROLLERS:
                return None  # 让原生滚动生效（滚到尽头时 Tk 会自然冒泡）
            if widget_class == "Canvas" and getattr(widget, "_bugcompass_wheel_native", False):
                return None  # 自己处理滚轮的画布（如思维导图画布）
            key = str(widget)
            if key in self._targets:
                return self._targets[key]
            parent = widget.winfo_parent()
            widget = None if not parent else self.root.nametowidget(parent)
        return None


class ScrollableFrame:
    """带垂直滚动条的容器。

    用法：把内容放进 ``frame.content``，内容变化后调用 :meth:`refresh`。
    """

    WHEEL_STEP = 60  # 一个滚轮档位滚动的像素数（120 为 Windows 标准 delta）

    def __init__(
        self,
        parent: Any,
        tk_module: Any,
        ttk_module: Any,
        router: MouseWheelRouter,
        *,
        background: str,
        content_style: str = "App.TFrame",
    ) -> None:
        self.tk = tk_module
        self.ttk = ttk_module
        self.router = router
        self._wrapped: list[tuple[Any, int]] = []
        self._zoom_callback: Callable[[int], None] | None = None
        self._show_scrollbar = True

        self.canvas = tk_module.Canvas(
            parent,
            highlightthickness=0,
            background=background,
            borderwidth=0,
            # 整像素滚动：嵌入式子窗口不会落在半个像素上（Windows 拖影的主因）。
            yscrollincrement=1,
            xscrollincrement=1,
            takefocus=False,
        )
        self.scrollbar = ttk_module.Scrollbar(parent, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll)

        self.content = ttk_module.Frame(self.canvas, style=content_style)
        self._window_id = self.canvas.create_window((0, 0), window=self.content, anchor="nw")

        self.content.bind("<Configure>", self._on_content_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Destroy>", self._on_destroy)
        router.register(self)

    # -- 布局 -------------------------------------------------------------
    def grid(self, **kwargs: Any) -> None:
        row = kwargs.pop("row", 0)
        column = kwargs.pop("column", 0)
        self.canvas.grid(row=row, column=column, sticky=kwargs.pop("sticky", "nsew"), **kwargs)
        self.scrollbar.grid(row=row, column=column + 1, sticky="ns")

    # -- 内容管理 ---------------------------------------------------------
    def clear(self) -> None:
        """销毁全部子控件并复位。重渲染前必须调用，避免旧卡片残留造成拖影。"""
        for child in list(self.content.winfo_children()):
            child.destroy()
        self._wrapped.clear()
        self.canvas.yview_moveto(0.0)
        self.refresh()

    def wrap_here(self, label: Any, padding: int = 56) -> Any:
        """登记一个需要随宽度自动换行的标签。

        ``padding`` 是标签到滚动区域两侧的大致留白（含卡片内边距）。
        """
        self._wrapped.append((label, padding))
        return label

    # -- 滚动 -------------------------------------------------------------
    def scroll_pixels(self, pixels: int) -> None:
        if pixels == 0:
            return
        self.canvas.yview_scroll(int(pixels), "units")

    def scroll_units(self, units: int) -> None:
        self.scroll_pixels(units * 20)

    def scroll_to_top(self) -> None:
        self.canvas.yview_moveto(0.0)

    def handle_zoom_wheel(self, delta: int) -> None:
        if self._zoom_callback is None:
            return
        step = 10 if delta > 0 else -10
        self._zoom_callback(step)

    def set_zoom_callback(self, callback: Callable[[int], None] | None) -> None:
        self._zoom_callback = callback

    # -- 尺寸同步 ---------------------------------------------------------
    def refresh(self) -> None:
        """同步内容宽度、换行宽度与 scrollregion。"""
        self.canvas.update_idletasks()
        width = max(1, self.canvas.winfo_width())
        self.canvas.itemconfigure(self._window_id, width=width)
        for label, padding in self._wrapped:
            try:
                if label.winfo_exists():
                    label.configure(wraplength=max(80, width - padding))
            except Exception:  # pragma: no cover
                continue
        self.canvas.update_idletasks()
        self._update_scrollregion()
        self._update_scrollbar_visibility()

    def _update_scrollregion(self) -> None:
        bbox = self.canvas.bbox("all")
        if bbox is None:
            self.canvas.configure(scrollregion=(0, 0, 1, 1))
            return
        # 未取整的 scrollregion 会让最后一行内容被裁掉一截。
        x0, y0, x1, y1 = (int(round(value)) for value in bbox)
        self.canvas.configure(scrollregion=(x0, y0, x1, y1))

    def _update_scrollbar_visibility(self) -> None:
        bbox = self.canvas.bbox("all")
        content_height = 0 if bbox is None else int(bbox[3]) - int(bbox[1])
        viewport_height = self.canvas.winfo_height()
        needed = content_height > viewport_height + 1
        if needed != self._show_scrollbar:
            self._show_scrollbar = needed
            if needed:
                self.scrollbar.grid()
            else:
                self.scrollbar.grid_remove()
                self.canvas.yview_moveto(0.0)

    # -- 回调 -------------------------------------------------------------
    def _on_content_configure(self, _event: Any) -> None:
        self._update_scrollregion()
        self._update_scrollbar_visibility()

    def _on_canvas_configure(self, event: Any) -> None:
        self.canvas.itemconfigure(self._window_id, width=max(1, event.width))
        self._update_scrollregion()

    def _on_scroll(self, first: float, last: float) -> None:
        self.scrollbar.set(first, last)

    def _on_destroy(self, _event: Any) -> None:
        try:
            self.router.unregister(self)
        except Exception:  # pragma: no cover
            pass


class UiScale:
    """界面缩放：调整 Tk 字体并回调所有需要重排的地方。

    Tk 没有「整体缩放」的原生能力，可行做法是缩放命名字体 + 重算换行宽度。
    """

    MIN = 80
    MAX = 250

    def __init__(self, root: Any, tk_module: Any) -> None:
        self.root = root
        self.tk = tk_module
        self.percent = 100
        self._fonts: dict[str, tuple[str, int, tuple[str, ...]]] = {}
        self._callbacks: list[Callable[[int], None]] = []

    def register_font(self, name: str, family: str, size: int, *extra: str) -> None:
        self._fonts[name] = (family, size, extra)
        self._apply_font(name)

    def register_callback(self, callback: Callable[[int], None]) -> None:
        self._callbacks.append(callback)

    @property
    def factor(self) -> float:
        return self.percent / 100.0

    def set_percent(self, percent: int) -> int:
        percent = max(self.MIN, min(self.MAX, int(percent)))
        if percent == self.percent:
            return self.percent
        self.percent = percent
        for name in self._fonts:
            self._apply_font(name)
        for callback in self._callbacks:
            callback(percent)
        return self.percent

    def step(self, delta: int) -> int:
        return self.set_percent(self.percent + delta)

    def _apply_font(self, name: str) -> None:
        family, size, extra = self._fonts[name]
        scaled = max(6, int(round(size * self.factor)))
        try:
            self.root.option_add(f"*{name}", (family, scaled, *extra))
        except Exception:  # pragma: no cover
            pass
