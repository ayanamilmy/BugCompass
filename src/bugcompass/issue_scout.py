"""AI Issue 筛选（Issue Scout）：从 Blender tracker 抓取候选，用大模型打分排序。

网络边界（与 AGENTS.md 约束一致）：

- **只在用户点击「开始筛选」时**访问 projects.blender.org 的公开 API（无需登录）；
- AI 评分走用户自己配置的大模型引擎（OpenAI 兼容端点，见 llm.py），
  纯文本进出，不需要工具循环，便宜且快；
- 抓取与评分结果缓存在 ``~/.bugcompass/scout/``，断网时可查看上次筛选；
- 评分眼光可私有化：``~/.bugcompass/scout-prompt.md`` 存在时，其内容会附加到
  评分指令之后（这份文件属于用户，不进仓库）。

数据来源（2026-09 实测验证）：Gitea API
``/api/v1/repos/blender/blender/issues?state=open&type=issues&limit=50&page=N``，
标签形如 ``Module/Render & Cycles``、``Type/Bug``、``Status/Needs Info from Developers``。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .llm import LLMError, LLMProviderConfig, chat_completion, resolve_api_key, ssl_context
from .resources import user_root

ProgressCallback = Callable[[str], None]

TRACKER_API = "https://projects.blender.org/api/v1/repos/blender/blender/issues"
PAGE_SIZE = 50
MAX_ISSUES_HARD_CAP = 150
FETCH_TIMEOUT_SECONDS = 30

#: 16 个真实存在的模块标签（实测于 2026-09；API 拉取失败时作为兜底选项）。
FALLBACK_MODULES: tuple[str, ...] = (
    "Module/Animation & Rigging",
    "Module/Asset System",
    "Module/Core",
    "Module/Grease Pencil",
    "Module/Modeling",
    "Module/Nodes & Physics",
    "Module/Pipeline & IO",
    "Module/Platforms & Builds",
    "Module/Python API",
    "Module/Render & Cycles",
    "Module/Sculpt, Paint & Texture",
    "Module/Triaging",
    "Module/User Interface",
    "Module/VFX & Video",
    "Module/Viewport & EEVEE",
)

TYPE_LABELS: tuple[str, ...] = ("Type/Bug", "Type/Report", "Type/Known Issue", "Type/To Do")
EXCLUDED_STATUS: tuple[str, ...] = ("Status/Archived", "Status/Duplicate", "Status/Resolved")
GOOD_FIRST_LABEL = "Meta/Good First Issue"
#: 评论里出现指向本仓库 PR 的链接 → 几乎可以断定已有人在做。
#: 兼容完整 URL（projects.blender.org/blender/blender/-/pulls/N）与裸引用（blender/blender/pulls/N）。
PR_LINK_PATTERN = re.compile(r"blender/blender/(?:-/)?pulls?/\d+", re.IGNORECASE)
COMMENTS_API = "https://projects.blender.org/api/v1/repos/blender/blender/issues"
COMMENTS_PAGE_SIZE = 50
COMMENTS_KEEP_PER_ISSUE = 20
COMMENT_BODY_CHARS = 600
ENRICH_MAX_WORKERS = 6
BODY_PREVIEW_CHARS = 3500
SCORING_BATCH_SIZE = 10


class ScoutError(Exception):
    """可安全展示给用户的筛选错误。"""


@dataclass
class IssueRecord:
    number: int
    title: str
    url: str
    body: str
    labels: list[str]
    created_at: str
    comments: int
    assignees: list[str] = field(default_factory=list)
    #: 最近评论（作者/正文片段/时间），由 enrich_with_comments 抓取，用于判断是否已有人接手。
    comments_data: list[dict[str, str]] = field(default_factory=list)

    @property
    def good_first(self) -> bool:
        return GOOD_FIRST_LABEL in self.labels

    @property
    def module_labels(self) -> list[str]:
        return [label for label in self.labels if label.startswith("Module/")]

    @property
    def type_labels(self) -> list[str]:
        return [label for label in self.labels if label.startswith("Type/")]

    @classmethod
    def from_api(cls, raw: Any) -> "IssueRecord | None":
        if not isinstance(raw, dict):
            return None
        try:
            number = int(raw.get("number", 0))
        except (TypeError, ValueError):
            return None
        if number <= 0:
            return None
        labels = [str(label.get("name")) for label in raw.get("labels") or [] if isinstance(label, dict) and label.get("name")]
        assignees = [
            str(person.get("login"))
            for person in raw.get("assignees") or []
            if isinstance(person, dict) and person.get("login")
        ]
        return cls(
            number=number,
            title=str(raw.get("title") or f"Issue #{number}"),
            url=str(raw.get("html_url") or f"https://projects.blender.org/blender/blender/issues/{number}"),
            body=str(raw.get("body") or "")[:BODY_PREVIEW_CHARS],
            labels=labels,
            created_at=str(raw.get("created_at") or ""),
            comments=int(raw.get("comments") or 0),
            assignees=assignees,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "url": self.url,
            "body": self.body,
            "labels": self.labels,
            "created_at": self.created_at,
            "comments": self.comments,
            "assignees": self.assignees,
            "comments_data": self.comments_data,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IssueRecord":
        return cls(
            number=int(data.get("number", 0)),
            title=str(data.get("title", "")),
            url=str(data.get("url", "")),
            body=str(data.get("body", "")),
            labels=[str(item) for item in data.get("labels", [])],
            created_at=str(data.get("created_at", "")),
            comments=int(data.get("comments", 0)),
            assignees=[str(item) for item in data.get("assignees", [])],
            comments_data=[
                {str(k): str(v) for k, v in item.items()}
                for item in data.get("comments_data", [])
                if isinstance(item, dict)
            ],
        )


@dataclass
class IssueScore:
    number: int
    score: int | None  # 1-10；None = 该条未评分（批次失败等）
    difficulty: str  # 入门 / 进阶 / 挑战 / 未知
    reason: str
    taken: bool = False  # 是否已有人接手（指派/PR 链接/AI 判断）
    taken_evidence: str = ""  # 判断依据（供用户核对）

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "score": self.score,
            "difficulty": self.difficulty,
            "reason": self.reason,
            "taken": self.taken,
            "taken_evidence": self.taken_evidence,
        }


# ---------------------------------------------------------------------- 抓取
def fetch_open_issues(limit: int = 50, progress: ProgressCallback | None = None) -> list[IssueRecord]:
    """抓取最新的 open issue（分页，每页 50）。只在用户主动发起时调用。"""
    limit = max(10, min(MAX_ISSUES_HARD_CAP, int(limit)))
    records: list[IssueRecord] = []
    page = 1
    while len(records) < limit and page <= (MAX_ISSUES_HARD_CAP // PAGE_SIZE + 1):
        url = f"{TRACKER_API}?state=open&type=issues&limit={PAGE_SIZE}&page={page}"
        if progress:
            progress(f"正在抓取 Blender tracker 第 {page} 页……")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "BugCompass", "Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS, context=ssl_context()) as response:  # noqa: S310 - 固定公开端点
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if "CERTIFICATE_VERIFY_FAILED" in str(reason) or "certificate verify failed" in str(reason):
                from .llm import _SSL_GUIDANCE

                raise ScoutError(f"无法访问 projects.blender.org：{reason}\n{_SSL_GUIDANCE}") from exc
            raise ScoutError(f"无法访问 projects.blender.org：{reason}\n请检查网络；也可以先查看上次筛选的离线缓存。") from exc
        except json.JSONDecodeError as exc:
            raise ScoutError(f"tracker 返回了无法解析的内容：{str(exc)[:120]}") from exc
        if not isinstance(payload, list):
            raise ScoutError("tracker 返回格式异常（不是列表）。")
        batch = [record for raw in payload if (record := IssueRecord.from_api(raw)) is not None]
        records.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        page += 1
    return records[:limit]


def enrich_with_comments(
    records: list[IssueRecord],
    progress: ProgressCallback | None = None,
) -> list[IssueRecord]:
    """抓取有评论的 issue 的评论区（并行、限流），用于判断是否已有人接手。

    issue 列表接口自带 ``comments`` 计数——计数为 0 的直接跳过，不浪费请求。
    单个 issue 抓取失败只影响它自己的判断，不影响整批。
    """
    from concurrent.futures import ThreadPoolExecutor

    targets = [record for record in records if record.comments > 0]
    if not targets:
        return records
    if progress:
        progress(f"正在抓取 {len(targets)} 个 issue 的评论区（判断是否已有人接手）……")

    def fetch_one(record: IssueRecord) -> None:
        url = f"{COMMENTS_API}/{record.number}/comments?limit={COMMENTS_PAGE_SIZE}&page=1"
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "BugCompass", "Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS, context=ssl_context()) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, list):
            return
        record.comments_data = [
            {
                "author": str((item.get("user") or {}).get("login", "")),
                "body": str(item.get("body") or "")[:COMMENT_BODY_CHARS],
                "created_at": str(item.get("created_at") or ""),
            }
            for item in payload
            if isinstance(item, dict)
        ][:COMMENTS_KEEP_PER_ISSUE]

    with ThreadPoolExecutor(max_workers=ENRICH_MAX_WORKERS) as pool:
        list(pool.map(fetch_one, targets))
    return records


def deterministic_taken(record: IssueRecord) -> tuple[bool, str]:
    """不需要 AI 的占用判断：已指派 / 评论里出现 PR 链接。"""
    if record.assignees:
        return True, f"已指派给 {', '.join(record.assignees)}"
    for comment in record.comments_data:
        if PR_LINK_PATTERN.search(comment.get("body", "")):
            author = comment.get("author") or "有人"
            return True, f"评论中出现 PR 链接（{author}）"
    return False, ""


def filter_issues(
    records: list[IssueRecord],
    *,
    modules: list[str] | None = None,
    types: list[str] | None = None,
    good_first_only: bool = False,
) -> list[IssueRecord]:
    """本地过滤：模块（任一命中）、类型（任一命中）、Good First Issue、剔除已归档/重复/已解决。"""
    wanted_modules = {m for m in (modules or []) if m}
    wanted_types = {t for t in (types or []) if t}
    result: list[IssueRecord] = []
    for record in records:
        if any(label in EXCLUDED_STATUS for label in record.labels):
            continue
        if good_first_only and not record.good_first:
            continue
        if wanted_modules and not (set(record.module_labels) & wanted_modules):
            continue
        if wanted_types and not (set(record.type_labels) & wanted_types):
            continue
        result.append(record)
    return result


# ---------------------------------------------------------------------- 评分
SCORING_SYSTEM = (
    "你是 Blender 项目的 issue 评估员，帮助刚上手的贡献者挑选值得调查的 Bug。"
    "对给出的每个 issue，按「调查价值」打 1-10 分：\n"
    "- 高分：复现步骤清晰、影响明确、根因大概率能在源码里定位、适合用工具链调查；\n"
    "- 低分：信息残缺、依赖外部文件/硬件、纯设计讨论、或需要 deep 系统知识才能动手。\n"
    "同时判断难度（入门/进阶/挑战）并用不超过 40 字的中文说明理由。\n\n"
    "还要判断该 issue 是否已有人接手（taken）：\n"
    "- true 的情形：评论里出现修复 PR/补丁链接、有人认领（I'll fix / working on a patch / "
    "I'll take this 等）、维护者明确说在处理、或讨论显示已有代码在评审；\n"
    "- false 的情形：只有复现确认（I can reproduce）、提问、需求讨论、或与修复无关的闲聊；\n"
    "- 不确定时给 false。taken_evidence 用不超过 30 字引用或概括证据。\n"
    "只输出 JSON，格式：{\"results\": [{\"number\": 编号, \"score\": 分数, "
    "\"difficulty\": \"入门|进阶|挑战\", \"reason\": \"理由\", "
    "\"taken\": true|false, \"taken_evidence\": \"证据\"}]}，不要 Markdown 围栏。"
)


def custom_prompt_path() -> Path:
    return user_root() / "scout-prompt.md"


def build_scoring_messages(batch: list[IssueRecord]) -> list[dict[str, str]]:
    custom = ""
    path = custom_prompt_path()
    if path.is_file():
        try:
            custom = "\n\n# 用户的补充评估标准（优先级高于以上默认规则）\n" + path.read_text(encoding="utf-8")[:4000]
        except OSError:
            custom = ""
    lines = []
    for record in batch:
        labels = ", ".join(record.labels) or "无标签"
        body = re.sub(r"\s+", " ", record.body or "")[:1200]
        assignees = ", ".join(record.assignees) if record.assignees else "无"
        comments = ""
        if record.comments_data:
            excerpts = []
            for comment in reversed(record.comments_data[-8:]):  # 最新在后，取末尾 8 条
                author = comment.get("author") or "?"
                text = re.sub(r"\s+", " ", comment.get("body", ""))[:200]
                excerpts.append(f"{author}: {text}")
            comments = "\n评论（最新在最后）：\n" + "\n".join(excerpts)
        lines.append(
            f"#{record.number}｜{record.title}\n标签：{labels}\n指派：{assignees}\n正文：{body or '（无正文）'}{comments}"
        )
    return [
        {"role": "system", "content": SCORING_SYSTEM + custom},
        {"role": "user", "content": "请评估以下 issue：\n\n" + "\n\n".join(lines)},
    ]


def score_issues(
    provider: LLMProviderConfig,
    records: list[IssueRecord],
    *,
    progress: ProgressCallback | None = None,
) -> dict[int, IssueScore]:
    """分批让大模型打分。单批失败不炸整体——该批 issue 记为未评分。"""
    from .llm_runner import LLMInvestigator  # 复用 JSON 提取器

    try:
        api_key = resolve_api_key(provider)
    except LLMError:
        raise
    scores: dict[int, IssueScore] = {}
    batches = [records[i : i + SCORING_BATCH_SIZE] for i in range(0, len(records), SCORING_BATCH_SIZE)]
    for index, batch in enumerate(batches, 1):
        if progress:
            progress(f"AI 正在评分（{index}/{len(batches)} 批，每批 {len(batch)} 个）……")
        try:
            response = chat_completion(provider, build_scoring_messages(batch), api_key=api_key, temperature=0.1, max_tokens=2000)
            data = LLMInvestigator._extract_json(response.content)
            raw_results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(raw_results, list):
                raise ValueError("缺少 results 数组")
            valid_numbers = {record.number for record in batch}
            for raw in raw_results:
                if not isinstance(raw, dict):
                    continue
                try:
                    number = int(raw.get("number"))
                except (TypeError, ValueError):
                    continue
                if number not in valid_numbers:
                    continue
                try:
                    score_value: int | None = max(1, min(10, int(raw.get("score"))))
                except (TypeError, ValueError):
                    score_value = None
                difficulty = str(raw.get("difficulty") or "未知")
                if difficulty not in {"入门", "进阶", "挑战"}:
                    difficulty = "未知"
                taken_raw = raw.get("taken")
                ai_taken = taken_raw is True  # 只有显式 true 才算
                ai_evidence = str(raw.get("taken_evidence") or "")[:60]
                record = next((item for item in batch if item.number == number), None)
                det_taken, det_evidence = (deterministic_taken(record) if record else (False, ""))
                scores[number] = IssueScore(
                    number=number,
                    score=score_value,
                    difficulty=difficulty,
                    reason=str(raw.get("reason") or "")[:120],
                    taken=ai_taken or det_taken,
                    taken_evidence=ai_evidence or det_evidence,
                )
        except (LLMError, ValueError, KeyError, json.JSONDecodeError) as exc:
            for record in batch:
                scores[record.number] = IssueScore(number=record.number, score=None, difficulty="未知", reason=f"本批评分失败：{str(exc)[:80]}")
    return scores


# ---------------------------------------------------------------------- 缓存
def scout_dir() -> Path:
    path = user_root() / "scout"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_scan(records: list[IssueRecord], scores: dict[int, IssueScore], filters: dict[str, Any]) -> Path:
    target = scout_dir() / "last-scan.json"
    payload = {
        "schema_version": 1,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "filters": filters,
        "issues": [record.to_dict() for record in records],
        "scores": [score.to_dict() for score in scores.values()],
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def load_last_scan() -> dict[str, Any] | None:
    target = scout_dir() / "last-scan.json"
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def scan_records_from_cache(cache: dict[str, Any]) -> list[IssueRecord]:
    issues = cache.get("issues")
    if not isinstance(issues, list):
        return []
    records: list[IssueRecord] = []
    for raw in issues:
        if isinstance(raw, dict):
            records.append(IssueRecord.from_dict(raw))
    return records


def scan_scores_from_cache(cache: dict[str, Any]) -> dict[int, IssueScore]:
    scores_raw = cache.get("scores")
    if not isinstance(scores_raw, list):
        return {}
    scores: dict[int, IssueScore] = {}
    for raw in scores_raw:
        if not isinstance(raw, dict):
            continue
        try:
            number = int(raw.get("number"))
        except (TypeError, ValueError):
            continue
        score_value = raw.get("score")
        scores[number] = IssueScore(
            number=number,
            score=int(score_value) if isinstance(score_value, (int, float)) else None,
            difficulty=str(raw.get("difficulty") or "未知"),
            reason=str(raw.get("reason") or ""),
            taken=bool(raw.get("taken", False)),
            taken_evidence=str(raw.get("taken_evidence") or ""),
        )
    return scores


# ---------------------------------------------------------------------- 导出
def issue_to_bug_text(record: IssueRecord, score: IssueScore | None = None) -> str:
    """把筛出的 issue 转成新建案件的 Bug 描述（Markdown）。"""
    score_line = ""
    if score is not None:
        score_line = f"\n> AI 筛选评分：{score.score if score.score is not None else '未评分'}/10 · {score.difficulty} · {score.reason}\n"
        if score.taken:
            score_line += f">\n> ⚠️ 筛选时发现可能已有人接手（{score.taken_evidence or '证据见 tracker'}）。动手前先到上面的链接确认最新状态，避免重复劳动。\n"
    return (
        f"# {record.title}\n"
        f"\n来源：{record.url}（Blender tracker #{record.number}，创建于 {record.created_at[:10]}）\n"
        f"标签：{', '.join(record.labels) or '无'}{score_line}\n"
        "## 问题描述\n\n"
        f"{record.body or '（tracker 正文为空，请点来源链接查看讨论。）'}\n"
        "\n（由 BugCompass「AI 挑选 Issue」导入）"
    )


def export_markdown(records: list[IssueRecord], scores: dict[int, IssueScore], dest: Path) -> Path:
    ranked = sorted(records, key=lambda r: (scores.get(r.number).score is None, -(scores.get(r.number).score or 0)))
    lines = ["# AI Issue 筛选结果", "", f"导出时间：{datetime.now().isoformat(timespec='seconds')}", ""]
    for rank, record in enumerate(ranked, 1):
        score = scores.get(record.number)
        value = f"{score.score}/10" if score and score.score is not None else "未评分"
        reason = score.reason if score else ""
        taken_mark = " · **已有人接手**" if score and score.taken else ""
        gfi_mark = " · ★ Good First Issue" if record.good_first else ""
        lines.append(f"{rank}. **{value}** · {score.difficulty if score else '未知'} · [#{record.number} {record.title}]({record.url}){gfi_mark}{taken_mark}")
        if reason:
            lines.append(f"   - {reason}")
        if score and score.taken and score.taken_evidence:
            lines.append(f"   - 占用证据：{score.taken_evidence}")
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest
