"""每个 Case 的运行指标：模型、耗时、工具轮次、token 与估计成本。

数据全部存在本地 ``<case>/metrics.json``。Codex CLI 的原始事件日志放在
``<case>/codex-runs/*.jsonl``，本模块只做**聚合统计**，不复制任何正文内容。

关于成本：BugCompass 不内置任何模型价格表（无法保证准确，也不该替用户拍板）。
价格来自用户可编辑的 ``pricing.json``（工作区或用户目录），未配置时成本显示为
``None``（界面上显示“未配置单价”），绝不凭空估算。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .resources import user_root

PRICING_FILENAME = "pricing.json"

PRICING_TEMPLATE: dict[str, Any] = {
    "_comment": (
        "每 100 万 token 的单价（美元）。BugCompass 不内置价格表，请按你的实际合同填写；"
        "未列出的模型不会估算成本。cached_input 是命中缓存的输入单价，可留空表示与 input 同价。"
    ),
    "models": {
        "示例-model": {"input": 1.0, "cached_input": 0.1, "output": 4.0, "currency": "USD"},
    },
}


@dataclass
class RunMetrics:
    """一次 Codex 运行的聚合结果。"""

    case_id: str = ""
    model: str = ""
    reasoning_effort: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    turns: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float | None = None
    status: str = "unknown"
    log: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CaseMetrics:
    case_id: str
    runs: list[RunMetrics] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return round(sum(run.duration_seconds for run in self.runs), 3)

    @property
    def tool_calls(self) -> int:
        return sum(run.tool_calls for run in self.runs)

    @property
    def turns(self) -> int:
        return sum(run.turns for run in self.runs)

    @property
    def input_tokens(self) -> int:
        return sum(run.input_tokens for run in self.runs)

    @property
    def cached_input_tokens(self) -> int:
        return sum(run.cached_input_tokens for run in self.runs)

    @property
    def output_tokens(self) -> int:
        return sum(run.output_tokens for run in self.runs)

    @property
    def estimated_cost_usd(self) -> float | None:
        values = [run.estimated_cost_usd for run in self.runs if run.estimated_cost_usd is not None]
        if len(values) != len(self.runs):  # 有任一运行缺单价 → 总成本不假装完整
            return None
        return round(sum(values), 6)

    @property
    def models(self) -> list[str]:
        return sorted({run.model for run in self.runs if run.model})

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "models": self.models,
            "runs": [run.to_dict() for run in self.runs],
            "totals": {
                "duration_seconds": self.duration_seconds,
                "turns": self.turns,
                "tool_calls": self.tool_calls,
                "input_tokens": self.input_tokens,
                "cached_input_tokens": self.cached_input_tokens,
                "output_tokens": self.output_tokens,
                "estimated_cost_usd": self.estimated_cost_usd,
            },
        }


# --------------------------------------------------------------------- 价格表
def pricing_path(workspace: str | Path | None = None) -> Path:
    if workspace:
        candidate = Path(workspace).expanduser() / PRICING_FILENAME
        if candidate.is_file():
            return candidate
    return user_root() / PRICING_FILENAME


def load_pricing(workspace: str | Path | None = None) -> dict[str, Any]:
    path = pricing_path(workspace)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    models = data.get("models") if isinstance(data, dict) else None
    return models if isinstance(models, dict) else {}


def write_pricing_template(workspace: str | Path | None = None) -> Path:
    path = user_root() / PRICING_FILENAME if not workspace else Path(workspace).expanduser() / PRICING_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(PRICING_TEMPLATE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def estimate_cost(
    model: str,
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    pricing: dict[str, Any] | None = None,
) -> float | None:
    """按用户价格表估算成本；未配置该模型时返回 None（不猜）。"""
    entry = (pricing or {}).get(model)
    if not isinstance(entry, dict):
        return None
    try:
        price_in = float(entry.get("input"))
        price_out = float(entry.get("output"))
        cached = entry.get("cached_input")
        price_cached = float(cached) if cached is not None else price_in
    except (TypeError, ValueError):
        return None
    billable_input = max(0, input_tokens - cached_input_tokens)
    cost = (billable_input / 1_000_000) * price_in
    cost += (cached_input_tokens / 1_000_000) * price_cached
    cost += (output_tokens / 1_000_000) * price_out
    return round(cost, 6)


# ------------------------------------------------------------------- 事件解析
def parse_codex_log(path: str | Path) -> dict[str, int]:
    """解析一次运行的 JSONL 事件流，返回轮次/工具调用/token 计数。

    只读取数字字段，任何无法识别的行都会被忽略（Codex 的事件格式会演进）。
    """
    counts = {"turns": 0, "tool_calls": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    target = Path(path)
    if not target.is_file():
        return counts
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return counts
    for line in content.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "turn.started":
            counts["turns"] += 1
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "command_execution" and event_type == "item.completed":
            counts["tool_calls"] += 1
        usage = event.get("usage")
        if not isinstance(usage, dict):
            usage = event.get("item", {}).get("usage") if isinstance(event.get("item"), dict) else None
        if isinstance(usage, dict):
            counts["input_tokens"] += _as_int(usage.get("input_tokens"))
            counts["cached_input_tokens"] += _as_int(
                usage.get("cached_input_tokens", usage.get("input_tokens_details", {}).get("cached_tokens"))
                if isinstance(usage.get("input_tokens_details"), dict)
                else usage.get("cached_input_tokens")
            )
            counts["output_tokens"] += _as_int(usage.get("output_tokens"))
    return counts


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def run_metrics_from_summary(summary: dict[str, Any], case_dir: str | Path, *, pricing: dict[str, Any] | None = None) -> RunMetrics:
    """把 ``codex-last-run.json`` 与事件日志合并成一次运行的指标。"""
    case_dir = Path(case_dir)
    counts = {"turns": 0, "tool_calls": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    log_rel = summary.get("log_path")
    if isinstance(log_rel, str) and log_rel:
        counts = parse_codex_log(case_dir / log_rel)
    started = _parse_iso(str(summary.get("started_at", "")))
    finished = _parse_iso(str(summary.get("finished_at", "")))
    duration = (finished - started).total_seconds() if started and finished else 0.0
    if duration <= 0 and (summary.get("duration_seconds") is not None):
        duration = float(summary.get("duration_seconds") or 0.0)
    model = str(summary.get("model", "") or "")
    if summary.get("cancelled"):
        status = "cancelled"
    elif summary.get("timed_out"):
        status = "timeout"
    elif int(summary.get("return_code", -1) or -1) == 0:
        status = "ok"
    else:
        status = "failed"
    return RunMetrics(
        case_id=str(summary.get("case_id", "")),
        model=model,
        reasoning_effort=str(summary.get("reasoning_effort", "") or ""),
        started_at=str(summary.get("started_at", "") or ""),
        finished_at=str(summary.get("finished_at", "") or ""),
        duration_seconds=round(max(0.0, duration), 3),
        turns=int(counts["turns"]),
        tool_calls=int(counts["tool_calls"]),
        input_tokens=int(counts["input_tokens"]),
        cached_input_tokens=int(counts["cached_input_tokens"]),
        output_tokens=int(counts["output_tokens"]),
        estimated_cost_usd=estimate_cost(
            model,
            input_tokens=counts["input_tokens"],
            cached_input_tokens=counts["cached_input_tokens"],
            output_tokens=counts["output_tokens"],
            pricing=pricing,
        ),
        status=status,
        log=str(log_rel or ""),
    )


# ------------------------------------------------------------------- Case 汇总
def collect_case_metrics(case_dir: str | Path, *, pricing: dict[str, Any] | None = None) -> CaseMetrics:
    """汇总一个 Case 到目前为止所有运行的指标（本地 JSON，不联网）。"""
    case_dir = Path(case_dir)
    case_id = case_dir.name
    metrics = CaseMetrics(case_id=case_id)
    summary_file = case_dir / "codex-last-run.json"
    if summary_file.is_file():
        try:
            summary = json.loads(summary_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            summary = None
        if isinstance(summary, dict):
            metrics.runs.append(run_metrics_from_summary(summary, case_dir, pricing=pricing))
    # 其余历史运行：每个 JSONL 日志单独统计（不受 codex-last-run.json 覆盖影响）
    covered = {run.log for run in metrics.runs}
    runs_dir = case_dir / "codex-runs"
    if runs_dir.is_dir():
        for log_path in sorted(runs_dir.glob("*.jsonl")):
            relative = log_path.relative_to(case_dir).as_posix()
            if relative in covered:
                continue
            counts = parse_codex_log(log_path)
            metrics.runs.append(
                RunMetrics(
                    case_id=case_id,
                    started_at=log_path.stem,
                    finished_at=log_path.stem,
                    turns=counts["turns"],
                    tool_calls=counts["tool_calls"],
                    input_tokens=counts["input_tokens"],
                    cached_input_tokens=counts["cached_input_tokens"],
                    output_tokens=counts["output_tokens"],
                    status="unknown",
                    log=relative,
                )
            )
    return metrics


def write_case_metrics(case_dir: str | Path, metrics: CaseMetrics) -> Path:
    case_dir = Path(case_dir)
    payload = metrics.to_dict()
    payload["schema_version"] = 1
    payload["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    target = case_dir / "metrics.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def read_case_metrics(case_dir: str | Path) -> dict[str, Any] | None:
    target = Path(case_dir) / "metrics.json"
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def format_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes}m{rest:02d}s"


def format_cost(value: float | None) -> str:
    if value is None:
        return "未配置单价"
    if value == 0:
        return "$0"
    if value < 0.01:
        return f"${value:.6f}"
    return f"${value:.4f}"
