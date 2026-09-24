"""大模型服务接入（OpenAI 兼容端点）。

安全设计（与诊断包/备份的隐私边界一致）：

- ``providers.json`` 只保存「环境变量名」（api_key_env），**永不保存密钥本体**；
- 密钥只在发起请求的瞬间从环境变量读取，不写入任何文件、日志、备份或诊断包；
- 不引入第三方依赖，仅用标准库 ``urllib``；
- 默认调查引擎仍是 Codex CLI——不配置本模块时，应用行为与从前完全一致。

预设覆盖：DeepSeek、通义千问（DashScope 兼容模式）、Kimi（月之暗面）、智谱 GLM、
OpenAI、Ollama（本地，无需密钥），以及任意自定义 OpenAI 兼容端点。
"""

from __future__ import annotations

import copy
import json
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .resources import user_root
from .workspace import BugCompassError

PROVIDERS_FILENAME = "providers.json"
PROVIDERS_SCHEMA_VERSION = 1
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 2

PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class LLMError(BugCompassError):
    """可安全展示给用户的大模型服务错误。"""


@dataclass(frozen=True)
class LLMProviderConfig:
    """一个 OpenAI 兼容服务的配置。密钥不在此处——只有环境变量名。"""

    id: str
    label: str
    base_url: str
    model: str
    api_key_env: str = ""
    max_turns: int = 12
    timeout_seconds: float = 120.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "max_turns": self.max_turns,
            "timeout_seconds": self.timeout_seconds,
        }

    @property
    def display_name(self) -> str:
        return f"{self.label} · {self.model}"

    @property
    def needs_key(self) -> bool:
        return bool(self.api_key_env)


#: 内置预设：providers.json 不存在时使用；生成模板时也以此为蓝本。
PROVIDER_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
        "max_turns": 12,
        "timeout_seconds": 120,
    },
    {
        "id": "qwen",
        "label": "通义千问（DashScope 兼容模式）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "api_key_env": "DASHSCOPE_API_KEY",
        "max_turns": 12,
        "timeout_seconds": 120,
    },
    {
        "id": "kimi",
        "label": "Kimi（月之暗面）",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "kimi-k2-0905-preview",
        "api_key_env": "MOONSHOT_API_KEY",
        "max_turns": 12,
        "timeout_seconds": 120,
    },
    {
        "id": "glm",
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4.5",
        "api_key_env": "ZHIPU_API_KEY",
        "max_turns": 12,
        "timeout_seconds": 120,
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_key_env": "OPENAI_API_KEY",
        "max_turns": 12,
        "timeout_seconds": 120,
    },
    {
        "id": "ollama",
        "label": "Ollama（本地）",
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5-coder:7b",
        "api_key_env": "",
        "max_turns": 12,
        "timeout_seconds": 180,
    },
)


def providers_path() -> Path:
    return user_root() / PROVIDERS_FILENAME


def _provider_from_dict(raw: Any) -> LLMProviderConfig:
    if not isinstance(raw, dict):
        raise LLMError("providers.json 中每项必须是对象。")
    provider_id = str(raw.get("id", "")).strip()
    if not PROVIDER_ID_PATTERN.fullmatch(provider_id):
        raise LLMError(f"服务 id 非法（小写字母/数字/连字符，≤32 字符）：{provider_id!r}")
    base_url = str(raw.get("base_url", "")).strip()
    if not base_url.startswith(("http://", "https://")):
        raise LLMError(f"服务 {provider_id} 的 base_url 必须以 http(s):// 开头。")
    model = str(raw.get("model", "")).strip()
    if not model:
        raise LLMError(f"服务 {provider_id} 缺少 model。")
    return LLMProviderConfig(
        id=provider_id,
        label=str(raw.get("label", provider_id)).strip() or provider_id,
        base_url=base_url,
        model=model,
        api_key_env=str(raw.get("api_key_env", "")).strip(),
        max_turns=max(1, min(30, int(raw.get("max_turns", 12) or 12))),
        timeout_seconds=max(10.0, min(600.0, float(raw.get("timeout_seconds", 120) or 120))),
    )


def load_providers() -> list[LLMProviderConfig]:
    """读取用户配置；没有配置文件时返回内置预设。"""
    path = providers_path()
    if not path.is_file():
        return [_provider_from_dict(copy.deepcopy(preset)) for preset in PROVIDER_PRESETS]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LLMError(f"无法读取 {path}：{exc}\n可删除该文件后用 `bugcompass llm init-config` 重新生成。") from exc
    raw_providers = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(raw_providers, list) or not raw_providers:
        raise LLMError(f"{path} 里没有可用的 providers 列表。")
    seen: set[str] = set()
    providers = []
    for raw in raw_providers:
        provider = _provider_from_dict(raw)
        if provider.id in seen:
            raise LLMError(f"providers.json 中服务 id 重复：{provider.id}")
        seen.add(provider.id)
        providers.append(provider)
    return providers


def provider_by_id(providers: list[LLMProviderConfig], provider_id: str) -> LLMProviderConfig | None:
    return next((provider for provider in providers if provider.id == provider_id), None)


