"""API 密钥的本地存储：macOS 钥匙串优先，其余平台用权限 600 的本地文件。

安全边界（与诊断包/备份的隐私设计一致）：

- 密钥只存在环境变量或这里，永不写入 providers.json / settings.json /
  工作区备份（只打包工作区目录）/ 诊断包（只含日志与环境摘要）；
- 钥匙串操作失败时抛出的错误信息经过净化，**绝不包含密钥本体**；
- 文件模式：``~/.bugcompass/keys.json``，POSIX 下 chmod 600，
  Windows 下位于 %APPDATA%\\BugCompass（默认仅当前用户可访问）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .resources import user_root

KEYS_FILENAME = "keys.json"
KEYCHAIN_SERVICE = "BugCompass"

MODE_KEYCHAIN = "keychain"
MODE_FILE = "file"


class KeyStoreError(Exception):
    """密钥存储错误。错误信息保证不含密钥本体。"""


def storage_mode() -> str:
    """当前使用的存储模式：macOS 钥匙串（可用时）或本地文件。"""
    override = os.environ.get("BUGCOMPASS_KEY_STORE", "").strip().lower()
    if override in (MODE_KEYCHAIN, MODE_FILE):
        return override
    if sys.platform == "darwin" and shutil.which("security"):
        return MODE_KEYCHAIN
    return MODE_FILE


def keys_path() -> Path:
    return user_root() / KEYS_FILENAME


# ------------------------------------------------------------------ 文件模式
def _read_file_store() -> dict[str, str]:
    path = keys_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, dict):
        return {}
    return {str(k): str(v) for k, v in keys.items() if isinstance(v, str) and v}


def _write_file_store(keys: dict[str, str]) -> None:
    path = keys_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "keys": keys}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover - 权限设置失败不阻断
            pass


# ------------------------------------------------------------------ 钥匙串
def _keychain(*args: str, secret: str | None = None) -> str:
    binary = shutil.which("security") or "/usr/bin/security"
    command = [binary, *args]
    if secret is not None:
        command += ["-w", secret]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        # 注意：不能把 exc 的内容带出去——它可能包含完整命令行（含密钥）。
        raise KeyStoreError(f"无法调用 macOS 钥匙串（{args[0]}）：{type(exc).__name__}") from None
    if result.returncode != 0:
        raise KeyStoreError(f"钥匙串操作失败（{args[0]}，返回码 {result.returncode}）")
    return result.stdout


def _keychain_add(provider_id: str, secret: str) -> None:
    _keychain("add-generic-password", "-s", KEYCHAIN_SERVICE, "-a", provider_id, "-w", secret, "-U")


def _keychain_get(provider_id: str) -> str | None:
    try:
        output = _keychain("find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", provider_id, "-w")
    except KeyStoreError:
        return None
    value = output.strip()
    return value or None


def _keychain_delete(provider_id: str) -> None:
    try:
        _keychain("delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", provider_id)
    except KeyStoreError:
        pass  # 不存在视为已删除


# ------------------------------------------------------------------ 对外接口
def save_key(provider_id: str, secret: str) -> str:
    """保存密钥，返回实际使用的存储模式（keychain / file）。"""
    if not secret or not secret.strip():
        raise KeyStoreError("密钥不能为空。")
    secret = secret.strip()
    if storage_mode() == MODE_KEYCHAIN:
        try:
            _keychain_add(provider_id, secret)
            # 回读验证：macOS 钥匙串存在“写入成功但读取被 ACL 拒绝”的边界情况，
            # 一旦回读不一致立即退回文件模式，避免“保存成功却连不上”的迷案。
            if _keychain_get(provider_id) == secret:
                return MODE_KEYCHAIN
            # 回读不一致：清掉坏条目再退回文件模式，避免之后读到旧值。
            _keychain_delete(provider_id)
        except KeyStoreError:
            pass  # 钥匙串不可用（如无图形会话）→ 退回文件模式
    keys = _read_file_store()
    keys[provider_id] = secret.strip()
    _write_file_store(keys)
    return MODE_FILE


def get_key(provider_id: str) -> str | None:
    """读取密钥；不存在返回 None。"""
    if storage_mode() == MODE_KEYCHAIN:
        value = _keychain_get(provider_id)
        if value:
            return value
    return _read_file_store().get(provider_id)


def delete_key(provider_id: str) -> None:
    """删除密钥（钥匙串与文件两处都清）。"""
    if storage_mode() == MODE_KEYCHAIN or sys.platform == "darwin":
        _keychain_delete(provider_id)
    keys = _read_file_store()
    if provider_id in keys:
        keys.pop(provider_id)
        _write_file_store(keys)


def stored_providers() -> dict[str, str]:
    """已存密钥的服务清单：{provider_id: 存储模式}。"""
    stored: dict[str, str] = {}
    mode = storage_mode()
    if mode == MODE_KEYCHAIN:
        stored = {provider_id: MODE_KEYCHAIN for provider_id in _list_keychain_accounts()}
    for provider_id in _read_file_store():
        stored.setdefault(provider_id, MODE_FILE)
    return stored


def _list_keychain_accounts() -> list[str]:
    """列出钥匙串里属于本服务的账户（provider id）。不返回密钥本体。"""
    binary = shutil.which("security") or "/usr/bin/security"
    try:
        result = subprocess.run(
            [binary, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-g"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    accounts: list[str] = []
    for line in result.stderr.splitlines():  # security 把属性打到 stderr
        line = line.strip()
        if line.startswith('"acct"'):
            parts = line.split("=")
            if len(parts) == 2:
                accounts.append(parts[1].strip().strip('"'))
    return accounts


def storage_hint() -> str:
    """给用户看的存储位置说明。"""
    if storage_mode() == MODE_KEYCHAIN:
        return "macOS 钥匙串（系统级加密存储）"
    if os.name == "nt":
        return "本地密钥文件（%APPDATA%\\BugCompass\\keys.json，仅当前用户可访问）"
    return "本地密钥文件（~/.bugcompass/keys.json，权限 600）"
