"""GUI 基础控件：跨平台的滚动容器、滚轮路由、缩放与自适应换行。

Windows 上 Tk 的常见毛病与本模块的对策：

* **滚轮滚不动页面**：Tk 只把 ``<MouseWheel>`` 发给焦点控件，鼠标停在子控件上时
  事件不会到达外层 Canvas。这里用「指针命中测试 + 向上查找可滚动容器」统一路由。
* **拖动滚动条/滚动时拖影**：嵌入式子窗口落在非整数像素上，Windows 会留下残影。
  这里把 ``yscrollincrement`` 固定为 1（按整像素滚动），并在内容变化后立刻同步
  ``scrollregion``，避免出现半屏空白或被裁掉的一截（断层）。
* **长文本被裁切**：Tk 的 ``wraplength`` 是写死的像素值，窗口变窄或 DPI 变大时文字
  会溢出卡片。这里登记所有需要换行的标签，在每次尺寸变化时重算换行宽度。
* **按钮行被窗口边缘裁掉**：Tk 没有流式布局，一行放不下时右侧控件会被直接切掉
  （表现为按钮文字只剩一半）。:class:`FlowRow` 按可用宽度自动折行。
* **指标格子被挤窄**：grid 装不下时会把每一列一起压缩，格子里的数值被裁成半截
  （``491,312（缓存 386,9``）。:class:`FlowGrid` 宽度不够时减少列数，而不是压窄每列。
"""

from __future__ import annotations

from typing import Any, Callable

# 自身就能滚动、应当优先使用原生行为的控件类型名。
_NATIVE_SCROLLERS = ("Text", "Listbox", "Treeview", "TCombobox", "Spinbox", "Entry", "TEntry")

# 按自身宽度换行时留出的余量：分到的宽度里含边框与内边距，文字区要窄一点，
# 贴着整宽换行会让每行末尾的字符被切掉半个。
WRAP_MARGIN = 6


class _CanvasWheelAdapter:
    """自管画布（如思维导图）的滚动代理。

    macOS 上 Tk 把滚轮事件投递给焦点控件而非指针下的控件，画布自身的
    bind 经常收不到事件；全局路由器（bind_all）反而一定能收到。注册本
    适配器后，路由器把滚轮转发给画布控制器（Linux/Windows 上画布自身
    绑定先触发并 "break"，不会重复滚动）。
    """

    def __init__(self, canvas: Any, controller: Any) -> None:
        self.canvas = canvas
        self.controller = controller  # 需要 _pan(dx, dy) 与 zoom_at(x, y, factor)

    def scroll_pixels(self, pixels: int) -> None:
        if pixels:
            self.controller._pan(0.0, pixels / 2.0)

    def scroll_units(self, units: int) -> None:
        self.scroll_pixels(units * 20)

    def scroll_horizontal(self, pixels: int) -> None:
        if pixels:
            self.controller._pan(pixels / 2.0, 0.0)

    def handle_zoom_wheel(self, delta: int, event: Any = None) -> None:
        try:
            x = max(0, self.canvas.winfo_pointerx() - self.canvas.winfo_rootx())
            y = max(0, self.canvas.winfo_pointery() - self.canvas.winfo_rooty())
        except Exception:  # pragma: no cover - 画布已销毁
            x = y = 0
        self.controller.zoom_at(x, y, 1.1 if delta > 0 else 1 / 1.1)


class MouseWheelRouter:
    """把滚轮事件路由到指针下方的可滚动容器。

    直接在根窗口上 ``bind_all``，再用 ``winfo_containing`` 做命中测试，
    这样无论鼠标停在容器里的哪个子控件上都能滚动，也不会影响 Text/Listbox 自身的滚动。
    """

    def __init__(self, root: Any) -> None:
        self.root = root
        self._targets: dict[str, "ScrollableFrame"] = {}
        self._canvas_adapters: dict[str, _CanvasWheelAdapter] = {}
        root.bind_all("<MouseWheel>", self._on_mouse_wheel, add="+")
        root.bind_all("<Button-4>", self._on_button_4, add="+")
        root.bind_all("<Button-5>", self._on_button_5, add="+")

    def register(self, frame: "ScrollableFrame") -> None:
        self._targets[str(frame.canvas)] = frame

    def register_canvas(self, canvas: Any, controller: Any) -> None:
        """注册自管滚轮的画布（macOS 焦点投递问题由路由器转发兜底）。"""
        self._canvas_adapters[str(canvas)] = _CanvasWheelAdapter(canvas, controller)

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
            target.handle_zoom_wheel(delta, event)
            return "break"
        if bool(event.state & 0x0001) and hasattr(target, "scroll_horizontal"):
            target.scroll_horizontal(-delta)  # Shift：横向平移（画布）
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
                return self._canvas_adapters.get(str(widget))  # 自管画布：路由器转发（macOS 兜底）
            key = str(widget)
            if key in self._targets:
                return self._targets[key]
            parent = widget.winfo_parent()
            widget = None if not parent else self.root.nametowidget(parent)
        return None


