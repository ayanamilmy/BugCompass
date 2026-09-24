"""界面双语（i18n）的固定测试集：目录完整性、回退、占位符一致、语言设置。"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bugcompass import i18n  # noqa: E402

import unittest

GUI_SOURCES = (
    Path(__file__).resolve().parents[1] / "src" / "bugcompass" / "gui.py",
    Path(__file__).resolve().parents[1] / "src" / "bugcompass" / "report_gui.py",
)


def _tr_keys_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tr":
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                keys.add(node.args[0].value)
    return keys


class CatalogCompletenessTests(unittest.TestCase):
    """gui.py / report_gui.py 里每个 tr() 键都必须有英文翻译（防止后续提交漏翻）。"""

    def test_every_tr_key_has_translation(self) -> None:
        for path in GUI_SOURCES:
            keys = _tr_keys_in(path)
            missing = sorted(key for key in keys if key not in i18n.EN)
            self.assertEqual(missing, [], f"{path.name} 有 {len(missing)} 个 tr() 键缺英文翻译，例如：{missing[:3]}")

    def test_placeholder_counts_match(self) -> None:
        for key, value in i18n.EN.items():
            self.assertEqual(
                key.count("{}"), value.count("{}"),
                f"占位符数量不一致：{key!r} -> {value!r}",
            )

    def test_no_chinese_left_in_english_values(self) -> None:
        import re

        cjk = re.compile(r"[\u4e00-\u9fff]")
        leaked = [key for key, value in i18n.EN.items() if cjk.search(value)]
        self.assertEqual(leaked, [], f"英文翻译里残留中文：{leaked[:3]}")


class BehaviorTests(unittest.TestCase):
    def test_default_language_is_chinese(self) -> None:
        i18n.set_language("zh")
        self.assertEqual(i18n.get_language(), "zh")
        self.assertEqual(i18n.tr("设置"), "设置")

    def test_english_lookup_and_fallback(self) -> None:
        i18n.set_language("en")
        try:
            self.assertEqual(i18n.tr("设置"), "Settings")
            self.assertEqual(i18n.tr("全新未收录的键"), "全新未收录的键")  # 回退原样
        finally:
            i18n.set_language("zh")

    def test_set_language_normalizes(self) -> None:
        i18n.set_language("en-US")
        self.assertEqual(i18n.get_language(), "en")
        i18n.set_language("whatever")
        self.assertEqual(i18n.get_language(), "zh")
        i18n.set_language("zh")

    def test_template_formatting(self) -> None:
        i18n.set_language("en")
        try:
            self.assertEqual(i18n.tr("案件 {} 的调查已完成。").format("abc"), "Investigation for case abc is complete.")
        finally:
            i18n.set_language("zh")


class SettingsIntegrationTests(unittest.TestCase):
    def test_language_default_in_settings(self) -> None:
        from bugcompass.settings import DEFAULTS

        self.assertEqual(DEFAULTS.get("language"), "zh")


class LanguageSwitchTests(unittest.TestCase):
    """切换语言要整窗生效的字符串一致性：比较逻辑型字符串两侧都走 tr()。"""

    def test_gate_word_translates_consistently(self) -> None:
        # 红色实验授权门：提示与校验共用同一键 → 两种语言下比较仍然成立。
        i18n.set_language("en")
        try:
            prompt_gate = i18n.tr("明确授权")
            self.assertEqual(prompt_gate, "I AUTHORIZE")
        finally:
            i18n.set_language("zh")


if __name__ == "__main__":
    unittest.main()
