"""崩溃日志与诊断包导出。

硬约束（对应"不要把用户的 API key 或完整源码打进诊断包"）：

* 诊断包是 zip，里面**只有**日志、环境摘要和脱敏后的结构信息；
* 绝不包含：源码文件、Blender 仓库内容、练习答案、问题描述正文、
  环境变量取值、可执行文件；
* 所有文本在写入前都会经过 :func:`redact`；
* :func:`verify_bundle` 会在导出后自检（测试里也会跑），
  一旦发现疑似密钥或源码文件就直接失败。
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .resources import logs_dir, runtime_info, user_root

MAX_LOG_LINES = 400
MAX_LINE_CHARS = 2000

#: 疑似密钥的模式；命中后整段替换为占位符。
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}")),
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("key_assignment", re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password|passwd|authorization)\b\s*[:=]\s*[\"']?[^\s\"',}]{6,}")),
    ("long_hex", re.compile(r"\b[0-9a-fA-F]{32,}\b")),
)

EMAIL_PATTERN = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

#: 环境变量：只记录“名字 + 是否存在”，绝不记录取值。
SECRET_ENV_NAMES = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "CODEX_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
)

#: 诊断包里不允许出现的扩展名（源码/构建产物）。
FORBIDDEN_SUFFIXES = {
    ".py", ".pyw", ".c", ".cc", ".cpp", ".h", ".hpp", ".cu", ".rs", ".go", ".java",
    ".md", ".blend", ".exe", ".dll", ".pyd", ".so", ".dylib",
}


def redact_home(text: str) -> str:
    """把用户主目录替换成 ``~`` / ``%USERPROFILE%``。"""
    home = str(Path.home())
    if home and home in text:
        text = text.replace(home, "~" if os.name != "nt" else "%USERPROFILE%")
    if os.name == "nt":
        text = re.sub(r"[A-Za-z]:\\+Users\\+[^\\\s]+", r"%USERPROFILE%", text)
    else:
        text = re.sub(r"/(?:home|Users)/[^/\s]+", r"~", text)
    return text


def redact(text: str) -> str:
    """脱敏：密钥、令牌、邮箱、主目录路径。"""
    if not text:
        return text
    for name, pattern in SECRET_PATTERNS:
        text = pattern.sub(f"[REDACTED:{name}]", text)
    text = EMAIL_PATTERN.sub("[REDACTED:email]", text)
    return redact_home(text)


def install_crash_handler() -> None:
    """安装全局异常处理（线程异常 + Tk 回调异常写同一份日志）。"""
    if getattr(sys, "_bugcompass_crash_handler", False):
        return
    setattr(sys, "_bugcompass_crash_handler", True)

    def handle(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        write_crash_log("".join(traceback.format_exception(exc_type, exc, tb)))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = handle

    def threading_handler(args: Any) -> None:
        if args.exc_type is not None:
            write_crash_log(
                "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
            )

    try:  # pragma: no cover - Python 3.8+
        import threading

        threading.excepthook = threading_handler
    except Exception:
        pass


def install_tk_handler(root: Any) -> None:
    """把 Tk 回调里的异常也写进崩溃日志（否则 Tk 只在终端打印，安装版看不到）。"""
    def report(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        write_crash_log("Tk callback exception:\n" + text)
        sys.__excepthook__(exc_type, exc, tb)

    try:
        root.report_callback_exception = report
    except Exception:  # pragma: no cover
        pass


def write_crash_log(text: str) -> Path:
    directory = logs_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = directory / f"crash-{stamp}.log"
    target.write_text(_truncate(redact(text)), encoding="utf-8", errors="replace")
    return target


def _truncate(text: str) -> str:
    lines = text.splitlines()[-MAX_LOG_LINES:]
    return "\n".join(line[:MAX_LINE_CHARS] for line in lines)


def environment_summary(root: Any | None = None) -> dict[str, Any]:
    info = runtime_info()
    info["timezone"] = str(datetime.now(timezone.utc).astimezone().tzinfo)
    try:
        import tkinter

        info["tkinter_version"] = str(tkinter.TkVersion)
    except Exception:
        info["tkinter_version"] = "unavailable"
    if root is not None:
        try:
            from .dpi import describe

            info["display"] = describe(root)
        except Exception:  # pragma: no cover
            info["display"] = "unavailable"
    info["env"] = {
        name: {"present": bool(os.environ.get(name))} for name in SECRET_ENV_NAMES
    }
    info["note"] = "只记录环境变量是否存在，不记录取值。"
    return info


def export_bundle(
    dest_dir: str | Path,
    *,
    root: Any | None = None,
    case_dir: str | Path | None = None,
    include_case_structure: bool = False,
) -> Path:
    """导出诊断包（zip）。

    :param include_case_structure: 需要用户明确勾选。只包含案件的结构化计数，
        不包含问题描述、证据正文或任何源码内容。
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = dest / f"bugcompass-diagnostics-{stamp}.zip"

    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "bugcompass_version": __version__,
        "contents": [],
        "privacy": {
            "redaction": "密钥、令牌、邮箱、用户目录路径已被替换（见 SECRET_PATTERNS）",
            "excludes": ["源码文件", "Blender 仓库内容", "练习答案", "问题描述正文", "环境变量取值"],
        },
    }

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        archive.writestr(
            "environment.json",
            json.dumps(environment_summary(root), ensure_ascii=False, indent=2) + "\n",
        )
        from .settings import load_settings

        settings = load_settings()
        settings.pop("telemetry_endpoint", None)
        archive.writestr("settings.json", json.dumps(settings, ensure_ascii=False, indent=2) + "\n")

        log_files = sorted(logs_dir().glob("*.log"))[-10:]
        for path in log_files:
            archive.writestr(
                f"logs/{path.name}",
                _truncate(redact(path.read_text(encoding="utf-8", errors="replace"))),
            )

        if include_case_structure and case_dir is not None:
            archive.writestr("case-structure.json", json.dumps(_case_structure(case_dir), ensure_ascii=False, indent=2) + "\n")

    verify_bundle(target)
    return target


