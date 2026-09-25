"""案件内的追问对话：只记录问答，绝不改动调查结果。

追问是「纯对话」——引擎的回答落进案件目录的 qa.json，investigation.json 一个字节都不动。
所以这里没有依赖 tkinter，也不需要 Blender 仓库，可以单独测试。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .workspace import BugCompassError, utc_now


CONVERSATION_FILENAME = "qa.json"
ROLES = ("user", "assistant")
# 落盘上限：只留最近这些轮，避免长对话把案件目录撑大。
MAX_TURNS = 200
# 送进引擎的历史轮数：太多会挤掉案件上下文，太少又接不上话。
CONTEXT_TURNS = 8
# 案件简报的字数上限，防止超长题面吃掉整个上下文窗口。
BRIEF_LIMIT = 6000
ISSUE_LIMIT = 1500


def conversation_path(case_dir: str | Path) -> Path:
    return Path(case_dir) / CONVERSATION_FILENAME


def load_turns(case_dir: str | Path) -> list[dict[str, str]]:
    """读取对话记录；文件缺失或损坏时当作空对话——追问坏了不能连累案件页。"""

    path = conversation_path(case_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = raw.get("turns") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    turns: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ROLES or not isinstance(content, str) or not content.strip():
            continue
        turns.append(
            {
                "role": role,
                "content": content,
                "at": str(item.get("at") or ""),
                "engine": str(item.get("engine") or ""),
            }
        )
    return turns[-MAX_TURNS:]


def append_turn(case_dir: str | Path, role: str, content: str, *, engine: str = "") -> list[dict[str, str]]:
    text = content.strip()
    if role not in ROLES:
        raise BugCompassError(f"未知的对话角色：{role}")
    if not text:
        raise BugCompassError("对话内容不能为空。")
    turns = load_turns(case_dir)
    turns.append({"role": role, "content": text, "at": utc_now(), "engine": engine})
    turns = turns[-MAX_TURNS:]
    _write(case_dir, turns)
    return turns


def clear_turns(case_dir: str | Path) -> None:
    _write(case_dir, [])


def _write(case_dir: str | Path, turns: list[dict[str, str]]) -> None:
    path = conversation_path(case_dir)
    payload = {"schema_version": 1, "updated_at": utc_now(), "turns": turns}
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        raise BugCompassError(f"无法保存追问记录：{exc}") from exc


def recent_messages(turns: list[dict[str, str]], limit: int = CONTEXT_TURNS) -> list[dict[str, str]]:
    """把落盘的对话裁成引擎要的消息数组（纯函数，便于测试）。"""

    usable = [
        {"role": item["role"], "content": item["content"]}
        for item in turns
        if item.get("role") in ROLES and str(item.get("content") or "").strip()
    ]
    return usable[-limit:] if limit > 0 else []


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "\n…（已截断）"


def _dicts(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """取出对象数组：键缺失、写成 null、或混进非对象时都不该让简报炸掉。"""

    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def case_brief(investigation: dict[str, Any], issue_text: str = "", *, limit: int = BRIEF_LIMIT) -> str:
    """把当前案件压成一段简报，让追问引擎答得有据可依。

    只读 investigation 字典，不碰文件——所以既能给大模型 API 用，也能给 Codex 用。
    """

    data = investigation if isinstance(investigation, dict) else {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    parts: list[str] = []

    if issue_text.strip():
        parts.append("# 原始问题\n" + _clip(issue_text, ISSUE_LIMIT))

    fields = (
        ("问题", summary.get("problem")),
        ("预期行为", summary.get("expected_behavior")),
        ("实际行为", summary.get("actual_behavior")),
    )
    rendered = [f"- {name}：{str(value).strip()}" for name, value in fields if str(value or "").strip()]
    if rendered:
        parts.append("# 问题摘要\n" + "\n".join(rendered))

    hypotheses = _dicts(data, "hypotheses")
    if hypotheses:
        lines = []
        for index, item in enumerate(hypotheses):
            mark = chr(0x2460 + index) if index < 10 else f"({index + 1})"
            status = str(item.get("status") or "active")
            status_text = {"rejected": "，已被用户否定", "supported": "，已支持", "weakened": "，已削弱"}.get(status, "")
            line = f"{mark} [{item.get('priority', '未知')}] {item.get('title', '未命名路径')}{status_text}：{item.get('claim', '')}"
            if str(item.get("next_step") or "").strip():
                line += f"\n   下一步：{item['next_step']}"
            lines.append(line)
        parts.append("# 三条调查路径\n" + "\n".join(lines))

    for title, kind in (("已观察事实", "fact"), ("推测", "inference")):
        values = [
            str(item.get("statement") or "").strip() for item in _dicts(data, "evidence") if item.get("kind") == kind
        ]
        if any(values):
            parts.append(f"# {title}\n" + "\n".join(f"- {value}" for value in values if value))

    unknowns = [str(item.get("question") or "").strip() for item in _dicts(data, "unknowns")]
    if any(unknowns):
        parts.append("# 未知信息\n" + "\n".join(f"- {value}" for value in unknowns if value))

    experiments = _dicts(data, "suggested_experiments")
    if experiments:
        lines = []
        for item in experiments:
            command = item.get("command")
            command_text = " ".join(str(part) for part in command) if isinstance(command, list) else ""
            line = f"- {item.get('id', '?')}（{item.get('status', '未知')}）"
            if command_text:
                line += f" {command_text}"
            if str(item.get("result") or "").strip():
                line += f" → {item['result']}"
            lines.append(line)
        parts.append("# 实验\n" + "\n".join(lines))

    conclusion = data.get("conclusion")
    if isinstance(conclusion, dict) and str(conclusion.get("statement") or "").strip():
        chosen = next(
            (item.get("title", "") for item in hypotheses if item.get("id") == conclusion.get("hypothesis_id")),
            "",
        )
        parts.append(
            "# 用户已经写下的结论\n"
            f"- 采信路径：{chosen or conclusion.get('hypothesis_id', '')}\n"
            f"- 根因判断：{conclusion['statement']}"
        )

    return _clip("\n\n".join(parts), limit) if parts else "（案件还没有调查结果）"
