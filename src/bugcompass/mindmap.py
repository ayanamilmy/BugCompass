"""思维导图式因果链画布。

替代原来「每次鼠标移动都 delete("all") 重画」的实现——那是 Windows 上拖动时
拖影（残影/闪烁）的直接来源。这里：

* 拖动节点时只更新该节点和相邻连线的坐标，不整体重画；
* 所有运动会合并到一次 ``after_idle`` 重绘，避免消息队列被塞爆；
* 坐标一律取整，连线端点贴到节点边框上（不会从卡片中间穿过去造成断层）；
* 支持平移、缩放、框选、就地改名、撤销/重做、自动布局、网格吸附。

世界坐标 <-> 屏幕坐标：``screen = world * zoom + offset``。
对外只暴露 :meth:`get_graph` / :meth:`set_graph`，节点 ``x``/``y`` 始终为 int。
"""

from __future__ import annotations

import copy
from typing import Any, Callable

NODE_MIN_WIDTH = 150
NODE_MAX_WIDTH = 190
NODE_PADDING = 12
LINE_HEIGHT = 14
KIND_LABEL_HEIGHT = 12
GRID = 8
ZOOM_MIN, ZOOM_MAX = 0.4, 2.5

KIND_NAMES = {
    "trigger": "触发",
    "decision": "分支",
    "state": "状态",
    "failure": "故障",
    "fix": "修复",
    "unknown": "未知",
}
CERTAINTY_DASH = {"fact": (), "inference": (5, 4), "unknown": (2, 5)}


