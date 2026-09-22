from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bugcompass.markdown_view import plain_text, render_markdown  # noqa: E402


class MarkdownViewTests(unittest.TestCase):
    def test_hides_common_markdown_markers(self) -> None:
        source = "# 标题\n\n- **事实**：这里有 `代码`\n> 一条引用\n"
        rendered = plain_text(source)
        self.assertEqual(rendered, "标题\n\n• 事实：这里有 代码\n▎ 一条引用\n")
        self.assertNotIn("#", rendered)
        self.assertNotIn("**", rendered)
        self.assertNotIn("`", rendered)

    def test_assigns_styles_to_headings_and_inline_content(self) -> None:
        spans = render_markdown("## 调查路径\n包含 **重点** 和 `symbol_name`。\n")
        tagged_text = {(span.text, span.tags) for span in spans if span.text.strip()}
        self.assertIn(("调查路径", ("heading2",)), tagged_text)
        self.assertIn(("重点", ("body", "bold")), tagged_text)
        self.assertIn(("symbol_name", ("body", "inline_code")), tagged_text)

    def test_preserves_markers_inside_code_blocks(self) -> None:
        source = "```python\n# 这是代码注释\nvalue = a * b\n```\n"
        self.assertEqual(plain_text(source), "# 这是代码注释\nvalue = a * b\n")
        code_spans = [span for span in render_markdown(source) if span.text]
        self.assertTrue(all("code_block" in span.tags for span in code_spans))

    def test_formats_links_and_ordered_lists(self) -> None:
        source = "1. 查看 [Blender](https://blender.org)\n"
        self.assertEqual(plain_text(source), "1. 查看 Blender（https://blender.org）\n")


if __name__ == "__main__":
    unittest.main()
