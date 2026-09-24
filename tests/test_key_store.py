"""密钥存储（key_store）与 GUI 导入链路的固定测试集。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bugcompass import key_store, llm  # noqa: E402

import unittest


def _reset_caches() -> None:
    from bugcompass import resources

    resources.bundle_root.cache_clear()
    resources.data_root.cache_clear()
    resources.user_root.cache_clear()


class IsolatedHomeTestCase(unittest.TestCase):
    """独立 BUGCOMPASS_HOME + 强制文件模式，测试不碰真实钥匙串/家目录。"""

    def setUp(self) -> None:
        self._home = TemporaryDirectory(prefix="bugcompass-keys-test-")
        self.addCleanup(self._home.cleanup)
        patcher = mock.patch.dict(
            os.environ,
            {"BUGCOMPASS_HOME": self._home.name, "BUGCOMPASS_KEY_STORE": "file"},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(_reset_caches)
        _reset_caches()


class FileStoreTests(IsolatedHomeTestCase):
    def test_roundtrip(self) -> None:
        self.assertIsNone(key_store.get_key("deepseek"))
        mode = key_store.save_key("deepseek", "sk-test-abc")
        self.assertEqual(mode, "file")
        self.assertEqual(key_store.get_key("deepseek"), "sk-test-abc")
        key_store.delete_key("deepseek")
        self.assertIsNone(key_store.get_key("deepseek"))

    def test_file_permissions_and_content(self) -> None:
        key_store.save_key("qwen", "sk-xyz")
        path = key_store.keys_path()
        self.assertTrue(path.is_file())
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["keys"]["qwen"], "sk-xyz")

    def test_corrupted_file_treated_as_empty(self) -> None:
        key_store.keys_path().parent.mkdir(parents=True, exist_ok=True)
        key_store.keys_path().write_text("{不是 json", encoding="utf-8")
        self.assertIsNone(key_store.get_key("qwen"))
        key_store.save_key("qwen", "sk-1")  # 覆盖后恢复正常
        self.assertEqual(key_store.get_key("qwen"), "sk-1")

    def test_empty_secret_rejected(self) -> None:
        with self.assertRaises(key_store.KeyStoreError):
            key_store.save_key("x", "   ")

    def test_stored_providers_listing(self) -> None:
        key_store.save_key("a", "k1")
        key_store.save_key("b", "k2")
        self.assertEqual(key_store.stored_providers(), {"a": "file", "b": "file"})

    def test_storage_hint_mentions_location(self) -> None:
        hint = key_store.storage_hint()
        self.assertIn("keys.json", hint)


class KeychainModeTests(unittest.TestCase):
    """模拟 macOS 钥匙串（mock subprocess），验证调用与失败净化。"""

    def setUp(self) -> None:
        self._home = TemporaryDirectory(prefix="bugcompass-keychain-test-")
        self.addCleanup(self._home.cleanup)
        patcher = mock.patch.dict(
            os.environ,
            {"BUGCOMPASS_HOME": self._home.name, "BUGCOMPASS_KEY_STORE": "keychain"},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(_reset_caches)
        _reset_caches()

    def test_save_and_get_via_keychain(self) -> None:
        saved_commands: list[list[str]] = []

        def fake_run(command, **kwargs):
            saved_commands.append(list(command))
            if command[1] == "add-generic-password":
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[1] == "find-generic-password" and "-w" in command:
                return subprocess.CompletedProcess(command, 0, "sk-from-keychain\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(key_store.subprocess, "run", side_effect=fake_run):
            mode = key_store.save_key("deepseek", "sk-secret-value")
            self.assertEqual(mode, "keychain")
            value = key_store.get_key("deepseek")
        self.assertEqual(value, "sk-from-keychain")
        add_command = next(c for c in saved_commands if c[1] == "add-generic-password")
        self.assertIn("-w", add_command)
        self.assertIn("sk-secret-value", add_command)  # 确实传给了钥匙串

    def test_keychain_failure_falls_back_to_file_without_leaking(self) -> None:
        def failing_run(command, **kwargs):
            raise subprocess.CalledProcessError(1, command)

        with mock.patch.object(key_store.subprocess, "run", side_effect=failing_run):
            mode = key_store.save_key("deepseek", "sk-LEAK-CHECK-123")
            self.assertEqual(mode, "file")  # 钥匙串失败 → 文件模式
            # 读取路径也走文件
            self.assertEqual(key_store.get_key("deepseek"), "sk-LEAK-CHECK-123")

    def test_keychain_error_message_never_contains_secret(self) -> None:
        def failing_run(command, **kwargs):
            raise subprocess.CalledProcessError(1, command)

        with mock.patch.object(key_store.subprocess, "run", side_effect=failing_run), \
             mock.patch.object(key_store, "_write_file_store") as broken_write:
            broken_write.side_effect = OSError("disk full")
            with self.assertRaises(Exception) as ctx:
                key_store.save_key("deepseek", "sk-MUST-NOT-APPEAR")
        self.assertNotIn("sk-MUST-NOT-APPEAR", str(ctx.exception))
        self.assertNotIn("sk-MUST-NOT-APPEAR", str(ctx.exception.__cause__ or ""))

    def test_delete_ignores_missing(self) -> None:
        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(key_store.subprocess, "run", side_effect=fake_run):
            key_store.delete_key("never-existed")  # 不应抛错


class ResolveOrderTests(IsolatedHomeTestCase):
    def _provider(self):
        return llm.LLMProviderConfig(
            id="deepseek", label="DeepSeek", base_url="https://api.test/v1",
            model="m", api_key_env="TEST_ORDER_KEY",
        )

    def test_env_var_takes_priority_over_store(self) -> None:
        key_store.save_key("deepseek", "sk-stored")
        with mock.patch.dict(os.environ, {"TEST_ORDER_KEY": "sk-from-env"}):
            self.assertEqual(llm.resolve_api_key(self._provider()), "sk-from-env")

    def test_store_used_when_env_missing(self) -> None:
        key_store.save_key("deepseek", "sk-stored")
        env = {k: v for k, v in os.environ.items() if k != "TEST_ORDER_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(llm.resolve_api_key(self._provider()), "sk-stored")

    def test_error_message_points_to_gui_import(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "TEST_ORDER_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(llm.LLMError) as ctx:
                llm.resolve_api_key(self._provider())
        message = str(ctx.exception)
        self.assertIn("导入密钥", message)
        self.assertIn("TEST_ORDER_KEY", message)


class NoKeyInArtifactsTests(IsolatedHomeTestCase):
    def test_key_never_in_settings_or_providers(self) -> None:
        key_store.save_key("deepseek", "sk-NEVER-IN-SETTINGS")
        from bugcompass.settings import load_settings
        from bugcompass.llm import write_providers_template

        write_providers_template()
        settings_dump = json.dumps(load_settings(), ensure_ascii=False)
        providers_dump = key_store.keys_path().parent.joinpath("providers.json").read_text(encoding="utf-8")
        self.assertNotIn("sk-NEVER-IN-SETTINGS", settings_dump)
        self.assertNotIn("sk-NEVER-IN-SETTINGS", providers_dump)


if __name__ == "__main__":
    unittest.main()
