"""界面基础控件的排版计算：折行算法与换行宽度。

折行算法是纯函数，不需要窗口也能测；换行宽度那部分用假的 Tk 控件验证
「登记时立刻套用宽度」这条不变量。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bugcompass.widgets import WRAP_MARGIN, ScrollableFrame, flow_rows, grid_columns, wrap_to_self  # noqa: E402


def row_widths(rows: list[list[int]], widths: list[int], gap: int = 10) -> list[int]:
    """按 Tk 的真实规则算每行宽度：列宽取所有行在该列的最大值。"""

    columns = max((len(row) for row in rows), default=0)
    column_width = [0] * columns
    for row in rows:
        for column, index in enumerate(row):
            column_width[column] = max(column_width[column], widths[index])
    return [sum(column_width[: len(row)]) + gap * (len(row) - 1) for row in rows]


class FlowRowsTests(unittest.TestCase):
    """按钮行折行：任何一行都不能超出可用宽度，否则控件会被容器裁掉。"""

    BUTTONS = [168, 116, 126, 141, 116, 116, 116, 116, 116]

    def test_never_exceeds_the_available_width(self) -> None:
        for available in (200, 300, 500, 660, 1000, 1600):
            with self.subTest(available=available):
                rows = flow_rows(self.BUTTONS, available, 10)
                widest = max(row_widths(rows, self.BUTTONS, 10))
                self.assertLessEqual(widest, available)

    def test_keeps_every_widget_exactly_once(self) -> None:
        rows = flow_rows(self.BUTTONS, 500, 10)
        self.assertEqual(sorted(index for row in rows for index in row), list(range(len(self.BUTTONS))))

    def test_shared_columns_are_accounted_for(self) -> None:
        # 第 0 个按钮最宽（168），后面几行的第 0 列也会被撑到 168。
        # 只看单行累加会以为「126 + 141」放得下，实际会越界。
        rows = flow_rows([168, 126, 141], 300, 10)
        self.assertLessEqual(max(row_widths(rows, [168, 126, 141], 10)), 300)
        self.assertEqual(rows, [[0], [1], [2]])

    def test_single_row_when_everything_fits(self) -> None:
        self.assertEqual(flow_rows([100, 100, 100], 500, 10), [[0, 1, 2]])

    def test_a_widget_wider_than_the_row_is_left_alone(self) -> None:
        # 单个控件就比容器宽：无处可折，不能死循环，也不能把它丢掉。
        self.assertEqual(flow_rows([500], 300, 10), [[0]])

    def test_empty_input(self) -> None:
        self.assertEqual(flow_rows([], 300), [])

    def test_unmeasured_width_falls_back_to_one_per_row(self) -> None:
        self.assertEqual(flow_rows([10, 20], 0), [[0], [1]])


class _FakeLabel:
    """够用就好的假 Label：只关心 wraplength 有没有被设置。"""

    def __init__(self, path: str = ".label") -> None:
        self.options: dict[str, int] = {}
        self._path = path
        self.destroyed = False

    def configure(self, **kwargs: int) -> None:
        self.options.update(kwargs)

    def winfo_exists(self) -> bool:
        return not self.destroyed

    def cget(self, key: str) -> int:
        return self.options.get(key, 0)

    def __str__(self) -> str:
        return self._path


class _FakeFrame:
    """假内容帧：只需要路径、子控件列表和销毁。"""

    def __init__(self, path: str, children: list | None = None) -> None:
        self._path = path
        self._children = list(children or [])

    def winfo_children(self) -> list:
        return list(self._children)

    def __str__(self) -> str:
        return self._path


class _FakeBindable:
    """假控件：记下 ``<Configure>`` 回调，测试里手动触发。"""

    def __init__(self, cget_value: str = "") -> None:
        self.options: dict[str, int] = {}
        self._handler = None
        self._cget_value = cget_value

    def bind(self, _sequence: str, handler) -> None:
        self._handler = handler

    def configure(self, **kwargs: int) -> None:
        self.options.update(kwargs)

    def cget(self, _key: str) -> str:
        return self._cget_value

    def fire_configure(self, width: int) -> None:
        self._handler(type("ConfigureEvent", (), {"width": width})())


class WrapToSelfTests(unittest.TestCase):
    """按自身宽度换行：ttk 的空字符串选项不能把这件事搞砸。"""

    def test_sets_wraplength_from_its_own_width(self) -> None:
        label = _FakeBindable()
        wrap_to_self(label)
        label.fire_configure(320)
        # 留出余量：分到的宽度含边框与内边距，按整宽换行会切掉每行末尾半个字。
        self.assertEqual(label.options["wraplength"], 320 - WRAP_MARGIN)

    def test_never_exceeds_the_width_it_was_given(self) -> None:
        for width in (100, 320, 800, 1600):
            with self.subTest(width=width):
                label = _FakeBindable()
                wrap_to_self(label)
                label.fire_configure(width)
                self.assertLess(label.options["wraplength"], width)

    def test_works_even_when_the_option_was_never_set(self) -> None:
        # 没设过的 ttk 像素选项读出来是空字符串；按返回值判断会抛异常并被吞掉，
        # 换行就永远不生效（Windows 上一句话会被切成好几行）。
        label = _FakeBindable(cget_value="")
        wrap_to_self(label)
        label.fire_configure(400)
        self.assertEqual(label.options["wraplength"], 400 - WRAP_MARGIN)

    def test_same_width_twice_is_ignored(self) -> None:
        label = _FakeBindable()
        wrap_to_self(label)
        label.fire_configure(300)
        label.options.clear()
        label.fire_configure(300)
        self.assertEqual(label.options, {})

    def test_tiny_width_keeps_a_readable_floor(self) -> None:
        label = _FakeBindable()
        wrap_to_self(label)
        label.fire_configure(1)
        self.assertEqual(label.options["wraplength"], 80)

    def test_floor_survives_the_margin(self) -> None:
        # 80 是下限，不能被余量再削一次（窄到 82 时结果还得是 80）。
        label = _FakeBindable()
        wrap_to_self(label)
        label.fire_configure(80 + WRAP_MARGIN)
        self.assertEqual(label.options["wraplength"], 80)


class GridColumnsTests(unittest.TestCase):
    """等宽网格：宽度不够时减列，而不是把每列压窄（压窄就会裁字）。"""

    def test_wide_container_takes_all_columns(self) -> None:
        self.assertEqual(grid_columns([200] * 6, 1400, 8, 3), 3)

    def test_narrow_container_drops_columns(self) -> None:
        self.assertEqual(grid_columns([200] * 6, 600, 8, 3), 2)

    def test_one_column_when_nothing_fits(self) -> None:
        self.assertEqual(grid_columns([200] * 6, 100, 8, 3), 1)

    def test_the_widest_item_decides(self) -> None:
        self.assertEqual(grid_columns([100, 400], 900, 8, 3), 2)
        self.assertEqual(grid_columns([100, 400], 800, 8, 3), 1)

    def test_empty_input(self) -> None:
        self.assertEqual(grid_columns([], 500), 1)


class _FakeCanvas:
    def __init__(self) -> None:
        self.itemconfigure_calls: list[tuple] = []
        self.scrollregion = None
        self.configures: list[tuple] = []

    def itemconfigure(self, *args, **kwargs) -> None:
        self.itemconfigure_calls.append((args, kwargs))

    def update_idletasks(self) -> None:
        pass

    def winfo_width(self) -> int:
        return 800

    def winfo_height(self) -> int:
        return 600

    def bbox(self, _item):
        return (0, 0, 800, 100)

    def configure(self, **kwargs) -> None:
        self.configures.append(tuple(kwargs.items()))

    def bind(self, *_args, **_kwargs) -> None:
        pass

    def yview_moveto(self, _fraction: float) -> None:
        pass


class _FakeScrollbar:
    def grid(self, **_kwargs) -> None:
        pass

    def grid_remove(self) -> None:
        pass


class WrapWidthTests(unittest.TestCase):
    """换行宽度必须在登记时就套用，不能等下一次 refresh。"""

    def _scroll(self, content_path: str = ".app.canvas.content") -> ScrollableFrame:
        scroll = ScrollableFrame.__new__(ScrollableFrame)
        scroll.canvas = _FakeCanvas()
        scroll.scrollbar = _FakeScrollbar()
        scroll.content = _FakeFrame(content_path)
        scroll._window_id = 1
        scroll._wrapped = []
        scroll._show_scrollbar = True
        scroll._applied_width = 0
        scroll._zoom_callback = None
        return scroll

    def test_registered_label_gets_a_width_right_away(self) -> None:
        # 先量过宽度，再登记标签：不能停在 0（Tk 里 0 等于不换行，长文本会顶破卡片）。
        scroll = self._scroll()
        scroll.refresh()
        label = _FakeLabel()
        scroll.wrap_here(label, padding=56)
        self.assertEqual(label.cget("wraplength"), 800 - 56)

    def test_label_registered_before_measuring_is_left_for_the_next_pass(self) -> None:
        scroll = self._scroll()
        label = _FakeLabel()
        scroll.wrap_here(label, padding=56)
        self.assertEqual(label.cget("wraplength"), 0)
        scroll.refresh()
        self.assertEqual(label.cget("wraplength"), 800 - 56)

    def test_width_changes_reach_the_labels(self) -> None:
        scroll = self._scroll()
        scroll.refresh()
        label = _FakeLabel()
        scroll.wrap_here(label, padding=56)
        scroll.canvas.winfo_width = lambda: 400
        scroll.refresh()
        self.assertEqual(label.cget("wraplength"), 400 - 56)

    def test_tiny_width_keeps_a_readable_floor(self) -> None:
        scroll = self._scroll()
        scroll.canvas.winfo_width = lambda: 10
        scroll.refresh()
        label = _FakeLabel()
        scroll.wrap_here(label, padding=56)
        self.assertEqual(label.cget("wraplength"), 80)

    def test_clear_keeps_labels_that_live_outside_the_content(self) -> None:
        # 概览正文是画布的兄弟节点，清空内容不会让它消失：清一次就注销的话，
        # 它再也不跟着窗口宽度换行，窗口一变宽文字就被裁。
        scroll = self._scroll()
        scroll.refresh()
        outside = _FakeLabel(".app.summary.label")
        inside = _FakeLabel(".app.canvas.content.card.label")
        scroll.wrap_here(outside, padding=56)
        scroll.wrap_here(inside, padding=56)
        scroll.clear()
        self.assertEqual([str(label) for label, _ in scroll._wrapped], [".app.summary.label"])
        # 保留登记还不够，宽度也得跟着新尺寸更新
        scroll.canvas.winfo_width = lambda: 400
        scroll.refresh()
        self.assertEqual(outside.cget("wraplength"), 400 - 56)

    def test_clear_drops_labels_that_are_gone(self) -> None:
        scroll = self._scroll()
        scroll.refresh()
        label = _FakeLabel(".app.summary.label")
        scroll.wrap_here(label, padding=56)
        label.destroyed = True
        scroll.clear()
        self.assertEqual(scroll._wrapped, [])



if __name__ == "__main__":
    unittest.main()
