from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import qa
from .gui_controller import CaseView
from .experiments import latest_experiment_record
from .investigation import export_markdown, merge_user_decisions, read_investigation, validate_investigation, write_investigation
from .workspace import BugCompassError


ProgressCallback = Callable[[str], None]


def _read_issue_text(case_dir: Path, limit: int = 1500) -> str:
    """原始 Bug 报告：追问时要让引擎看到用户当初到底报了什么。"""

    try:
        text = (Path(case_dir) / "issue-original.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[:limit]


class CodexRunBusyError(BugCompassError):
    """Raised when another GUI process already owns this Case run."""


@dataclass(frozen=True)
class CodexRunResult:
    returncode: int
    cancelled: bool
    final_message: str
    error_detail: str
    investigation_updated: bool = False
    timed_out: bool = False


class CodexRunner:
    DEFAULT_MODEL = "gpt-5.6-terra"
    DEFAULT_REASONING_EFFORT = "low"
    DEFAULT_TIMEOUT_SECONDS = 180

    def __init__(
        self,
        project_root: str | Path,
        executable: str | Path | None = None,
        *,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.executable = str(executable) if executable else self.find_executable()
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._cancel_requested = threading.Event()

    @staticmethod
    def find_executable() -> str | None:
        discovered = shutil.which("codex")
        if discovered:
            return discovered
        mac_app_binary = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
        if mac_app_binary.is_file() and os.access(mac_app_binary, os.X_OK):
            return str(mac_app_binary)
        return None

    def build_command(self, view: CaseView, action: str = "initial", target_id: str | None = None) -> list[str]:
        if not self.executable:
            raise BugCompassError("找不到 Codex CLI。请先安装或登录 Codex，再重试；案件已经安全保存在本地。")
        prompt = self._build_prompt(view, action, target_id)
        schema = self.project_root / "packs" / "blender" / "investigation.schema.json"
        output = view.case_dir / "investigation.next.json"
        return [
            self.executable,
            "exec",
            "--ignore-user-config",
            "--model",
            self.model,
            "--config",
            f'model_reasoning_effort="{self.reasoning_effort}"',
            "--json",
            "--color",
            "never",
            "--sandbox",
            "workspace-write",
            "--cd",
            str(view.case_dir),
            "--output-schema",
            str(schema),
            "-o",
            str(output),
            prompt,
        ]

    def _build_prompt(self, view: CaseView, action: str, target_id: str | None) -> str:
        skill_path = self.project_root / ".agents" / "skills" / "blender-bug-investigator" / "SKILL.md"
        actions = {
            "initial": "完成首次调查，生成三条调查路径。",
            "practice_initial": "这是历史练习。只根据题面和修复前源码完成首次调查，生成三条调查路径。不得寻找、读取或推断练习答案文件、修复 commit、PR 或修复后的 diff。",
            "deepen": f"深入调查路径 {target_id}，复用已有证据并更新原有路径；不要另建答案。",
            "experiment": f"实验 {target_id} 已由 BugCompass 执行。读取 experiments/ 中该实验最新的 result.json、stdout.txt 和 stderr.txt，根据真实结果更新实验的 result/effect/latest_run，并更新对应假设。不要再次执行命令。",
            "semantic_diff": "只读检查 Blender 工作树中与当前案件相关的 git diff，更新 semantic_diff。描述旧规则、新规则、变化的不变量、受影响路径和剩余风险；没有实际相关改动时明确使用 proposed 或 not_available，不得伪装成 observed。",
            "continue": "继续调查当前案件。如果三条路径仍为空，完成首次调查；否则复用已有证据、用户决定、因果链和实验记录，补强最需要验证的部分并更新同一个结果。不要另建答案。",
        }
        if action not in actions:
            raise BugCompassError(f"不支持的 Codex 调查动作：{action}")
        return (
            f"使用 $blender-bug-investigator 调查 BugCompass case {view.case_id}，"
            f"工作区是 {view.workspace_path}。先完整读取 Skill：{skill_path}。"
            f"这是非交互运行：{actions[action]}不要向用户提问；缺失信息写入 unknowns。"
            f"读取当前案件目录 {view.case_dir} 中的 investigation.json 并在其基础上工作。"
            f"只允许修改当前案件目录 {view.case_dir} 中由运行器指定的结构化输出文件。"
            "用户已标记为 rejected 的路径和 user_note 必须原样保留。"
            "保留用户编辑过的因果节点、连线和已经锁定的实验预测；不得事后改写用户预测。"
            "最终消息只输出符合给定 JSON Schema 的完整 investigation 对象；不要用 Markdown 代码围栏。"
            f"Blender 仓库 {view.repo_path} 必须保持只读。"
            "不要修改 Blender 源码，不运行 Blender、不构建、不执行 Bug 报告中的命令或附件，也不访问网络。"
            "所有事实和源码结论附相对路径与行号；事实用 fact，推测用 inference，未知写入 unknowns。"
            "本轮最多执行 8 组只读命令；优先精确搜索和小范围读取，不做无边界源码遍历。"
            "在证据不足时写入 unknowns，不要为了追求完整而持续扩大搜索。"
        )

    def build_ask_command(self, view: CaseView, question: str, turns: list[dict[str, Any]]) -> list[str]:
        """追问用的命令：只读沙箱、没有 --output-schema、不写 investigation.next.json。

        调查用 workspace-write 是因为要把结构化结果落盘；追问是纯对话，
        给它 read-only 就够了，也从根上保证它改不动案件。
        """

        if not self.executable:
            raise BugCompassError("找不到 Codex CLI。请先安装或登录 Codex，再重试；案件已经安全保存在本地。")
        return [
            self.executable,
            "exec",
            "--ignore-user-config",
            "--model",
            self.model,
            "--config",
            f'model_reasoning_effort="{self.reasoning_effort}"',
            "--json",
            "--color",
            "never",
            "--sandbox",
            "read-only",
            "--cd",
            str(view.case_dir),
            self._build_ask_prompt(view, question, turns),
        ]

    def _build_ask_prompt(self, view: CaseView, question: str, turns: list[dict[str, Any]]) -> str:
        history = qa.recent_messages(turns)
        conversation = "\n".join(
            f"{'用户' if item['role'] == 'user' else '你'}：{item['content']}" for item in history
        )
        return (
            f"这是对 BugCompass case {view.case_id} 的追问，不是新的调查。"
            f"案件目录 {view.case_dir}，Blender 仓库 {view.repo_path}（只读）。\n"
            "只回答用户这一次的问题：不要生成或改写 investigation.json，不要创建任何文件，"
            "不要执行 Bug 报告里的命令，不构建、不运行 Blender、不访问网络。\n"
            "可以只读查看案件目录和 Blender 源码来支撑回答，引用源码时给出相对路径与行号。\n"
            "用简体中文回答：先给结论，再给依据；依据不足就直接说证据不足，不要编造。"
            "不要输出 JSON，不要重复整份调查内容，控制在 400 字以内。\n\n"
            "# 当前案件摘要\n"
            + qa.case_brief(
                view.investigation,
                _read_issue_text(view.case_dir),
            )
            + ("\n\n# 此前的问答\n" + conversation if conversation else "")
            + f"\n\n# 用户这次的问题\n{question.strip()}"
        )

    def ask(
        self,
        view: CaseView,
        question: str,
        turns: list[dict[str, Any]],
        progress: ProgressCallback | None = None,
    ) -> str:
        """就当前案件追问一次，返回 Codex 的回答；不写 investigation，也不计一次调查运行。"""

        if not question.strip():
            raise BugCompassError("请先写下你的问题。")
        lock_token = self._acquire_case_lock(view)
        try:
            if self._cancel_requested.is_set():
                raise BugCompassError("追问已取消。")
            command = self.build_ask_command(view, question, turns)
            if progress:
                progress(f"Codex 正在读源码回答……（{self.model}，最多 {int(self.timeout_seconds)} 秒）")
            returncode, cancelled, final_message, detail, timed_out = self._stream_process(command, view.case_dir, progress)
            if cancelled:
                raise BugCompassError("追问已取消。")
            if timed_out:
                raise BugCompassError(f"Codex 超过 {int(self.timeout_seconds)} 秒仍未回答，已停止本次追问。")
            if returncode != 0:
                raise BugCompassError(CodexRunner.extract_error_message(detail) or f"Codex 退出码 {returncode}，没有回答。")
            if not final_message.strip():
                raise BugCompassError("Codex 没有返回回答。")
            return final_message.strip()
        finally:
            self._release_case_lock(view, lock_token)

    def run(self, view: CaseView, progress: ProgressCallback | None = None, action: str = "initial", target_id: str | None = None) -> CodexRunResult:
        lock_token = self._acquire_case_lock(view)
        try:
            return self._run_locked(view, progress, action, target_id)
        finally:
            self._release_case_lock(view, lock_token)

    def _stream_process(
        self,
        command: list[str],
        cwd: Path,
        progress: ProgressCallback | None = None,
        log_stream: Any = None,
    ) -> tuple[int, bool, str, str, bool]:
        """跑一个 Codex 进程并把事件流式转发出去。

        返回 ``(returncode, cancelled, final_message, detail, timed_out)``。
        调查和追问共用这一段：取消、超时、按行转发只写一次。
        """

        recent_output: deque[str] = deque(maxlen=30)
        final_message = ""
        timed_out = threading.Event()
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            raise BugCompassError(f"无法启动 Codex：{exc}") from exc

        with self._lock:
            self._process = process
        timer = threading.Timer(self.timeout_seconds, self._timeout_process, args=(process, timed_out))
        timer.daemon = True
        timer.start()
        try:
            assert process.stdout is not None
            for raw_line in process.stdout:
                if log_stream is not None:
                    log_stream.write(raw_line)
                    log_stream.flush()
                line = raw_line.strip()
                if not line:
                    continue
                recent_output.append(line)
                event_message, agent_message = self._parse_event(line)
                if agent_message:
                    final_message = agent_message
                if event_message and progress:
                    progress(event_message)
            returncode = process.wait()
        except Exception:
            self._terminate_process(process)
            raise
        finally:
            timer.cancel()
            with self._lock:
                self._process = None

        detail = "\n".join(recent_output)
        if timed_out.is_set():
            detail += f"\nCodex 超过 {int(self.timeout_seconds)} 秒仍未完成，BugCompass 已自动停止本次运行。"
        return returncode, self._cancel_requested.is_set(), final_message, detail, timed_out.is_set()

    def _run_locked(self, view: CaseView, progress: ProgressCallback | None, action: str, target_id: str | None) -> CodexRunResult:
        output = view.case_dir / "investigation.next.json"
        output.unlink(missing_ok=True)
        command = self.build_command(view, action, target_id)
        if self._cancel_requested.is_set():
            return CodexRunResult(-1, True, "", "", False)
        run_started_at = datetime.now(timezone.utc)
        run_dir = view.case_dir / "codex-runs"
        run_dir.mkdir(exist_ok=True)
        run_log = run_dir / f"{run_started_at.strftime('%Y%m%dT%H%M%SZ')}.jsonl"
        if progress:
            progress(
                f"Codex 正在读取案件和源码……（{self.model} / {self.reasoning_effort}，最多 {int(self.timeout_seconds)} 秒）"
            )

        with run_log.open("w", encoding="utf-8") as log_stream:
            returncode, cancelled, final_message, detail, timed_out = self._stream_process(
                command, view.case_dir, progress, log_stream
            )

        updated = False
        if returncode == 0 and not cancelled and not timed_out:
            if not output.is_file():
                detail += "\n缺少结构化输出文件。"
            else:
                try:
                    previous = read_investigation(view.case_dir / "investigation.json")
                    candidate = validate_investigation(json.loads(output.read_text(encoding="utf-8")), require_complete=True)
                    candidate = merge_user_decisions(previous, candidate)
                    write_investigation(view.case_dir / "investigation.json", candidate, require_complete=True)
                    export_markdown(view.case_dir, candidate)
                    if action == "experiment" and target_id:
                        record_path = latest_experiment_record(view.case_dir, target_id)
                        reviewed = next((item for item in candidate["suggested_experiments"] if item.get("id") == target_id), None)
                        if record_path and reviewed:
                            record = json.loads(record_path.read_text(encoding="utf-8"))
                            record["hypothesis_effect"] = reviewed.get("effect", "inconclusive")
                            record["codex_assessment"] = reviewed.get("result", "")
                            record["prediction_assessment"] = reviewed.get("prediction_assessment", "unknown")
                            record["prediction_comparison"] = reviewed.get("prediction_comparison", "")
                            record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    updated = True
                except (OSError, json.JSONDecodeError, BugCompassError) as exc:
                    detail += f"\n结构化结果无效：{exc}"
                finally:
                    output.unlink(missing_ok=True)
        self._write_run_summary(
            view,
            run_started_at=run_started_at,
            returncode=returncode,
            cancelled=cancelled,
            timed_out=timed_out,
            log_path=run_log,
            error_detail=detail,
        )
        return CodexRunResult(returncode, cancelled, final_message, detail, updated, timed_out)

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _acquire_case_lock(self, view: CaseView) -> str:
        lock_path = view.case_dir / ".codex-run.lock"
        token = secrets.token_hex(16)
        payload = json.dumps(
            {
                "case_id": view.case_id,
                "owner_pid": os.getpid(),
                "token": token,
                "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
            ensure_ascii=False,
        )
        for _attempt in range(2):
            try:
                descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                try:
                    existing = json.loads(lock_path.read_text(encoding="utf-8"))
                    owner_pid = int(existing.get("owner_pid", 0))
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    owner_pid = 0
                if self._pid_is_alive(owner_pid):
                    raise CodexRunBusyError(
                        f"案件 {view.case_id} 已经在另一个 BugCompass 窗口中调查，未重复启动 Codex。"
                    )
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise BugCompassError(f"无法清理案件的旧运行锁：{exc}") from exc
                continue
            try:
                os.write(descriptor, payload.encode("utf-8"))
            finally:
                os.close(descriptor)
            return token
        raise CodexRunBusyError(f"案件 {view.case_id} 已经在运行，未重复启动 Codex。")

    @staticmethod
    def _release_case_lock(view: CaseView, token: str) -> None:
        lock_path = view.case_dir / ".codex-run.lock"
        try:
            existing = json.loads(lock_path.read_text(encoding="utf-8"))
            if existing.get("token") == token:
                lock_path.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            return

    def _timeout_process(self, process: subprocess.Popen[str], timed_out: threading.Event) -> None:
        if process.poll() is not None:
            return
        timed_out.set()
        self._terminate_process(process)

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
            return

    def _write_run_summary(
        self,
        view: CaseView,
        *,
        run_started_at: datetime,
        returncode: int,
        cancelled: bool,
        timed_out: bool,
        log_path: Path,
        error_detail: str,
    ) -> None:
        summary = {
            "case_id": view.case_id,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "timeout_seconds": self.timeout_seconds,
            "started_at": run_started_at.isoformat().replace("+00:00", "Z"),
            "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "return_code": returncode,
            "cancelled": cancelled,
            "timed_out": timed_out,
            "log_path": str(log_path.relative_to(view.case_dir)),
            "error_tail": error_detail[-8000:],
        }
        try:
            (view.case_dir / "codex-last-run.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    def reset_cancellation(self) -> None:
        self._cancel_requested.clear()

    def cancel(self) -> None:
        self._cancel_requested.set()
        with self._lock:
            process = self._process
        if process is None or process.poll() is not None:
            return
        self._terminate_process(process)

    @staticmethod
    def extract_error_message(detail: str) -> str:
        for line in reversed(detail.splitlines()):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") not in {"error", "turn.failed"}:
                continue
            error = event.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                return error["message"].strip()
            if isinstance(error, str):
                return error.strip()
            if isinstance(event.get("message"), str):
                return event["message"].strip()
        return ""

    @staticmethod
    def _parse_event(line: str) -> tuple[str | None, str | None]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None, None
        event_type = event.get("type")
        if event_type == "thread.started":
            return "Codex 调查已开始……", None
        if event_type == "turn.started":
            return "Codex 正在分析问题……", None
        if event_type == "turn.completed":
            return "Codex 已完成调查，正在刷新结果……", None
        if event_type == "turn.failed":
            return "Codex 调查失败。", None
        if event_type == "error":
            return "Codex 返回了错误。", None
        item = event.get("item")
        if not isinstance(item, dict):
            return None, None
        item_type = item.get("type")
        if event_type == "item.started" and item_type == "command_execution":
            return "Codex 正在只读检查本地源码……", None
        if event_type == "item.completed" and item_type == "agent_message":
            text = item.get("text")
            return "Codex 正在整理调查结果……", text if isinstance(text, str) else None
        return None, None