def write_providers_template() -> Path:
    """把预设写成 providers.json 模板，返回路径。已存在时不覆盖。"""
    path = providers_path()
    if path.is_file():
        return path
    payload = {
        "schema_version": PROVIDERS_SCHEMA_VERSION,
        "_comment": (
            "BugCompass 大模型服务配置。api_key_env 是密钥所在环境变量的名字，"
            "密钥本体永远不要写进任何文件。删除不需要的服务，或按需添加自定义 OpenAI 兼容端点。"
        ),
        "providers": [copy.deepcopy(preset) for preset in PROVIDER_PRESETS],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def resolve_api_key(provider: LLMProviderConfig) -> str | None:
    """解析密钥：环境变量优先，其次用户在 GUI 里导入的本地密钥存储。"""
    if not provider.api_key_env:
        return None
    value = _env_get(provider.api_key_env)
    if value:
        return value
    from .key_store import get_key

    stored = get_key(provider.id)
    if stored:
        return stored
    raise LLMError(
        f"服务 {provider.label} 还没有密钥。两种导入方式（任选其一）：\n"
        "1. 图形界面：⚙ 设置 → 模型服务 → 导入密钥…（推荐，存入系统钥匙串或本地加密权限文件）；\n"
        f"2. 环境变量：设置 {provider.api_key_env} 后重启 BugCompass"
        "（macOS/Linux 在 ~/.zshrc 加 export，Windows 用系统属性设置）。"
    )


def _env_get(name: str) -> str | None:
    import os

    return os.environ.get(name)


# --------------------------------------------------------------------- HTTP 层
def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "BugCompass", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 用户配置的端点
        body = response.read().decode("utf-8", errors="replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise LLMError(f"服务返回了无法解析的内容（{url}）：{str(exc)[:200]}") from exc


def _error_message(body: str) -> str:
    try:
        data = json.loads(body)
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"][:300]
        if isinstance(error, str):
            return error[:300]
        if isinstance(data.get("message"), str):
            return data["message"][:300]
    except json.JSONDecodeError:
        pass
    return body[:300]


@dataclass(frozen=True)
class ChatResponse:
    """一次对话补全的归一化结果。"""

    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = ""


def chat_completion(
    provider: LLMProviderConfig,
    messages: list[dict[str, Any]],
    *,
    api_key: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout: float | None = None,
) -> ChatResponse:
    """调用 OpenAI 兼容的 /chat/completions。

    429/5xx 自动重试（指数退避，最多 2 次）；401/403 直接给出密钥指引。
    """
    url = provider.base_url.rstrip("/") + "/chat/completions"
    headers: dict[str, str] = {}
    if provider.api_key_env:
        if not api_key:
            raise LLMError(resolve_api_key(provider))  # 带设置指引
        headers["Authorization"] = f"Bearer {api_key}"
    payload: dict[str, Any] = {
        "model": provider.model,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    last_error = ""
    for attempt in range(MAX_RETRIES + 1):
        try:
            data = _post_json(url, payload, headers, timeout or provider.timeout_seconds)
            break
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:  # pragma: no cover - 读取错误体失败
                pass
            message = _error_message(body)
            if exc.code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                last_error = f"HTTP {exc.code}：{message}"
                time.sleep(min(8.0, 1.5 ** (attempt + 1)))
                continue
            if exc.code in (401, 403):
                raise LLMError(
                    f"{provider.label} 拒绝了密钥（HTTP {exc.code}）。"
                    f"请检查环境变量 {provider.api_key_env} 里的密钥是否正确、是否有余额。"
                ) from exc
            raise LLMError(f"{provider.label} 返回 HTTP {exc.code}：{message}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise LLMError(
                f"无法连接 {provider.label}（{provider.base_url}）：{reason}。"
                "请检查网络、base_url，或该服务是否支持 OpenAI 兼容接口。"
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise LLMError(
                f"{provider.label} 超过 {int(timeout or provider.timeout_seconds)} 秒未响应。"
                "可在 providers.json 调大 timeout_seconds。"
            ) from exc
    else:  # pragma: no cover - 重试耗尽
        raise LLMError(f"{provider.label} 多次重试后仍失败：{last_error}")

    return _normalize_response(data, provider)


def _normalize_response(data: dict[str, Any], provider: LLMProviderConfig) -> ChatResponse:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError(f"{provider.label} 返回里没有 choices。")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise LLMError(f"{provider.label} 返回里没有 message。")
    content = message.get("content")
    tool_calls: list[dict[str, Any]] = []
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, list):
        for raw in raw_calls:
            if not isinstance(raw, dict):
                continue
            function = raw.get("function") or {}
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"_raw": arguments[:200]}
            if not isinstance(arguments, dict):
                arguments = {}
            tool_calls.append(
                {
                    "id": str(raw.get("id") or f"call_{len(tool_calls)}"),
                    "name": str(function.get("name", "")),
                    "arguments": arguments,
                }
            )
    usage_raw = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    usage = {
        "input_tokens": _as_int(usage_raw.get("prompt_tokens", usage_raw.get("input_tokens"))),
        "output_tokens": _as_int(usage_raw.get("completion_tokens", usage_raw.get("output_tokens"))),
        "cached_input_tokens": _as_int(
            (usage_raw.get("prompt_tokens_details") or {}).get("cached_tokens")
            if isinstance(usage_raw.get("prompt_tokens_details"), dict)
            else usage_raw.get("cached_tokens")
        ),
    }
    return ChatResponse(
        content=content if isinstance(content, str) else "",
        tool_calls=tool_calls,
        usage=usage,
        finish_reason=str(choices[0].get("finish_reason", "")),
    )


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def test_connection(provider: LLMProviderConfig) -> tuple[bool, str]:
    """用一次最小请求验证端点与密钥。只在用户主动点击时调用。"""
    try:
        api_key = resolve_api_key(provider)
        chat_completion(
            provider,
            [{"role": "user", "content": "回复：ok"}],
            api_key=api_key,
            max_tokens=8,
            timeout=min(30.0, provider.timeout_seconds),
        )
    except LLMError as exc:
        return False, str(exc)
    return True, f"连接成功：{provider.display_name}"