def wrap_to_self(label: Any) -> Any:
    """让标签按**自己的实际宽度**换行。

    与 :meth:`ScrollableFrame.wrap_here` 的区别：那个是从容器宽度减去估算的留白，
    适合 ``anchor="w"``（宽度由内容决定）的标签；而这个适合 ``fill="x"`` /
    ``sticky="ew"``（宽度由布局决定）的标签——直接读自己的宽度最准，
    不用猜留白，也不会因为估多了而被裁切。

    记住「上次套用的宽度」而不是回头读 ``cget("wraplength")``：ttk 控件没设过的
    像素选项读出来是空字符串，转 int 会抛异常，换行就永远不会生效。
    """

    applied = {"width": -1}

    def _on_configure(event: Any) -> None:
        # 留一点余量：控件分到的宽度包含边框和内边距，文字区比它窄。
        # 按整宽换行的话，每行最后一个字会正好压在边上被切掉半个。
        target = max(80, event.width - WRAP_MARGIN)
        if target == applied["width"]:
            return
        applied["width"] = target
        try:
            label.configure(wraplength=target)
        except Exception:  # pragma: no cover - 控件已销毁
            pass

    label.bind("<Configure>", _on_configure)
    return label


def flow_rows(widths: list[int], available: int, gap: int = 10) -> list[list[int]]:
    """把一串控件宽度折成若干行，返回每行放哪些控件的下标。

    纯函数（不碰 Tk），便于单测。之所以不能简单地「一行累加超了就往下一行」：
    grid 的列宽是**所有行共用**的，某一行第 2 个控件比较宽，会把第 2 列撑大，
    于是别的行的第 3 个控件也会被推到更右边——只看单行累加会算漏，最后一列
    控件就越过了容器右边缘（Windows 上表现为按钮被切掉一半）。
    所以先贪心分行，再按真实列宽回头校验，超宽的行把末尾控件挤到下一行，直到稳定。
    """

    if not widths or available <= 0:
        return [[index] for index in range(len(widths))]

    rows: list[list[int]] = [[]]
    for index in range(len(widths)):
        rows[-1].append(index)
        if len(rows[-1]) > 1 and _flow_row_overflowing(rows, widths, available, gap):
            rows[-1].pop()
            rows.append([index])

    # 回头校验：贪心时后面的行还没出现，列宽可能被后来的控件撑大。
    while True:
        overflowing = next(
            (r for r, items in enumerate(rows) if len(items) > 1 and _flow_row_overflowing(rows, widths, available, gap, only=r)),
            None,
        )
        if overflowing is None:
            return rows
        moved = rows[overflowing].pop()
        if overflowing + 1 < len(rows):
            rows[overflowing + 1].insert(0, moved)
        else:
            rows.append([moved])


def _flow_row_overflowing(
    rows: list[list[int]], widths: list[int], available: int, gap: int, *, only: int | None = None
) -> bool:
    """按「列宽取各行最大值」算出每行真实宽度，判断是否有行超出可用宽度。"""

    columns = max((len(row) for row in rows), default=0)
    column_width = [0] * columns
    for row in rows:
        for column, index in enumerate(row):
            column_width[column] = max(column_width[column], widths[index])
    targets = range(len(rows)) if only is None else [only]
    for row_index in targets:
        row = rows[row_index]
        total = sum(column_width[: len(row)]) + gap * (len(row) - 1)
        if total > available:
            return True
    return False


