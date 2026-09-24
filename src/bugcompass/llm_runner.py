"""大模型调查引擎：用 OpenAI 兼容 API 复现 Codex 的只读调查流程。

与 CodexRunner 的关系：

- **结果同构**：LLMRunResult 与 CodexRunResult 字段一致，GUI 的完成/取消/失败
  处理逻辑原样复用；运行摘要写 ``llm-last-run.json``（字段与 codex-last-run.json
  一致），指标统计（metrics.py）直接复用；
- **安全等价**：模型只拿到 4 个只读工具（list_dir / read_file / search_files 限
  Blender 仓库内，read_case_file 限当前案件目录白名单），无 shell、无写操作；
  工具结果带行数与大小上限，轮次与总时长有硬预算；
- **隐私等价**：密钥只在请求时从环境变量读取；事件日志只记轮次/工具名/token，
  不记密钥，也不复制问题正文。
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .investigation import (
    empty_investigation,
    export_markdown,
    merge_user_decisions,
    read_investigation,
    validate_investigation,
    write_investigation,
)
from .llm import LLMError, LLMProviderConfig, chat_completion, resolve_api_key
from .workspace import BugCompassError

ProgressCallback = Callable[[str], None]

MAX_TOOL_ROUNDS_HARD_CAP = 30
READ_FILE_MAX_LINES = 400
READ_FILE_MAX_CHARS = 40000
LIST_DIR_MAX_ENTRIES = 200
SEARCH_MAX_MATCHES = 50
SEARCH_MAX_FILES = 2000
SEARCH_MAX_FILE_BYTES = 1024 * 1024
ISSUE_MAX_CHARS = 12000
SKILL_MAX_CHARS = 9000
STATE_MAX_CHARS = 6000
FINAL_MAX_TOKENS = 8192
PRUNE_DIRS = {".git", "build", "__pycache__", ".cache", "node_modules", ".venv"}

BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".pdf",
    ".zip", ".gz", ".tar", ".7z", ".rar", ".exe", ".dll", ".dylib", ".so",
    ".a", ".o", ".obj", ".lib", ".blend", ".blend1", ".mp3", ".mp4", ".mov",
    ".avi", ".wav", ".ttf", ".otf", ".woff", ".woff2", ".pyc", ".bin", ".dat",
}

#: 案件目录里允许模型读取的路径前缀（read_case_file 的白名单）。
CASE_FILE_WHITELIST = ("experiments/", "investigation.json", "issue-original.md", "intake.md")

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出 Blender 仓库内某目录的内容（目录在前，最多 200 项）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对仓库根目录的路径，默认根目录"}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取 Blender 仓库内的文本文件片段（每次最多 400 行）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对仓库根目录的文件路径"},
                    "start_line": {"type": "integer", "description": "起始行号，从 1 开始，默认 1"},
                    "end_line": {"type": "integer", "description": "结束行号（含），默认 start+199"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "在 Blender 仓库文本文件里按正则搜索（最多返回 50 处匹配，含行号）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "path_glob": {"type": "string", "description": "限制搜索的子目录，如 source/blender，默认整个仓库"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_case_file",
            "description": "读取当前案件目录里的文件：investigation.json、issue-original.md、intake.md 或 experiments/ 下的实验记录。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "案件目录内的相对路径"},
                },
                "required": ["path"],
            },
        },
    },
]


@dataclass(frozen=True)
class LLMRunResult:
    """与 CodexRunResult 同构的结果（engine 字段用于 GUI 区分文案）。"""

    returncode: int
    cancelled: bool
    final_message: str
    error_detail: str
    investigation_updated: bool = False
    timed_out: bool = False
    engine: str = "llm"


class ToolError(Exception):
    """工具参数/路径问题——以错误文本回给模型，让它自行修正。"""


class LLMInvestigator:
    """用大模型 + 只读工具完成一次调查（同步执行，GUI 在工作线程里调用）。"""

    def __init__(self, data_root: str | Path, provider: LLMProviderConfig) -> None:
        self.data_root = Path(data_root).resolve()
        self.provider = provider
        self.max_rounds = max(1, min(MAX_TOOL_ROUNDS_HARD_CAP, provider.max_turns))
        self.total_budget_seconds = max(60.0, provider.timeout_seconds * (self.max_rounds + 1))
        self._cancel = threading.Event()

    # ------------------------------------------------------------------ 控制信号
    def reset_cancellation(self) -> None:
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    # ------------------------------------------------------------------ 主流程
    def run(
        self,
        view: Any,
        progress: ProgressCallback | None = None,
        action: str = "initial",
        target_id: str | None = None,
    ) -> LLMRunResult:
        # 注意：这里不重置取消信号——调用方（GUI）在启动前调用 reset_cancellation()，
        # 这样「启动前就已请求取消」的情况也能立即返回，与 CodexRunner 行为一致。
        case_dir = Path(view.case_dir)
        started = datetime.now(timezone.utc)
        run_dir = case_dir / "llm-runs"
        run_dir.mkdir(exist_ok=True)
        log_path = run_dir / f"{started.strftime('%Y%m%dT%H%M%SZ')}.jsonl"
        totals = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}

        def log(event: dict[str, Any]) -> None:
            try:
                with log_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            except OSError:  # pragma: no cover - 日志失败不中断调查
                pass

        def cancelled_result() -> LLMRunResult:
            return LLMRunResult(-1, True, "", "", False, False)

        try:
            api_key = resolve_api_key(self.provider)
        except LLMError as exc:
            return self._finish(view, started, log_path, 1, False, False, str(exc), totals, "密钥未配置")

        try:
            messages = self._build_messages(view, action, target_id)
        except BugCompassError as exc:
            return self._finish(view, started, log_path, 1, False, False, str(exc), totals, "构建提示失败")

        log({"type": "thread.started", "provider": self.provider.id, "model": self.provider.model})
        if progress:
            progress(f"{self.provider.label} 正在分析问题……（模型 {self.provider.model}，最多 {self.max_rounds} 轮工具调用）")

        deadline = started.timestamp() + self.total_budget_seconds
        rounds = 0
        final_content = ""
        final_finish_reason = ""
        error_detail = ""
        timed_out = False

        while rounds < self.max_rounds:
            if self._cancel.is_set():
                return cancelled_result()
            if datetime.now(timezone.utc).timestamp() > deadline:
                timed_out = True
                error_detail = (
                    f"调查超过总预算 {int(self.total_budget_seconds)} 秒，已停止。"
                    "可在 providers.json 调大 timeout_seconds 或调小 max_turns。"
                )
                break
            rounds += 1
            log({"type": "turn.started", "round": rounds})
            try:
                response = chat_completion(
                    self.provider,
                    messages,
                    api_key=api_key,
                    tools=TOOL_DEFINITIONS,
                )
            except LLMError as exc:
                error_detail = str(exc)
                break
            totals["input_tokens"] += response.usage.get("input_tokens", 0)
            totals["output_tokens"] += response.usage.get("output_tokens", 0)
            totals["cached_input_tokens"] += response.usage.get("cached_input_tokens", 0)
            log({"type": "turn.completed", "round": rounds, "usage": dict(response.usage),
                 "finish_reason": response.finish_reason})

            if response.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": response.content or "",
                        "tool_calls": [
                            {
                                "id": call["id"],
                                "type": "function",
                                "function": {"name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False)},
                            }
                            for call in response.tool_calls
                        ],
                    }
                )
                for call in response.tool_calls:
                    result_text = self._execute_tool(call["name"], call["arguments"], view)
                    log({"type": "item.completed", "item": {"type": "command_execution", "name": call["name"]}})
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": result_text})
                if progress:
                    progress(f"{self.provider.label} 正在只读检查本地源码（第 {rounds}/{self.max_rounds} 轮）……")
                continue

            final_content = response.content or ""
            final_finish_reason = response.finish_reason
            break

        # 轮次用尽但还没拿到最终答案：强制一次无工具请求，只要 JSON。
        if not final_content and not error_detail and not timed_out and not self._cancel.is_set():
            try:
                final_messages = messages + [{
                    "role": "user",
                    "content": "源码检查已结束。请根据已获得的证据，直接输出紧凑、完整的 investigation JSON 对象。"
                    "必须恰好包含 3 条调查路径；简要填写文字字段，不要重复源码片段或输出解释。",
                }]
                response = chat_completion(
                    self.provider, final_messages, api_key=api_key, tools=None,
                    max_tokens=FINAL_MAX_TOKENS,
                )
                totals["input_tokens"] += response.usage.get("input_tokens", 0)
                totals["output_tokens"] += response.usage.get("output_tokens", 0)
                totals["cached_input_tokens"] += response.usage.get("cached_input_tokens", 0)
                log({"type": "final.completed", "finish_reason": response.finish_reason,
                     "usage": dict(response.usage)})
                final_content = response.content or ""
                final_finish_reason = response.finish_reason
            except LLMError as exc:
                error_detail = str(exc)

        if self._cancel.is_set():
            return cancelled_result()

        if error_detail or timed_out:
            return self._finish(
                view, started, log_path, 124 if timed_out else 1, False, timed_out,
                error_detail, totals, "超时" if timed_out else "请求失败",
            )

        # 解析 + 落盘（与 CodexRunner 相同的后处理管线）；失败自动纠偏重试一次。
        detail_tail = ""

        def try_finalize(text: str) -> bool:
            try:
                data = self._extract_json(text)
                previous_path = Path(view.case_dir) / "investigation.json"
                previous = read_investigation(previous_path) if previous_path.is_file() else empty_investigation(view.case_id)
                candidate = validate_investigation(data, require_complete=True)
                merged = merge_user_decisions(previous, candidate)
                write_investigation(previous_path, merged, require_complete=True)
                export_markdown(view.case_dir, merged)
            except (ValueError, KeyError, BugCompassError, OSError, json.JSONDecodeError) as exc:
                nonlocal detail_tail
                detail_tail = f"\n结构化结果无效：{exc}"
                return False
            log({"type": "item.completed", "item": {"type": "agent_message", "text": "调查结果已写入 investigation.json"}})
            if progress:
                progress(f"{self.provider.label} 已完成调查，正在刷新结果……")
            return True

        # finish_reason=length 表示服务端已截断回复，即使其中恰好有一个可解析的
        # JSON 片段也不能当作完整调查结果写盘。
        updated = final_finish_reason != "length" and try_finalize(final_content or "")
        if final_finish_reason == "length":
            detail_tail = "\n结构化结果无效：模型回复达到输出长度上限，JSON 不完整。"
        if not updated and not self._cancel.is_set():
            # 截断或格式错误时，保留调查上下文，要求模型重发紧凑的完整 JSON。
            try:
                retry_messages = messages + [
                    {"role": "assistant", "content": (final_content or "")[:4000]},
                    {"role": "user", "content": "上一条回复无效（" + detail_tail.strip() + "）。"
                     "请根据前面的源码检查结果重新输出完整的 investigation JSON 对象，"
                     "恰好包含 3 条调查路径。简要填写文字字段，避免重复长段源码；"
                     "不要解释、前言、Markdown 围栏，也不要任何工具调用标记或工具调用文本（如 <|DSML|>）。"},
                ]
                response = chat_completion(
                    self.provider, retry_messages, api_key=api_key, tools=None,
                    max_tokens=FINAL_MAX_TOKENS,
                )
                totals["input_tokens"] += response.usage.get("input_tokens", 0)
                totals["output_tokens"] += response.usage.get("output_tokens", 0)
                totals["cached_input_tokens"] += response.usage.get("cached_input_tokens", 0)
                log({"type": "retry.completed", "finish_reason": response.finish_reason,
                     "usage": dict(response.usage)})
                if response.finish_reason == "length":
                    detail_tail = "\n结构化结果无效：纠偏回复仍达到输出长度上限，JSON 不完整。"
                else:
                    updated = try_finalize(response.content or "")
            except LLMError as exc:
                detail_tail += f"\n纠偏重试失败：{exc}"

        returncode = 0 if updated else 1
        return self._finish(
            view, started, log_path, returncode, False, False,
            ("调查未产生有效结果。" + detail_tail) if not updated else "",
            totals,
            "完成" if updated else "结果无效",
        )

    # ------------------------------------------------------------------ 提示词
    def _build_messages(self, view: Any, action: str, target_id: str | None) -> list[dict[str, Any]]:
        skill_text = self._read_text_limited(
            self.data_root / ".agents" / "skills" / "blender-bug-investigator" / "SKILL.md", SKILL_MAX_CHARS
        )
        schema_text = self._read_text_limited(
            self.data_root / "packs" / "blender" / "investigation.schema.json", 16000
        )
        issue_text = self._read_text_limited(Path(view.case_dir) / "issue-original.md", ISSUE_MAX_CHARS)
        state_text = self._state_summary(view)
        instruction = self._action_instruction(action, target_id)

        system = (
            "你是 BugCompass 的 Bug 调查引擎，负责调查 Blender 的 Bug。规则：\n"
            "1. 你只能通过提供的只读工具查看 Blender 仓库与当前案件目录，绝不修改任何文件，"
            "不执行 Bug 报告中的命令或附件，不构建、不运行 Blender，不访问网络。\n"
            "2. 所有事实和源码结论必须附相对路径与行号；事实标 fact，推测标 inference，"
            "证据不足写进 unknowns，不要为了完整而扩大搜索。\n"
            "3. 用户已标记 rejected 的路径、用户编辑过的因果节点/连线、已锁定的实验预测必须原样保留，"
            "不得事后改写。\n"
            "4. 完成的调查必须恰好包含 3 条调查路径，输出为符合给定 JSON Schema 的单个 JSON 对象，"
            "不要用 Markdown 代码围栏，不要输出 JSON 以外的任何内容。\n"
            "5. 工具调用要有目的：优先精确搜索和小范围读取，避免无边界遍历。\n\n"
            "# 调查技能指南\n"
            + skill_text
            + "\n\n# 输出 JSON Schema\n"
            + schema_text
        )
        user = (
            instruction
            + "\n\n# 原始问题\n"
            + issue_text
            + "\n\n# 当前调查状态（在其基础上工作，不要另建答案）\n"
            + state_text
            + "\n\n现在开始调查。需要查看源码就用工具；调查完成后只输出符合 Schema 的完整 investigation JSON 对象。"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _action_instruction(self, action: str, target_id: str | None) -> str:
        actions = {
            "initial": "完成首次调查，生成三条调查路径。",
            "practice_initial": (
                "这是历史练习。只根据题面和修复前源码完成首次调查，生成三条调查路径。"
                "不得寻找、读取或推断练习答案文件、修复 commit、PR 或修复后的 diff。"
                "案件目录里只有 issue-original.md 和 intake.md 可读。"
            ),
            "deepen": f"深入调查路径 {target_id}，复用已有证据并更新原有路径；不要另建答案。",
            "experiment": (
                f"实验 {target_id} 已由 BugCompass 执行。用 read_case_file 读取 experiments/ 中该实验最新的 "
                "result.json、stdout.txt 和 stderr.txt，根据真实结果更新实验的 result/effect/latest_run，"
                "并更新对应假设。不要再次执行命令。"
            ),
            "semantic_diff": (
                "只读检查 Blender 工作树中与当前案件相关的改动，更新 semantic_diff。"
                "描述旧规则、新规则、变化的不变量、受影响路径和剩余风险；"
                "没有实际相关改动时明确使用 proposed 或 not_available，不得伪装成 observed。"
            ),
            "continue": (
                "继续调查当前案件。如果三条路径仍为空，完成首次调查；否则复用已有证据、用户决定、"
                "因果链和实验记录，补强最需要验证的部分并更新同一个结果。不要另建答案。"
            ),
        }
        if action not in actions:
            raise BugCompassError(f"不支持的调查动作：{action}")
        return f"任务：{actions[action]}"

    def _state_summary(self, view: Any) -> str:
        investigation = getattr(view, "investigation", None)
        if not isinstance(investigation, dict):
            return "（尚无调查状态）"
        hypotheses = [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "priority": item.get("priority"),
                "status": item.get("status", "open"),
            }
            for item in investigation.get("hypotheses", [])
            if isinstance(item, dict)
        ]
        experiments = [
            {"id": item.get("id"), "status": item.get("status"), "latest_run": item.get("latest_run")}
            for item in investigation.get("suggested_experiments", [])
            if isinstance(item, dict)
        ]
        nodes = [
            {"id": node.get("id"), "label": node.get("label"), "user_edited": node.get("user_edited", False)}
            for node in (investigation.get("causal_graph") or {}).get("nodes", [])
            if isinstance(node, dict)
        ]
        state = {
            "case_id": getattr(view, "case_id", ""),
            "hypotheses": hypotheses,
            "rejected_hypotheses": [h["id"] for h in hypotheses if h["status"] == "rejected"],
            "causal_nodes": nodes,
            "experiments": experiments,
            "semantic_diff_status": (investigation.get("semantic_diff") or {}).get("status"),
        }
        text = json.dumps(state, ensure_ascii=False, indent=1)
        return text[:STATE_MAX_CHARS] + ("\n…（已截断）" if len(text) > STATE_MAX_CHARS else "")

    @staticmethod
    def _read_text_limited(path: Path, limit: int) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return f"（{path.name} 不可读，按通用规则调查）"
        return text[:limit] + ("\n…（已截断）" if len(text) > limit else "")

    # ------------------------------------------------------------------ 工具
    def _execute_tool(self, name: str, arguments: dict[str, Any], view: Any) -> str:
        try:
            if name == "list_dir":
                return self._list_dir(Path(view.repo_path), arguments.get("path") or "")
            if name == "read_file":
                return self._read_file(Path(view.repo_path), arguments)
            if name == "search_files":
                return self._search_files(Path(view.repo_path), arguments)
            if name == "read_case_file":
                return self._read_case_file(Path(view.case_dir), arguments.get("path"))
            return f"未知工具：{name}（可用：list_dir / read_file / search_files / read_case_file）"
        except ToolError as exc:
            return f"工具调用失败：{exc}"
        except (OSError, ValueError, TypeError, re.error) as exc:
            return f"工具调用失败：{exc}"

    @staticmethod
    def _safe_resolve(root: Path, raw: Any) -> Path:
        if not isinstance(raw, str) or not raw.strip():
            raise ToolError("path 必须是非空字符串（相对仓库根目录）。")
        if raw.startswith(("/", "\\")):
            raise ToolError("不允许绝对路径；只接受仓库内的相对路径。")
        rel = raw.strip().replace("\\", "/").lstrip("/")
        parts = [part for part in rel.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise ToolError("不允许使用 ..；只接受仓库内的相对路径。")
        if parts and (":" in parts[0] or len(parts[0]) == 0):
            raise ToolError("不允许绝对路径或盘符；只接受仓库内的相对路径。")
        resolved_root = root.resolve()
        target = resolved_root.joinpath(*parts) if parts else resolved_root
        target = target.resolve()
        if target != resolved_root and resolved_root not in target.parents:
            raise ToolError("路径超出仓库范围。")
        return target

    def _list_dir(self, repo_root: Path, raw_path: Any) -> str:
        target = self._safe_resolve(repo_root, raw_path if raw_path else ".")
        if not target.is_dir():
            return f"不是目录：{raw_path}"
        entries = []
        for child in sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
            if child.name.startswith(".") and child.name != ".gitignore":
                continue
            if child.is_dir():
                entries.append(child.name + "/")
            elif child.suffix.lower() not in BINARY_SUFFIXES:
                entries.append(child.name)
            if len(entries) >= LIST_DIR_MAX_ENTRIES:
                entries.append("…（超过 200 项，已截断）")
                break
        if not entries:
            return f"（目录为空：{raw_path}）"
        return "\n".join(entries)

    def _read_file(self, repo_root: Path, arguments: dict[str, Any]) -> str:
        raw_path = arguments.get("path")
        target = self._safe_resolve(repo_root, raw_path)
        if not target.is_file():
            return f"找不到文件：{raw_path}"
        if target.suffix.lower() in BINARY_SUFFIXES:
            return f"跳过二进制文件：{raw_path}"
        try:
            start = max(1, int(arguments.get("start_line", 1) or 1))
        except (TypeError, ValueError):
            start = 1
        try:
            requested_end = int(arguments.get("end_line", 0) or 0)
        except (TypeError, ValueError):
            requested_end = 0
        end = min(start + READ_FILE_MAX_LINES - 1, requested_end if requested_end > start else start + 199)
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return f"读取失败：{exc}"
        selected = lines[start - 1 : end]
        if not selected:
            return f"行号范围无内容（文件共 {len(lines)} 行）：{raw_path}:{start}-{end}"
        body = "\n".join(f"{number}: {line}" for number, line in enumerate(selected, start))
        if len(body) > READ_FILE_MAX_CHARS:
            body = body[:READ_FILE_MAX_CHARS] + "\n…（已截断）"
        header = f"{raw_path}:{start}-{min(end, start + len(selected) - 1)}（共 {len(lines)} 行）"
        return header + "\n" + body

    def _search_files(self, repo_root: Path, arguments: dict[str, Any]) -> str:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            return "缺少 pattern 参数。"
        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(re.escape(pattern))
        sub_dir = arguments.get("path_glob") or arguments.get("subdir") or ""
        search_root = self._safe_resolve(repo_root, sub_dir) if sub_dir else repo_root.resolve()
        if not search_root.is_dir():
            return f"搜索目录不存在：{sub_dir}"

        matches: list[str] = []
        scanned = 0
        for current, dirs, files in os.walk(search_root):
            dirs[:] = sorted(d for d in dirs if d not in PRUNE_DIRS)
            for filename in sorted(files):
                path = Path(current) / filename
                if path.suffix.lower() in BINARY_SUFFIXES:
                    continue
                if scanned >= SEARCH_MAX_FILES:
                    matches.append(f"…（已扫描 {scanned} 个文件达到上限，停止）")
                    return self._format_matches(matches, pattern, True)
                scanned += 1
                try:
                    if path.stat().st_size > SEARCH_MAX_FILE_BYTES:
                        continue
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        relative = path.relative_to(repo_root.resolve()).as_posix()
                        matches.append(f"{relative}:{number}: {line.strip()[:200]}")
                        if len(matches) >= SEARCH_MAX_MATCHES:
                            return self._format_matches(matches, pattern, True)
        return self._format_matches(matches, pattern, False)

    @staticmethod
    def _format_matches(matches: list[str], pattern: str, truncated: bool) -> str:
        if not matches:
            return f"没有匹配：{pattern}"
        suffix = "…（达到上限，结果被截断）" if truncated else ""
        return f"匹配 {pattern}：{len(matches)} 处\n" + "\n".join(matches) + suffix

    def _read_case_file(self, case_dir: Path, raw_path: Any) -> str:
        if not isinstance(raw_path, str) or not raw_path.strip():
            return "缺少 path 参数。"
        rel = raw_path.strip().replace("\\", "/").lstrip("/")
        if rel.startswith("..") or ":" in rel.split("/")[0]:
            return "不允许的路径；只接受案件目录内的相对路径。"
        if not (rel == "investigation.json" or rel == "issue-original.md" or rel == "intake.md" or rel.startswith("experiments/")):
            return f"只允许读取：{', '.join(CASE_FILE_WHITELIST)}"
        target = (case_dir / rel).resolve()
        if case_dir.resolve() not in target.parents:
            return "路径超出案件目录范围。"
        if not target.is_file():
            return f"找不到文件：{raw_path}"
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"读取失败：{exc}"
        if target.suffix.lower() in BINARY_SUFFIXES:
            return f"跳过二进制文件：{raw_path}"
        return text[:READ_FILE_MAX_CHARS] + ("\n…（已截断）" if len(text) > READ_FILE_MAX_CHARS else "")

    # ------------------------------------------------------------------ 收尾
    def _finish(
        self,
        view: Any,
        started: datetime,
        log_path: Path,
        returncode: int,
        cancelled: bool,
        timed_out: bool,
        error_detail: str,
        totals: dict[str, int],
        status_note: str,
    ) -> LLMRunResult:
        finished = datetime.now(timezone.utc)
        case_dir = Path(view.case_dir)
        summary = {
            "case_id": getattr(view, "case_id", ""),
            "model": f"{self.provider.label}·{self.provider.model}",
            "reasoning_effort": "api",
            "timeout_seconds": self.provider.timeout_seconds,
            "started_at": started.isoformat().replace("+00:00", "Z"),
            "finished_at": finished.isoformat().replace("+00:00", "Z"),
            "return_code": returncode,
            "cancelled": cancelled,
            "timed_out": timed_out,
            "log_path": str(log_path.relative_to(case_dir)),
            "error_tail": error_detail[-8000:],
            "status_note": status_note,
            "usage_total": dict(totals),
        }
        try:
            (case_dir / "llm-last-run.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError:  # pragma: no cover
            pass
        return LLMRunResult(returncode, cancelled, "", error_detail, returncode == 0, timed_out)

    # ------------------------------------------------------------------ JSON 解析
    _TOOL_MARKUP_TAG = re.compile(r"<[|｜*]?\s*(?:DSML|tool_call|think)[^>]*>", re.IGNORECASE)

    @classmethod
    def _strip_tool_markup(cls, text: str) -> str:
        """剥掉模型以文本形式输出的工具调用标记（DeepSeek 的 <|DSML|…> 等）。"""
        return cls._TOOL_MARKUP_TAG.sub(" ", text)

    @classmethod
    def _extract_json(cls, text: str) -> dict[str, Any]:
        markup_found = bool(cls._TOOL_MARKUP_TAG.search(text))
        stripped = cls._strip_tool_markup(text).strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            preview = stripped[:120].replace("\n", " ")
            if markup_found:
                guidance = (
                    "模型在用文本形式反复发起工具调用（DSML 标记）而没有输出约定的 JSON。"
                    "请在 ⚙ 设置 → 模型服务里换一个非推理模型后重试。"
                )
            elif not stripped:
                guidance = (
                    "回复为空，可能是推理类模型把输出放在思维链里——"
                    "请在 ⚙ 设置里换用非推理模型（如 deepseek-v4-flash）后重试。"
                )
            else:
                guidance = "请在 ⚙ 设置里换用非推理模型后重试。"
            raise ValueError(
                f"回复里找不到 JSON 对象（回复开头：{preview or '（空回复）'}）。{guidance}"
            )
        data = json.loads(stripped[start : end + 1])
        if not isinstance(data, dict):
            raise ValueError("JSON 顶层必须是对象。")
        return data
