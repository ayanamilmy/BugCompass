from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from .workspace import BugCompassError, utc_now


PRIORITIES = {"high", "medium", "low"}
# 界面展示顺序：高 → 中 → 低；表里没有的优先级一律排到最后。
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
KINDS = {"fact", "inference"}
CAUSAL_NODE_KINDS = {"trigger", "decision", "state", "failure", "fix", "unknown"}
CAUSAL_CERTAINTIES = {"fact", "inference", "unknown"}
SEMANTIC_DIFF_STATUSES = {"not_available", "proposed", "observed"}


def empty_investigation(case_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "case_id": case_id,
        "updated_at": utc_now(),
        "stage": "intake",
        "summary": {
            "problem": "",
            "expected_behavior": "",
            "actual_behavior": "",
            "reproduction_steps": [],
            "known_environment": [],
            "missing_information": [],
        },
        "hypotheses": [],
        "evidence": [],
        "unknowns": [],
        "suggested_experiments": [],
        "causal_graph": {"nodes": [], "edges": []},
        "semantic_diff": {
            "status": "not_available",
            "summary": "",
            "old_rule": "",
            "new_rule": "",
            "changed_invariants": [],
            "affected_paths": [],
            "remaining_risks": [],
            "source_references": [],
        },
    }


def order_hypotheses(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按展示顺序排列调查路径：有效路径按 高→中→低 优先级，已否定路径沉底。

    ``sorted`` 是稳定排序：同优先级（或同被否定）保持调查引擎给出的原始顺序，
    所以界面上「路径 ①」永远是此刻最值得先做的那一条。
    """

    def sort_key(item: dict[str, Any]) -> tuple[int, int]:
        return (
            1 if item.get("status") == "rejected" else 0,
            PRIORITY_ORDER.get(item.get("priority"), len(PRIORITY_ORDER)),
        )

    return sorted(items, key=sort_key)


def validate_investigation(data: Any, *, require_complete: bool = False) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise BugCompassError("结构化调查结果必须是 JSON 对象。")
    # Older 0.1 cases remain readable after the responsibility-workflow upgrade.
    data.setdefault("causal_graph", {"nodes": [], "edges": []})
    data.setdefault(
        "semantic_diff",
        {"status": "not_available", "summary": "", "old_rule": "", "new_rule": "", "changed_invariants": [], "affected_paths": [], "remaining_risks": [], "source_references": []},
    )
    required = {"summary", "hypotheses", "evidence", "unknowns", "suggested_experiments", "causal_graph", "semantic_diff"}
    missing = required.difference(data)
    if missing:
        raise BugCompassError(f"结构化调查结果缺少字段：{', '.join(sorted(missing))}")
    if not isinstance(data["summary"], dict):
        raise BugCompassError("investigation.json 的 summary 必须是对象。")
    for name in ("hypotheses", "evidence", "unknowns", "suggested_experiments"):
        if not isinstance(data[name], list):
            raise BugCompassError(f"investigation.json 的 {name} 必须是数组。")
    if require_complete and len(data["hypotheses"]) != 3:
        raise BugCompassError("完成的调查结果必须恰好包含 3 条路径。")
    ids: set[str] = set()
    for hypothesis in data["hypotheses"]:
        if not isinstance(hypothesis, dict):
            raise BugCompassError("每条调查路径必须是对象。")
        hypothesis_id = hypothesis.get("id")
        if not isinstance(hypothesis_id, str) or not hypothesis_id:
            raise BugCompassError("每条调查路径都必须有 id。")
        if hypothesis_id in ids:
            raise BugCompassError(f"调查路径 id 重复：{hypothesis_id}")
        ids.add(hypothesis_id)
        if hypothesis.get("priority") not in PRIORITIES:
            raise BugCompassError(f"调查路径 {hypothesis_id} 的 priority 无效。")
        references = hypothesis.get("source_references", [])
        if not isinstance(references, list):
            raise BugCompassError(f"调查路径 {hypothesis_id} 的 source_references 必须是数组。")
    for evidence in data["evidence"]:
        if not isinstance(evidence, dict) or evidence.get("kind") not in KINDS:
            raise BugCompassError("证据必须明确标记为 fact 或 inference。")
    for experiment in data["suggested_experiments"]:
        if not isinstance(experiment, dict):
            raise BugCompassError("每个实验必须是对象。")
        if experiment.get("permission") not in {None, "green", "yellow", "red"}:
            raise BugCompassError("实验 permission 必须是 green、yellow 或 red。")
        command = experiment.get("command")
        if command is not None and (not isinstance(command, list) or not all(isinstance(part, str) for part in command)):
            raise BugCompassError("实验 command 必须是字符串参数数组。")
        experiment.setdefault("prediction", {"choice": "", "rationale": "", "predicted_at": ""})
        experiment.setdefault("prediction_assessment", "pending")
        experiment.setdefault("prediction_comparison", "")
        prediction = experiment["prediction"]
        if not isinstance(prediction, dict) or not all(isinstance(prediction.get(key, ""), str) for key in ("choice", "rationale", "predicted_at")):
            raise BugCompassError("实验 prediction 格式无效。")
    graph = data["causal_graph"]
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("edges"), list):
        raise BugCompassError("causal_graph 必须包含 nodes 和 edges 数组。")
    node_ids: set[str] = set()
    for node in graph["nodes"]:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"]:
            raise BugCompassError("因果节点必须有合法 id。")
        if node["id"] in node_ids:
            raise BugCompassError(f"因果节点 id 重复：{node['id']}")
        node_ids.add(node["id"])
        if node.get("kind") not in CAUSAL_NODE_KINDS or node.get("certainty") not in CAUSAL_CERTAINTIES:
            raise BugCompassError(f"因果节点 {node['id']} 的类型或确定性无效。")
        if not isinstance(node.get("label"), str) or not isinstance(node.get("x"), int) or not isinstance(node.get("y"), int):
            raise BugCompassError(f"因果节点 {node['id']} 的标签或位置无效。")
        node.setdefault("user_edited", False)
        node.setdefault("user_created", False)
        if not isinstance(node["user_edited"], bool) or not isinstance(node["user_created"], bool):
            raise BugCompassError(f"因果节点 {node['id']} 的用户编辑标记无效。")
    edge_ids: set[str] = set()
    for edge in graph["edges"]:
        if not isinstance(edge, dict) or not isinstance(edge.get("id"), str) or not edge["id"]:
            raise BugCompassError("因果连线必须有合法 id。")
        if edge["id"] in edge_ids:
            raise BugCompassError(f"因果连线 id 重复：{edge['id']}")
        edge_ids.add(edge["id"])
        if edge.get("from") not in node_ids or edge.get("to") not in node_ids or edge.get("from") == edge.get("to"):
            raise BugCompassError(f"因果连线 {edge['id']} 引用了无效节点。")
        edge.setdefault("user_created", False)
        if not isinstance(edge["user_created"], bool):
            raise BugCompassError(f"因果连线 {edge['id']} 的用户创建标记无效。")
    semantic = data["semantic_diff"]
    if not isinstance(semantic, dict) or semantic.get("status") not in SEMANTIC_DIFF_STATUSES:
        raise BugCompassError("semantic_diff 格式无效。")
    for name in ("changed_invariants", "affected_paths", "remaining_risks", "source_references"):
        if not isinstance(semantic.get(name), list):
            raise BugCompassError(f"semantic_diff.{name} 必须是数组。")
    return data


def read_investigation(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BugCompassError(f"无法读取结构化调查结果：{exc}") from exc
    return validate_investigation(data)


def write_investigation(path: str | Path, data: dict[str, Any], *, require_complete: bool = False) -> None:
    target = Path(path)
    validated = validate_investigation(deepcopy(data), require_complete=require_complete)
    validated["updated_at"] = utc_now()
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(validated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)
    except OSError as exc:
        raise BugCompassError(f"无法保存结构化调查结果：{exc}") from exc


def merge_user_decisions(previous: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(candidate)
    old_by_id = {item.get("id"): item for item in previous.get("hypotheses", []) if isinstance(item, dict)}
    for item in merged.get("hypotheses", []):
        old = old_by_id.get(item.get("id"))
        if old and old.get("status") == "rejected":
            item["status"] = "rejected"
            item["user_note"] = old.get("user_note", "用户已否定此路径。")
    old_experiments = {item.get("id"): item for item in previous.get("suggested_experiments", []) if isinstance(item, dict)}
    for item in merged.get("suggested_experiments", []):
        old = old_experiments.get(item.get("id"))
        if old and old.get("prediction", {}).get("predicted_at"):
            item["prediction"] = deepcopy(old["prediction"])
    old_graph = previous.get("causal_graph", {"nodes": [], "edges": []})
    old_nodes = {item.get("id"): item for item in old_graph.get("nodes", []) if isinstance(item, dict)}
    candidate_ids = {item.get("id") for item in merged.get("causal_graph", {}).get("nodes", [])}
    for item in merged.get("causal_graph", {}).get("nodes", []):
        old = old_nodes.get(item.get("id"))
        if old:
            item["x"], item["y"] = old.get("x", item["x"]), old.get("y", item["y"])
            if old.get("user_edited"):
                item["label"] = old.get("label", item["label"])
                item["user_edited"] = True
    merged["causal_graph"]["nodes"].extend(deepcopy(item) for item in old_graph.get("nodes", []) if item.get("user_created") and item.get("id") not in candidate_ids)
    candidate_edges = {item.get("id") for item in merged.get("causal_graph", {}).get("edges", [])}
    merged["causal_graph"]["edges"].extend(deepcopy(item) for item in old_graph.get("edges", []) if item.get("user_created") and item.get("id") not in candidate_edges)
    return merged


def record_experiment_prediction(path: str | Path, experiment_id: str, choice: str, rationale: str) -> dict[str, Any]:
    if not choice.strip():
        raise BugCompassError("请先选择或填写你预测的实验结果。")
    if not rationale.strip():
        raise BugCompassError("请用一句话说明预测理由。")
    data = read_investigation(path)
    for experiment in data["suggested_experiments"]:
        if experiment.get("id") == experiment_id:
            experiment["prediction"] = {"choice": choice.strip(), "rationale": rationale.strip(), "predicted_at": utc_now()}
            experiment["prediction_assessment"] = "pending"
            experiment["prediction_comparison"] = ""
            write_investigation(path, data)
            return data
    raise BugCompassError(f"找不到实验：{experiment_id}")


def update_causal_graph(path: str | Path, graph: dict[str, Any]) -> dict[str, Any]:
    data = read_investigation(path)
    data["causal_graph"] = deepcopy(graph)
    write_investigation(path, data)
    return data


def reject_hypothesis(path: str | Path, hypothesis_id: str, note: str = "用户已否定此路径。") -> dict[str, Any]:
    data = read_investigation(path)
    for item in data["hypotheses"]:
        if item.get("id") == hypothesis_id:
            item["status"] = "rejected"
            item["user_note"] = note
            write_investigation(path, data)
            export_markdown(Path(path).parent, data)
            return data
    raise BugCompassError(f"找不到调查路径：{hypothesis_id}")


def _lines(values: list[Any], fallback: str = "- 暂无。") -> str:
    rendered = [f"- {value}" for value in values if str(value).strip()]
    return "\n".join(rendered) if rendered else fallback


def export_markdown(case_dir: str | Path, data: dict[str, Any]) -> None:
    root = Path(case_dir)
    summary = data.get("summary", {})
    intake = (
        "# 问题整理\n\n## 问题摘要\n\n"
        f"{summary.get('problem') or '待调查。'}\n\n## 预期行为\n\n{summary.get('expected_behavior') or '待整理。'}\n\n"
        f"## 实际行为\n\n{summary.get('actual_behavior') or '待整理。'}\n\n## 复现步骤\n\n"
        f"{_lines(summary.get('reproduction_steps', []))}\n\n## 已知环境\n\n{_lines(summary.get('known_environment', []))}\n\n"
        f"## 缺失信息\n\n{_lines(summary.get('missing_information', []))}\n"
    )
    priority_names = {"high": "高", "medium": "中", "low": "低"}
    hypothesis_parts = ["# 调查路径\n"]
    for index, item in enumerate(data.get("hypotheses", []), 1):
        hypothesis_parts.append(
            f"## {index}. {item.get('title', '未命名路径')}\n\n"
            f"- 相对优先级：{priority_names.get(item.get('priority'), item.get('priority', '未知'))}\n"
            f"- 状态：{item.get('status', 'active')}\n"
            f"- 假设：{item.get('claim', '')}\n"
            f"- 当前依据：{'; '.join(item.get('basis', [])) or '暂无'}\n"
            f"- 最低成本下一步：{item.get('next_step', '')}\n"
            f"- 支持结果：{item.get('supporting_result', '')}\n"
            f"- 削弱结果：{item.get('weakening_result', '')}\n"
            f"- 风险与成本：{item.get('risk', '')} / {item.get('estimated_cost', '')}\n"
        )
    evidence_parts = ["# 证据记录\n", "## 已观察事实\n", _lines([e.get("statement", "") for e in data.get("evidence", []) if e.get("kind") == "fact"]), "\n## 推测\n", _lines([e.get("statement", "") for e in data.get("evidence", []) if e.get("kind") == "inference"]), "\n## 未知信息\n", _lines([u.get("question", "") for u in data.get("unknowns", [])])]
    try:
        (root / "intake.md").write_text(intake, encoding="utf-8")
        (root / "hypotheses.md").write_text("\n".join(hypothesis_parts), encoding="utf-8")
        (root / "evidence.md").write_text("\n".join(evidence_parts) + "\n", encoding="utf-8")
    except OSError as exc:
        raise BugCompassError(f"无法导出 Markdown：{exc}") from exc