class MindMapCanvas:
    def __init__(
        self,
        parent: Any,
        tk_module: Any,
        *,
        colors: dict[str, str],
        height: int = 360,
        on_change: Callable[[dict[str, Any]], None] | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self.tk = tk_module
        self.colors = colors
        self.on_change = on_change
        self.on_status = on_status

        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.zoom = 1.0
        self.offset = (0.0, 0.0)
        self.snap = True
        self.selected_nodes: set[str] = set()
        self.selected_edge: str | None = None

        self._items: dict[str, dict[str, int]] = {}
        self._edge_items: dict[str, int] = {}
        self._drag: dict[str, Any] | None = None
        self._marquee: int | None = None
        self._editor: Any = None
        self._editor_node: str | None = None
        self._redo_stack: list[dict[str, Any]] = []
        self._undo_stack: list[dict[str, Any]] = []
        self._flush_scheduled = False

        self.canvas = tk_module.Canvas(
            parent,
            height=height,
            background=colors["surface_alt"],
            highlightthickness=1,
            highlightbackground=colors["border"],
            takefocus=True,
        )
        # 滚轮由画布自己处理（平移/缩放），不要被外层滚动容器截走。
        self.canvas._bugcompass_wheel_native = True  # type: ignore[attr-defined]

        self._bind()
        self._measure_font = self._make_measure_font()

    # ------------------------------------------------------------------ 绑定
    def _bind(self) -> None:
        widget = self.canvas
        widget.bind("<ButtonPress-1>", self._on_press, add="+")
        widget.bind("<B1-Motion>", self._on_motion, add="+")
        widget.bind("<ButtonRelease-1>", self._on_release, add="+")
        widget.bind("<ButtonPress-2>", self._on_pan_start, add="+")
        widget.bind("<B2-Motion>", self._on_pan_motion, add="+")
        widget.bind("<ButtonPress-3>", self._on_pan_start, add="+")
        widget.bind("<B3-Motion>", self._on_pan_motion, add="+")
        widget.bind("<ButtonRelease-2>", self._on_pan_end, add="+")
        widget.bind("<ButtonRelease-3>", self._on_pan_end, add="+")
        widget.bind("<MouseWheel>", self._on_wheel, add="+")
        widget.bind("<Button-4>", self._on_wheel_linux, add="+")
        widget.bind("<Button-5>", self._on_wheel_linux, add="+")
        widget.bind("<Double-Button-1>", self._on_double_click, add="+")
        widget.bind("<Delete>", self._on_delete_key, add="+")
        widget.bind("<BackSpace>", self._on_delete_key, add="+")
        widget.bind("<Configure>", self._on_configure, add="+")
        widget.bind("<Control-z>", self._on_undo, add="+")
        widget.bind("<Control-y>", self._on_redo, add="+")

    # ------------------------------------------------------------------ 数据
    def set_graph(self, graph: dict[str, Any]) -> None:
        nodes = graph.get("nodes") if isinstance(graph, dict) else None
        edges = graph.get("edges") if isinstance(graph, dict) else None
        self.nodes = {}
        for raw in nodes or []:
            if not isinstance(raw, dict):
                continue
            node_id = str(raw.get("id", "")).strip()
            if not node_id:
                continue
            self.nodes[node_id] = {
                "id": node_id,
                "label": str(raw.get("label", "")),
                "kind": raw.get("kind") if raw.get("kind") in KIND_NAMES else "unknown",
                "certainty": raw.get("certainty") if raw.get("certainty") in CERTAINTY_DASH else "unknown",
                "x": int(raw.get("x", 0) or 0),
                "y": int(raw.get("y", 0) or 0),
                "evidence_ids": list(raw.get("evidence_ids") or []),
                "user_edited": bool(raw.get("user_edited", False)),
                "user_created": bool(raw.get("user_created", False)),
            }
        self.edges = []
        for raw in edges or []:
            if not isinstance(raw, dict):
                continue
            source, target = str(raw.get("from", "")), str(raw.get("to", ""))
            if source not in self.nodes or target not in self.nodes:
                continue
            self.edges.append(
                {
                    "id": str(raw.get("id") or f"E{len(self.edges) + 1}"),
                    "from": source,
                    "to": target,
                    "label": str(raw.get("label", "")),
                    "certainty": raw.get("certainty") if raw.get("certainty") in CERTAINTY_DASH else "inference",
                    "user_created": bool(raw.get("user_created", False)),
                }
            )
        self.selected_nodes.clear()
        self.selected_edge = None
        if self.nodes:
            self.fit_to_content()
        else:
            self.redraw()

    def get_graph(self) -> dict[str, Any]:
        return {
            "nodes": [copy.deepcopy(node) for node in self.nodes.values()],
            "edges": [copy.deepcopy(edge) for edge in self.edges],
        }

    # ------------------------------------------------------------------ 几何
    def _make_measure_font(self) -> Any:
        try:  # pragma: no cover - 依赖 Tk
            from tkinter import font as tkfont

            return tkfont.Font(family="TkDefaultFont", size=10, weight="bold")
        except Exception:
            return None

    def node_size(self, node: dict[str, Any]) -> tuple[int, int]:
        """按文字长度算节点尺寸（世界坐标，整数）。"""
        label = node.get("label", "") or "未命名"
        max_text_width = NODE_MAX_WIDTH - 2 * NODE_PADDING
        lines: list[str] = []
        current = ""
        for word in self._split_units(label):
            candidate = current + word
            if self._text_width(candidate) <= max_text_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word.lstrip()
            if len(lines) >= 4:
                break
        if current:
            lines.append(current)
        if not lines:
            lines = [""]
        width = min(NODE_MAX_WIDTH, max(NODE_MIN_WIDTH, max(self._text_width(line) for line in lines) + 2 * NODE_PADDING))
        height = NODE_PADDING * 2 + KIND_LABEL_HEIGHT + len(lines) * LINE_HEIGHT
        return int(width), int(height)

    @staticmethod
    def _split_units(text: str) -> list[str]:
        """中英文混排切分：中文按字断行，西文按词断行。"""
        units: list[str] = []
        buffer = ""
        for char in text:
            if char.isspace():
                buffer += char
                units.append(buffer)
                buffer = ""
            elif ord(char) > 0x2E80:  # CJK
                if buffer:
                    units.append(buffer)
                    buffer = ""
                units.append(char)
            else:
                buffer += char
        if buffer:
            units.append(buffer)
        return units

    def _text_width(self, text: str) -> int:
        if self._measure_font is None:
            return len(text) * 7
        try:
            return int(self._measure_font.measure(text))
        except Exception:  # pragma: no cover
            return len(text) * 7

    def to_screen(self, x: float, y: float) -> tuple[float, float]:
        ox, oy = self.offset
        return x * self.zoom + ox, y * self.zoom + oy

    def to_world(self, sx: float, sy: float) -> tuple[float, float]:
        ox, oy = self.offset
        return (sx - ox) / self.zoom, (sy - oy) / self.zoom

    def _node_rect(self, node: dict[str, Any]) -> tuple[float, float, float, float]:
        width, height = self.node_size(node)
        x, y = int(node["x"]), int(node["y"])
        return float(x), float(y), float(x + width), float(y + height)

    @staticmethod
    def _center(rect: tuple[float, float, float, float]) -> tuple[float, float]:
        return (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2

    @staticmethod
    def _border_point(rect: tuple[float, float, float, float], toward: tuple[float, float]) -> tuple[float, float]:
        """从矩形中心朝 toward 方向走，返回与边框的交点（连线端点不会插进卡片里）。"""
        cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
        dx, dy = toward[0] - cx, toward[1] - cy
        if dx == 0 and dy == 0:
            return cx, cy
        half_w, half_h = (rect[2] - rect[0]) / 2, (rect[3] - rect[1]) / 2
        scale_x = half_w / abs(dx) if dx else float("inf")
        scale_y = half_h / abs(dy) if dy else float("inf")
        scale = min(scale_x, scale_y)
        return cx + dx * scale, cy + dy * scale

    def _edge_geometry(self, edge: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]] | None:
        source = self.nodes.get(edge.get("from"))
        target = self.nodes.get(edge.get("to"))
        if source is None or target is None:
            return None
        source_rect = self._node_rect(source)
        target_rect = self._node_rect(target)
        start = self._border_point(source_rect, self._center(target_rect))
        end = self._border_point(target_rect, self._center(source_rect))
        return start, end

    def _edge_curve(self, start: tuple[float, float], end: tuple[float, float]) -> list[float]:
        """三次贝塞尔：控制点沿水平/垂直方向外推，避免连线横穿其它卡片。"""
        sx, sy = self.to_screen(*start)
        ex, ey = self.to_screen(*end)
        dx, dy = ex - sx, ey - sy
        if abs(dx) >= abs(dy):
            bend = max(28.0, min(120.0, abs(dx) * 0.45))
            control = ((sx + bend if dx >= 0 else sx - bend), sy, (ex - bend if dx >= 0 else ex + bend), ey)
        else:
            bend = max(24.0, min(120.0, abs(dy) * 0.45))
            control = (sx, sy + bend if dy >= 0 else sy - bend, ex, ey - bend if dy >= 0 else ey + bend)
        return [sx, sy, *control, ex, ey]

    # ------------------------------------------------------------------ 绘制
    def _alive(self) -> bool:
        try:
            return bool(self.canvas.winfo_exists())
        except Exception:  # pragma: no cover - Tk 已销毁
            return False

    def redraw(self) -> None:
        if not self._alive():
            return
        self.canvas.delete("all")
        self._items.clear()
        self._edge_items.clear()
        for edge in self.edges:
            self._draw_edge(edge)
        for node in self.nodes.values():
            self._draw_node(node)
        if self._marquee is not None:
            self._marquee = None
        self._status()

    def _draw_edge(self, edge: dict[str, Any]) -> None:
        geometry = self._edge_geometry(edge)
        if geometry is None:
            return
        start, end = geometry
        selected = edge["id"] == self.selected_edge
        color = self.colors["orange"] if selected else self.colors["subtle"]
        item = self.canvas.create_line(
            *self._edge_curve(start, end),
            fill=color,
            width=3 if selected else 2,
            dash=CERTAINTY_DASH.get(edge.get("certainty"), CERTAINTY_DASH["inference"]),
            smooth=True,
            arrow="last",
            arrowshape=(8, 10, 4),
            tags=(f"edge:{edge['id']}", "mindmap-edge"),
        )
        self._edge_items[edge["id"]] = item

    def _draw_node(self, node: dict[str, Any]) -> None:
        left, top, right, bottom = self._node_rect(node)
        x0, y0 = self.to_screen(left, top)
        x1, y1 = self.to_screen(right, bottom)
        color = self._certainty_color(node.get("certainty"))
        tag = f"node:{node['id']}"
        rect = self.canvas.create_rectangle(
            x0, y0, x1, y1,
            fill=self.colors["surface"],
            outline=self.colors["orange"] if node["id"] in self.selected_nodes else color,
            width=3 if node["id"] in self.selected_nodes else 2,
            tags=(tag, "mindmap-node"),
        )
        kind = self.canvas.create_text(
            x0 + NODE_PADDING * self.zoom, y0 + 6 * self.zoom,
            text=KIND_NAMES.get(node.get("kind"), "节点"),
            anchor="nw",
            fill=color,
            font=(self.colors.get("font_family", "TkDefaultFont"), max(7, int(8 * self.zoom)), "bold"),
            tags=(tag, "mindmap-node"),
        )
        label = self.canvas.create_text(
            x0 + NODE_PADDING * self.zoom, y0 + (6 + KIND_LABEL_HEIGHT) * self.zoom,
            text=node.get("label", ""),
            anchor="nw",
            width=(right - left - 2 * NODE_PADDING) * self.zoom,
            fill=self.colors["text"],
            font=(self.colors.get("font_family", "TkDefaultFont"), max(8, int(10 * self.zoom)), "bold"),
            tags=(tag, "mindmap-node"),
        )
        port_x, port_y = x1, (y0 + y1) / 2
        port = self.canvas.create_oval(
            port_x - 5 * self.zoom, port_y - 5 * self.zoom,
            port_x + 5 * self.zoom, port_y + 5 * self.zoom,
            fill=self.colors["orange"], outline="",
            tags=(f"port:{node['id']}", "mindmap-port"),
        )
        self._items[node["id"]] = {"rect": rect, "kind": kind, "label": label, "port": port}
        self.canvas.tag_raise(rect)
        self.canvas.tag_raise(kind)
        self.canvas.tag_raise(label)
        self.canvas.tag_raise(port)

    def _certainty_color(self, certainty: str | None) -> str:
        return {
            "fact": self.colors["green"],
            "inference": self.colors["yellow"],
            "unknown": self.colors["muted"],
        }.get(certainty or "", self.colors["muted"])

    def _update_node(self, node_id: str) -> None:
        node = self.nodes.get(node_id)
        items = self._items.get(node_id)
        if node is None or items is None:
            return
        left, top, right, bottom = self._node_rect(node)
        x0, y0 = self.to_screen(left, top)
        x1, y1 = self.to_screen(right, bottom)
        self.canvas.coords(items["rect"], x0, y0, x1, y1)
        self.canvas.coords(items["kind"], x0 + NODE_PADDING * self.zoom, y0 + 6 * self.zoom)
        self.canvas.coords(items["label"], x0 + NODE_PADDING * self.zoom, y0 + (6 + KIND_LABEL_HEIGHT) * self.zoom)
        self.canvas.itemconfigure(items["label"], width=(right - left - 2 * NODE_PADDING) * self.zoom)
        self.canvas.coords(items["port"], x1 - 5 * self.zoom, y1 - (y1 - y0) / 2 - 5 * self.zoom, x1 + 5 * self.zoom, y1 - (y1 - y0) / 2 + 5 * self.zoom)

    def _update_edges_for(self, node_ids: set[str]) -> None:
        for edge in self.edges:
            if edge["from"] in node_ids or edge["to"] in node_ids:
                item = self._edge_items.get(edge["id"])
                geometry = self._edge_geometry(edge)
                if item is not None and geometry is not None:
                    self.canvas.coords(item, *self._edge_curve(*geometry))

    def _schedule_flush(self) -> None:
        """把连续的鼠标事件合并成一次重绘，避免消息队列积压造成的拖影。"""
        if self._flush_scheduled:
            return
        self._flush_scheduled = True
        self.canvas.after_idle(self._flush)

    def _flush(self) -> None:
        self._flush_scheduled = False
        if not self._alive():
            return
        drag = self._drag
        if not drag:
            return
        if drag["mode"] == "node":
            moved: set[str] = set()
            for node_id in drag["ids"]:
                self._update_node(node_id)
                moved.add(node_id)
            self._update_edges_for(moved)
        elif drag["mode"] == "edge":
            source = self.nodes.get(drag["source"])
            if source is not None:
                geometry = self._edge_geometry({"from": drag["source"], "to": drag["hover"] or drag["source"]})
                if geometry is not None and drag.get("item") is not None:
                    start = geometry[0]
                    sx, sy = self.to_screen(*start)
                    self.canvas.coords(drag["item"], sx, sy, drag["x"], drag["y"])
        elif drag["mode"] == "pan":
            self.offset = drag["offset"]
            self.redraw()
        elif drag["mode"] == "marquee" and self._marquee is not None:
            self.canvas.coords(self._marquee, drag["x0"], drag["y0"], drag["x"], drag["y"])

    # ------------------------------------------------------------------ 交互
    def _hit(self, event: Any) -> tuple[str | None, str | None]:
        """返回 (node_id, edge_id)。"""
        items = self.canvas.find_withtag("current")
        if not items:
            items = self.canvas.find_closest(event.x, event.y, halo=2)
        for item in reversed(items):
            for tag in self.canvas.gettags(item):
                if tag.startswith("port:"):
                    return tag[5:], None
                if tag.startswith("node:"):
                    return tag[5:], None
                if tag.startswith("edge:"):
                    return None, tag[5:]
        return None, None

    def _on_press(self, event: Any) -> None:
        self.canvas.focus_set()
        node_id, edge_id = self._hit(event)
        if node_id is not None:
            if event.state & 0x0001:  # Shift：多选
                self.selected_nodes.add(node_id)
            elif node_id not in self.selected_nodes:
                self.selected_nodes = {node_id}
            self.selected_edge = None
            self._push_undo()
            self._drag = {
                "mode": "node",
                "ids": set(self.selected_nodes),
                "origin": {nid: (self.nodes[nid]["x"], self.nodes[nid]["y"]) for nid in self.selected_nodes},
                "start": (event.x, event.y),
                "moved": False,
            }
            self.redraw()
            return
        if edge_id is not None:
            self.selected_edge = edge_id
            self.selected_nodes.clear()
            self.redraw()
            return
        self.selected_nodes.clear()
        self.selected_edge = None
        self._marquee = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline=self.colors["orange"], dash=(4, 3), width=1,
        )
        self._drag = {"mode": "marquee", "x0": event.x, "y0": event.y, "x": event.x, "y": event.y}
        self.redraw()

    def _on_motion(self, event: Any) -> None:
        drag = self._drag
        if drag is None:
            return
        if drag["mode"] == "node":
            dx = (event.x - drag["start"][0]) / self.zoom
            dy = (event.y - drag["start"][1]) / self.zoom
            for node_id, (origin_x, origin_y) in drag["origin"].items():
                node = self.nodes[node_id]
                new_x = int(round(origin_x + dx))
                new_y = int(round(origin_y + dy))
                if self.snap and not (event.state & 0x0008):  # Alt 临时关闭吸附
                    new_x = int(round(new_x / GRID) * GRID)
                    new_y = int(round(new_y / GRID) * GRID)
                node["x"], node["y"] = new_x, new_y
            drag["moved"] = True
        elif drag["mode"] == "marquee":
            drag["x"], drag["y"] = event.x, event.y
        self._schedule_flush()

    def _on_release(self, event: Any) -> None:
        drag = self._drag
        self._drag = None
        if drag is None:
            return
        if drag["mode"] == "node":
            if drag.get("moved"):
                for node_id in drag["ids"]:
                    self.nodes[node_id]["user_edited"] = True
                self._notify()
            else:
                self._undo_stack.pop()  # 只是点选，没有实际移动
        elif drag["mode"] == "marquee":
            self._apply_marquee(drag)
        self.redraw()

    def _apply_marquee(self, drag: dict[str, Any]) -> None:
        if self._marquee is not None:
            self.canvas.delete(self._marquee)
            self._marquee = None
        x0, x1 = sorted((drag["x0"], drag["x"]))
        y0, y1 = sorted((drag["y0"], drag["y"]))
        if abs(x1 - x0) < 4 and abs(y1 - y0) < 4:
            return
        world_x0, world_y0 = self.to_world(x0, y0)
        world_x1, world_y1 = self.to_world(x1, y1)
        for node in self.nodes.values():
            left, top, right, bottom = self._node_rect(node)
            if right >= world_x0 and left <= world_x1 and bottom >= world_y0 and top <= world_y1:
                self.selected_nodes.add(node["id"])

    def _on_double_click(self, event: Any) -> None:
        node_id, _ = self._hit(event)
        if node_id is None:
            return
        self.start_rename(node_id)

    def start_rename(self, node_id: str) -> None:
        node = self.nodes.get(node_id)
        if node is None:
            return
        left, top, right, bottom = self._node_rect(node)
        x0, y0 = self.to_screen(left, top)
        x1, _ = self.to_screen(right, bottom)
        editor = self.tk.Entry(self.canvas, width=20)
        editor.insert(0, node.get("label", ""))
        editor.select_range(0, "end")
        window = self.canvas.create_window(x0, y0, window=editor, anchor="nw", width=max(60, x1 - x0))
        editor.focus_set()
        self._editor = (editor, window)
        self._editor_node = node_id
        editor.bind("<Return>", lambda _e: self._commit_rename())
        editor.bind("<Escape>", lambda _e: self._cancel_rename())
        editor.bind("<FocusOut>", lambda _e: self._commit_rename())

    def _commit_rename(self) -> None:
        if self._editor is None or self._editor_node is None:
            return
        editor, window = self._editor
        value = editor.get().strip()
        node_id, self._editor_node = self._editor_node, None
        self._editor = None
        self.canvas.delete(window)
        editor.destroy()
        node = self.nodes.get(node_id)
        if node is None or not value or value == node.get("label", ""):
            return
        self._push_undo()
        node["label"] = value
        node["user_edited"] = True
        self.redraw()
        self._notify()

    def _cancel_rename(self) -> None:
        if self._editor is None:
            return
        editor, window = self._editor
        self._editor = None
        self._editor_node = None
        self.canvas.delete(window)
        editor.destroy()

    def add_node(self, label: str) -> str | None:
        """外部接口：添加一个用户节点（自动避让 id 冲突）。"""
        label = label.strip()
        if not label:
            return None
        existing = set(self.nodes)
        index = len(self.nodes) + 1
        node_id = f"UN{index}"
        while node_id in existing:
            index += 1
            node_id = f"UN{index}"
        self._push_undo()
        self.nodes[node_id] = {
            "id": node_id,
            "label": label,
            "kind": "unknown",
            "certainty": "unknown",
            "evidence_ids": [],
            "x": int(round((40 + (index % 3) * 190) / GRID) * GRID),
            "y": int(round((40 + (index % 4) * 75) / GRID) * GRID),
            "user_edited": True,
            "user_created": True,
        }
        self.redraw()
        self._notify()
        return node_id

    def _on_delete_key(self, _event: Any) -> str:
        if self.selected_edge:
            self._push_undo()
            self.edges = [edge for edge in self.edges if edge["id"] != self.selected_edge]
            self.selected_edge = None
            self.redraw()
            self._notify()
            return "break"
        if self.selected_nodes:
            self._push_undo()
            self.edges = [e for e in self.edges if e["from"] not in self.selected_nodes and e["to"] not in self.selected_nodes]
            for node_id in self.selected_nodes:
                self.nodes.pop(node_id, None)
            self.selected_nodes.clear()
            self.redraw()
            self._notify()
            return "break"
        return None  # type: ignore[return-value]

    # 平移与缩放
    def _on_pan_start(self, event: Any) -> None:
        self._drag = {"mode": "pan", "start": (event.x, event.y), "offset": self.offset}

    def _on_pan_motion(self, event: Any) -> None:
        drag = self._drag
        if drag is None or drag["mode"] != "pan":
            return
        start_x, start_y = drag["start"]
        base_x, base_y = drag["offset"]
        drag["offset"] = (base_x + (event.x - start_x), base_y + (event.y - start_y))
        self._schedule_flush()

    def _on_pan_end(self, _event: Any) -> None:
        if self._drag and self._drag["mode"] == "pan":
            self._drag = None

    def _on_wheel(self, event: Any) -> str:
        delta = int(getattr(event, "delta", 0) or 0)
        if delta == 0:
            return "break"
        if event.state & 0x0004:  # Ctrl + 滚轮：缩放
            self.zoom_at(event.x, event.y, 1.1 if delta > 0 else 1 / 1.1)
        elif event.state & 0x0001:  # Shift + 滚轮：横向平移
            self._pan(-delta / 2.0, 0.0)
        else:
            self._pan(0.0, -delta / 2.0)
        return "break"

    def _on_wheel_linux(self, event: Any) -> str:
        step = 60.0
        if event.num == 4:
            self._pan(0.0, step)
        elif event.num == 5:
            self._pan(0.0, -step)
        return "break"

    def _pan(self, dx: float, dy: float) -> None:
        ox, oy = self.offset
        self.offset = (ox + dx, oy + dy)
        self.redraw()

    def zoom_at(self, screen_x: float, screen_y: float, factor: float) -> None:
        new_zoom = max(ZOOM_MIN, min(ZOOM_MAX, self.zoom * factor))
        if abs(new_zoom - self.zoom) < 1e-6:
            return
        world_x, world_y = self.to_world(screen_x, screen_y)
        self.zoom = new_zoom
        ox, oy = self.offset
        self.offset = (screen_x - world_x * new_zoom, screen_y - world_y * new_zoom)
        self.redraw()
        self._status()

    def zoom_by(self, factor: float) -> None:
        width = self.canvas.winfo_width() or 600
        height = self.canvas.winfo_height() or 360
        self.zoom_at(width / 2, height / 2, factor)

    def reset_view(self) -> None:
        self.zoom = 1.0
        self.offset = (0.0, 0.0)
        self.redraw()

    def fit_to_content(self) -> None:
        if not self.nodes:
            self.reset_view()
            return
        rects = [self._node_rect(node) for node in self.nodes.values()]
        min_x = min(rect[0] for rect in rects)
        min_y = min(rect[1] for rect in rects)
        max_x = max(rect[2] for rect in rects)
        max_y = max(rect[3] for rect in rects)
        width = self.canvas.winfo_width() or 720
        height = self.canvas.winfo_height() or self.canvas.winfo_reqheight() or 360
        padding = 16
        zoom_x = (width - 2 * padding) / max(1.0, max_x - min_x)
        zoom_y = (height - 2 * padding) / max(1.0, max_y - min_y)
        self.zoom = max(ZOOM_MIN, min(1.0, min(zoom_x, zoom_y)))
        self.offset = (padding - min_x * self.zoom, padding - min_y * self.zoom)
        self.redraw()

    # 自动布局（分层：按入度分层，层内按重心排序）
    def auto_layout(self) -> None:
        if not self.nodes:
            return
        self._push_undo()
        indegree = {node_id: 0 for node_id in self.nodes}
        adjacency: dict[str, list[str]] = {node_id: [] for node_id in self.nodes}
        for edge in self.edges:
            if edge["from"] in indegree and edge["to"] in indegree:
                indegree[edge["to"]] += 1
                adjacency[edge["from"]].append(edge["to"])
        layer: dict[str, int] = {}
        current = [node_id for node_id, degree in indegree.items() if degree == 0]
        if not current:  # 有环：随便挑一个起点
            current = [next(iter(self.nodes))]
        depth = 0
        visited: set[str] = set()
        while current and depth < len(self.nodes) + 1:
            next_layer: list[str] = []
            for node_id in current:
                if node_id in visited:
                    continue
                visited.add(node_id)
                layer.setdefault(node_id, depth)
                for child in adjacency[node_id]:
                    if child not in layer:
                        next_layer.append(child)
            current = next_layer
            depth += 1
        for node_id in self.nodes:
            layer.setdefault(node_id, depth)

        columns: dict[int, list[str]] = {}
        for node_id, depth_value in layer.items():
            columns.setdefault(depth_value, []).append(node_id)
        for depth_value in sorted(columns):
            columns[depth_value].sort(key=lambda nid: (self.nodes[nid]["y"], self.nodes[nid]["x"]))

        column_width = NODE_MAX_WIDTH + 60
        row_height = 110
        x = 24
        for depth_value in sorted(columns):
            y = 24
            for node_id in columns[depth_value]:
                self.nodes[node_id]["x"] = int(round(x / GRID) * GRID)
                self.nodes[node_id]["y"] = int(round(y / GRID) * GRID)
                _, height = self.node_size(self.nodes[node_id])
                y += height + row_height - height + 24
            x += column_width
        self.redraw()
        self._notify()

    # 撤销 / 重做
    def _snapshot(self) -> dict[str, Any]:
        return self.get_graph()

    def _push_undo(self) -> None:
        self._undo_stack.append(self._snapshot())
        if len(self._undo_stack) > 50:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def _on_undo(self, _event: Any) -> str:
        if not self._undo_stack:
            return "break"
        self._redo_stack.append(self._snapshot())
        self.set_graph(self._undo_stack.pop())
        self._notify(quiet=True)
        return "break"

    def _on_redo(self, _event: Any) -> str:
        if not self._redo_stack:
            return "break"
        self._undo_stack.append(self._snapshot())
        self.set_graph(self._redo_stack.pop())
        self._notify(quiet=True)
        return "break"

    def _on_configure(self, _event: Any) -> None:
        if self._marquee is not None or self._drag is not None:
            return  # 交互中不重排，避免抖动
        self.redraw()

    # ------------------------------------------------------------------ 回调
    def _notify(self, quiet: bool = False) -> None:
        if self.on_change is not None:
            self.on_change(self.get_graph())
        if not quiet and self.on_status is not None:
            self.on_status("因果链已更新并保存。")

    def _status(self) -> None:
        if self.on_status is None:
            return
        self.on_status(f"缩放 {int(self.zoom * 100)}% · {len(self.nodes)} 个节点 · {len(self.edges)} 条连线")

    def destroy(self) -> None:
        self._cancel_rename()
        self.canvas.destroy()