class FlowRow:
    """按可用宽度自动折行的控件行。

    Tk 的 pack/grid 都不会折行：一行放不下时，右边的控件会被父容器直接裁掉，
    Windows 上表现为按钮文字只剩半截。这里在 ``<Configure>`` 里按每个控件的
    诉求宽度重新分行——宽度够就一行放完，不够就换行，一个控件都不会丢。
    """

    def __init__(self, parent: Any, ttk_module: Any, *, gap: int = 10, row_gap: int = 8) -> None:
        self.frame = ttk_module.Frame(parent, style="App.TFrame")
        self.gap = gap
        self.row_gap = row_gap
        self._items: list[Any] = []
        self._width = 0
        self.frame.bind("<Configure>", self._on_configure)

    def add(self, widget: Any) -> Any:
        """把一个控件交给本行管理，返回它本身便于链式使用。"""
        self._items.append(widget)
        widget.grid(row=0, column=len(self._items) - 1, sticky="w")
        return widget

    def _on_configure(self, event: Any) -> None:
        if event.width == self._width:
            return
        self._width = event.width
        self.relayout()

    def relayout(self) -> None:
        """按当前宽度重新分行（宽度还没量到就先不分行，保持初始顺序）。"""
        if not self._items or self._width <= 0:
            return
        self.frame.update_idletasks()
        widths = [widget.winfo_reqwidth() for widget in self._items]
        for row_index, row in enumerate(flow_rows(widths, self._width, self.gap)):
            for column, index in enumerate(row):
                self._items[index].grid(
                    row=row_index,
                    column=column,
                    sticky="w",
                    padx=(0 if column == 0 else self.gap, 0),
                    pady=(0 if row_index == 0 else self.row_gap, 0),
                )


def _still_exists(widget: Any) -> bool:
    """控件是否还在（已销毁的控件不能再设置属性）。"""
    try:
        return bool(widget.winfo_exists())
    except Exception:  # pragma: no cover - 控件已销毁
        return False


def grid_columns(widths: list[int], available: int, gap: int = 8, max_columns: int | None = None) -> int:
    """等宽网格一行摆几列：按最宽的格子算，宁可少一列也不把格子挤窄。

    纯函数，便于单测。Tk 的 grid 在容器装不下时会**压缩每一列**，格子里的文字
    就被裁掉（Windows 上表现为「491,312（缓存 386,9」）。所以宽度不够时要减列，
    而不是让每列一起变窄。
    """

    if not widths or available <= 0:
        return 1
    widest = max(widths) + gap  # 每列还要留出格子之间的间距
    limit = len(widths) if max_columns is None else min(max_columns, len(widths))
    columns = 1
    while columns < limit and (columns + 1) * widest <= available:
        columns += 1
    return columns