def _case_structure(case_dir: str | Path) -> dict[str, Any]:
    """案件的结构化摘要：只有计数与状态，没有正文。"""
    case_dir = Path(case_dir)
    structure: dict[str, Any] = {"case_id": case_dir.name, "files": []}
    if not case_dir.is_dir():
        return structure
    for path in sorted(case_dir.iterdir()):
        if path.name in {"issue-original.md", "intake.md", "answers.json"} or path.suffix in FORBIDDEN_SUFFIXES:
            structure["files"].append({"name": path.name, "included": False, "reason": "excluded-by-policy"})
            continue
        if path.is_dir():
            structure["files"].append({"name": path.name, "type": "dir", "entries": len(list(path.iterdir()))})
            continue
        structure["files"].append({"name": path.name, "type": "file", "bytes": path.stat().st_size, "included": False})
    investigation = case_dir / "investigation.json"
    if investigation.is_file():
        try:
            data = json.loads(investigation.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            structure["counts"] = {
                "hypotheses": len(data.get("hypotheses") or []),
                "evidence": len(data.get("evidence") or []),
                "unknowns": len(data.get("unknowns") or []),
                "experiments": len(data.get("suggested_experiments") or []),
                "causal_nodes": len((data.get("causal_graph") or {}).get("nodes") or []),
                "causal_edges": len((data.get("causal_graph") or {}).get("edges") or []),
            }
    return structure


def verify_bundle(path: str | Path) -> None:
    """自检：诊断包里不能有疑似密钥，也不能有源码文件。导出后与测试里都会调用。"""
    target = Path(path)
    if not target.is_file():
        raise AssertionError(f"诊断包不存在：{target}")
    with zipfile.ZipFile(target) as archive:
        for info in archive.namelist():
            suffix = Path(info).suffix.lower()
            if suffix in FORBIDDEN_SUFFIXES:
                raise AssertionError(f"诊断包不应包含源码/文档类文件：{info}")
            content = archive.read(info).decode("utf-8", errors="replace")
            for name, pattern in SECRET_PATTERNS:
                if pattern.search(content):
                    raise AssertionError(f"诊断包 {info} 疑似包含未脱敏的 {name}")
            if EMAIL_PATTERN.search(content):
                raise AssertionError(f"诊断包 {info} 疑似包含未脱敏的邮箱")


def recent_crash_logs(limit: int = 10) -> list[Path]:
    return sorted(logs_dir().glob("crash-*.log"))[-limit:]


def user_data_root() -> Path:
    return user_root()
