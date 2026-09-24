"""跨 Case 搜索工作区中的文本文件。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


SEARCH_FILES = (
    "case.json",
    "investigation.json",
    "issue-original.md",
    "intake.md",
    "hypotheses.md",
    "evidence.md",
    "report.md",
)


def search_workspace(
    workspace_path: str | Path,
    query: str,
    *,
    regex: bool = False,
    limit_per_case: int = 20,
) -> dict[str, Any]:
    """逐行搜索每个 Case，返回命中和读取警告。

    ``cases`` 仅包含有命中的 Case，键为 Case ID；每条命中包含
    ``case_id``、``filename``、``line_number`` 和 ``line``。
    每个 Case 的命中数量上限由 ``limit_per_case`` 控制。
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("查询词不能为空。")
    if limit_per_case < 1:
        raise ValueError("每个 Case 的结果上限必须大于零。")

    if regex:
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as exc:
            raise ValueError("正则表达式无效。") from exc

        def matches(line: str) -> bool:
            return pattern.search(line) is not None

    else:
        terms = [term.casefold() for term in query.split()]

        def matches(line: str) -> bool:
            folded = line.casefold()
            return all(term in folded for term in terms)

    report: dict[str, Any] = {
        "query": query,
        "regex": regex,
        "cases": {},
        "warnings": [],
    }
    cases_dir = Path(workspace_path).expanduser() / "cases"
    if not cases_dir.exists():
        return report

    try:
        case_dirs = sorted(path for path in cases_dir.iterdir() if path.is_dir())
    except OSError:
        report["warnings"].append({"case_id": None, "filename": "cases", "message": "无法读取目录。"})
        return report

    for case_dir in case_dirs:
        case_id = case_dir.name
        hits: list[dict[str, Any]] = []
        for filename in SEARCH_FILES:
            path = case_dir / filename
            try:
                if not path.exists():
                    continue
                content = path.read_text(encoding="utf-8")
                if path.suffix == ".json":
                    json.loads(content)
            except (OSError, UnicodeError):
                report["warnings"].append(
                    {"case_id": case_id, "filename": filename, "message": "文件无法读取，已跳过。"}
                )
                continue
            except json.JSONDecodeError:
                report["warnings"].append(
                    {"case_id": case_id, "filename": filename, "message": "JSON 格式无效，已跳过。"}
                )
                continue

            if len(hits) >= limit_per_case:
                continue
            for line_number, line in enumerate(content.splitlines(), start=1):
                if matches(line):
                    hits.append(
                        {
                            "case_id": case_id,
                            "filename": filename,
                            "line_number": line_number,
                            "line": line,
                        }
                    )
                    if len(hits) >= limit_per_case:
                        break
        if hits:
            report["cases"][case_id] = hits
    return report


def format_report(report: dict[str, Any]) -> str:
    """将搜索结果排版为终端可读的简体中文。"""
    cases = report["cases"]
    count = sum(len(hits) for hits in cases.values())
    lines = [f"搜索词：{report['query']}", f"找到 {len(cases)} 个 Case，共 {count} 条结果。"]
    if not cases:
        lines.append("未找到匹配结果。")
    for case_id, hits in cases.items():
        lines.append(f"\nCase：{case_id}")
        for hit in hits:
            lines.append(f"  {hit['filename']}:{hit['line_number']}: {hit['line']}")
    if report["warnings"]:
        lines.append("\n警告：")
        for warning in report["warnings"]:
            location = warning["filename"]
            if warning["case_id"] is not None:
                location = f"{warning['case_id']}/{location}"
            lines.append(f"  {location}：{warning['message']}")
    return "\n".join(lines)


class _ChineseArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["add_help"] = False
        super().__init__(*args, **kwargs)
        self._positionals.title = "位置参数"
        self._optionals.title = "选项"
        self.add_argument("-h", "--help", action="help", help="显示帮助并退出")

    def format_usage(self) -> str:
        return super().format_usage().replace("usage:", "用法：", 1)

    def format_help(self) -> str:
        return super().format_help().replace("usage:", "用法：", 1)

    def error(self, message: str) -> None:
        message = message.replace("the following arguments are required:", "缺少必需参数：")
        message = message.replace("unrecognized arguments:", "无法识别的参数：")
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: 错误：{message}\n")


def main(argv: list[str] | None = None) -> int:
    parser = _ChineseArgumentParser(description="跨 Case 全文搜索")
    parser.add_argument("--workspace", required=True, metavar="路径", help="BugCompass 工作区路径")
    parser.add_argument("--regex", action="store_true", help="将查询词视为正则表达式")
    parser.add_argument("query", nargs="+", metavar="查询词", help="要搜索的词或正则表达式")
    args = parser.parse_args(argv)
    try:
        report = search_workspace(args.workspace, " ".join(args.query), regex=args.regex)
    except ValueError as exc:
        parser.error(str(exc))
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