class FlowGrid:
    """列数随宽度变化的等宽网格（宽度不够就少摆一列）。

    用来摆一排等价的「格子」——运行指标那样的小卡片。Tk 的 grid 只会把列压窄，
    格子里的文字就没了；这里在 ``<Configure>`` 里重算列数并重新 grid。
    """

    def __init__(
        self,
        parent: Any,
        ttk_module: Any,
        *,
        gap: int = 8,
        row_gap: int = 6,
        max_columns: int | None = None,
        style: str = "Card.TFrame",
    ) -> None:
        self.frame = ttk_module.Frame(parent, style=style)
        self.gap = gap
        self.row_gap = row_gap
        self.max_columns = max_columns
        self._items: list[Any] = []
        self._width = 0
        self.frame.bind("<Configure>", self._on_configure)

    def add(self, widget: Any) -> Any:
        """把一个格子交给本网格管理，返回它本身便于链式使用。"""
        self._items.append(widget)
        widget.grid(row=0, column=len(self._items) - 1, sticky="ew")
        return widget

    def _on_configure(self, event: Any) -> None:
        if event.width == self._width:
            return
        self._width = event.width
        self.relayout()

    def relayout(self) -> None:
        """按当前宽度重新排列（宽度还没量到就先不动，保持初始顺序）。"""
        if not self._items or self._width <= 0:
            return
        self.frame.update_idletasks()
        widths = [widget.winfo_reqwidth() for widget in self._items]
        columns = grid_columns(widths, self._width, self.gap, self.max_columns)
        for index, widget in enumerate(self._items):
            column = index % columns
            widget.grid(
                row=index // columns,
                column=column,
                sticky="ew",
                padx=(0 if column == 0 else self.gap, 0),
                pady=(0 if index < columns else self.row_gap, 0),
            )
        # 上一次用过、这次用不到的列要复位，否则残留的 weight 会继续撑开旧列。
        for column in range(self.frame.grid_size()[0]):
            if column < columns:
                self.frame.columnconfigure(column, weight=1, uniform="flowgrid")
            else:
                self.frame.columnconfigure(column, weight=0, uniform="")


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
        # 已经套用过的宽度。0 表示还没量过——此时登记的标签先不设换行宽度。
        self._applied_width = 0

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
        # 只注销「住在内容帧里」的标签——它们已经随卡片一起销毁了。
        # 登记表里还有挂在滚动区域**外面**的标签（例如概览卡片的正文：它是画布的
        # 兄弟节点，清空内容并不会让它消失）。一并清掉的话，重渲染一次它就彻底
        # 失联，换行宽度永远停在上一次的数值上，窗口一变宽就被裁掉。
        self._wrapped = [
            (label, padding) for label, padding in self._wrapped
            if _still_exists(label) and not self._inside_content(label)
        ]
        self.canvas.yview_moveto(0.0)
        self.refresh()

    def _inside_content(self, widget: Any) -> bool:
        """控件是否住在内容帧里（内容帧里的控件会随 ``clear`` 一起销毁）。"""
        try:
            path = str(widget)
            content = str(self.content)
        except Exception:  # pragma: no cover - 控件已销毁
            return True
        return path == content or path.startswith(content + ".")

    def wrap_here(self, label: Any, padding: int = 56) -> Any:
        """登记一个需要随宽度自动换行的标签。

        ``padding`` 是标签到滚动区域两侧的大致留白（含卡片内边距）。
        登记时立刻套用已知宽度：否则要等下一次 refresh，期间标签的 wraplength 是 0
        （Tk 里 0 等于「不换行」），长文本会直接顶破卡片。
        """
        self._wrapped.append((label, padding))
        if self._applied_width:
            self._set_wraplength(label, padding, self._applied_width)
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

    def handle_zoom_wheel(self, delta: int, event: Any = None) -> None:
        if self._zoom_callback is None:
            return
        step = 10 if delta > 0 else -10
        self._zoom_callback(step)

    def set_zoom_callback(self, callback: Callable[[int], None] | None) -> None:
        self._zoom_callback = callback

    # -- 尺寸同步 ---------------------------------------------------------
    def _set_wraplength(self, label: Any, padding: int, width: int) -> None:
        try:
            if label.winfo_exists():
                label.configure(wraplength=max(80, width - padding))
        except Exception:  # pragma: no cover
            pass

    def _apply_width(self, width: int) -> None:
        """把当前宽度套到内容帧和所有登记过的换行标签上。

        宽度没变就直接返回：调用方之一是 ``<Configure>`` 回调，而改换行宽度本身
        会让内容重新排版、可能又触发滚动条显隐（进而改变画布宽度），
        加这道闸门可以避免两边互相触发。
        """
        width = max(1, width)
        if width == self._applied_width:
            return
        self._applied_width = width
        self.canvas.itemconfigure(self._window_id, width=width)
        for label, padding in self._wrapped:
            self._set_wraplength(label, padding, width)

    def refresh(self) -> None:
        """同步内容宽度、换行宽度与 scrollregion。"""
        self.canvas.update_idletasks()
        self._apply_width(self.canvas.winfo_width())
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
        # 这里也必须重算换行宽度：窗口从「还没排版」变成有真实宽度时只发这一次
        # Configure，漏掉它，文字就会一直按量到的那个小宽度换行（Windows 上尤其明显）。
        self._apply_width(event.width)
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


def fit_dialog(dialog: Any, min_width: int, min_height: int) -> None:
    """对话框自动适应内容：内容多时放大到装得下，超出屏幕则封顶。

    所有 Toplevel 对话框禁止写死 geometry，统一走这里（AGENTS.md 规则）。
    """
    dialog.update_idletasks()
    width = max(min_width, dialog.winfo_reqwidth())
    height = min(max(min_height, dialog.winfo_reqheight() + 12), dialog.winfo_screenheight() - 80)
    dialog.geometry(f"{width}x{height}")
    dialog.minsize(min_width, min_height)
