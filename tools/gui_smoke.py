#!/usr/bin/env python3
"""无头（Xvfb）GUI 冒烟测试：真实构造窗口、渲染 Case、滚动、拖动思维导图节点。

在 Linux CI / 沙箱里跑：
    xvfb-run -a python3 tools/gui_smoke.py

它不依赖 Codex CLI，也不需要显示器，用于回归「拖影/断层」相关代码路径：
ScrollableFrame、MouseWheelRouter、MindMapCanvas、缩放与自适应换行。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


class FakeEvent(SimpleNamespace):
    pass


def make_fake_repo(base: Path) -> Path:
    repo = base / "blender-fake"
    (repo / "source" / "blender").mkdir(parents=True)
    (repo / "CMakeLists.txt").write_text("# fake blender\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def build_investigation(case_id: str) -> dict:
    return {
        "schema_version": 1,
        "case_id": case_id,
        "summary": {
            "problem": "冒烟测试问题：这是一段很长的中文描述，用来验证换行宽度会跟随窗口宽度自动调整而不会被裁切。" * 3,
            "expected_behavior": "应当正常",
            "actual_behavior": "实际异常",
            "reproduction_steps": ["步骤一", "步骤二"],
            "known_environment": [],
            "missing_information": [],
        },
        "hypotheses": [
            {
                "id": "H1",
                "title": "路径一",
                "claim": "说明 " * 30,
                "priority": "high",
                "status": "open",
                "basis": ["依据一", "依据二"],
                "source_references": [],
                "next_step": "下一步",
                "evidence_ids": [],
            }
        ],
        "evidence": [{"id": f"E{i}", "kind": "fact", "statement": f"事实 {i} " * 12} for i in range(12)],
        "unknowns": [{"id": f"U{i}", "question": f"未知 {i}"} for i in range(4)],
        "suggested_experiments": [],
        "causal_graph": {
            "nodes": [
                {"id": "N1", "label": "触发：打开点云文件", "kind": "trigger", "certainty": "fact", "evidence_ids": [], "x": 24, "y": 24, "user_edited": False, "user_created": False},
                {"id": "N2", "label": "状态：position 属性缺失未检查", "kind": "state", "certainty": "inference", "evidence_ids": [], "x": 240, "y": 24, "user_edited": False, "user_created": False},
                {"id": "N3", "label": "故障：解引用空属性崩溃", "kind": "failure", "certainty": "unknown", "evidence_ids": [], "x": 456, "y": 24, "user_edited": False, "user_created": False},
            ],
            "edges": [
                {"id": "E1", "from": "N1", "to": "N2", "label": "", "certainty": "fact"},
                {"id": "E2", "from": "N2", "to": "N3", "label": "", "certainty": "inference"},
            ],
        },
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


def main() -> int:
    home = tempfile.mkdtemp(prefix="bugcompass-home-")
    os.environ["BUGCOMPASS_HOME"] = home
    work = tempfile.mkdtemp(prefix="bugcompass-work-")
    repo = make_fake_repo(Path(work))

    import tkinter  # noqa: F401  — 先确认 Tk 可用

    from bugcompass import gui
    from bugcompass.codex_runner import CodexRunner
    from bugcompass.investigation import write_investigation

    apps: list = []

    def capture_mainloop(self, *args, **kwargs):  # noqa: ANN001
        apps.append(self)

    with mock.patch.object(CodexRunner, "find_executable", staticmethod(lambda: "/bin/true")), \
         mock.patch("tkinter.Tk.mainloop", capture_mainloop):
        rc = gui.run_gui()
    check("run_gui 启动", rc == 0 and len(apps) == 1, f"rc={rc} apps={len(apps)}")
    if not apps:
        return report()
    app = apps[0]

    try:
        app.update()

        # 使用独立临时工作区，避免污染仓库目录
        from bugcompass.gui_controller import GuiController
        from bugcompass.practice import PracticeManager

        app.controller = GuiController(Path(work) / "ws")
        app.practice_manager = PracticeManager(REPO, app.controller.workspace_path)

        # 1) 创建真实工作区与案件
        view = app.controller.create_investigation(str(repo), "冒烟测试的 Bug 描述。")
        case_id = view.case_id
        check("创建案件", bool(case_id), case_id)

        # 写入一份带因果链的完整调查结果
        data = build_investigation(case_id)
        write_investigation(view.case_dir / "investigation.json", data)
        view = app.controller.load_case(case_id)

        # 2) 渲染结果页（含滚动容器、指标卡、思维导图）
        app._show_case(view)
        app.update()
        check("结果页渲染", app.current_case is not None and app.mindmap is not None)

        # 3) 滚动容器：内容应可滚动且 scrollregion 为整数
        scroll = app.cards_scroll
        bbox = scroll.canvas.bbox("all")
        check("scrollregion 为整数坐标", bbox is not None and all(float(v).is_integer() for v in bbox), str(bbox))
        height = scroll.canvas.winfo_height()
        content_height = (bbox[3] - bbox[1]) if bbox else 0
        check("内容超过视口（可滚动）", content_height > height > 0, f"content={content_height} view={height}")
        before = scroll.canvas.yview()
        scroll.scroll_pixels(240)
        app.update()
        after = scroll.canvas.yview()
        check("滚轮滚动生效", after != before, f"{before} -> {after}")

        # 4) 滚轮事件走 MouseWheelRouter（指针位置不需要真实鼠标）
        moved = scroll.canvas.yview()
        app.event_generate("<MouseWheel>", delta=-240, when="now")
        app.update()
        moved2 = scroll.canvas.yview()
        check("MouseWheelRouter 路由滚轮", moved2 != moved, f"{moved} -> {moved2}")

        # 5) 自适应换行：wraplength 应接近视口宽度而非固定 700
        if scroll._wrapped:
            sample = scroll._wrapped[0][0]
            width = sample.cget("wraplength")
            canvas_width = scroll.canvas.winfo_width()
            check(
                "自适应换行宽度",
                80 <= int(width) <= max(200, canvas_width),
                f"wraplength={width} canvas={canvas_width}",
            )

        # 6) 思维导图：拖动节点 → 坐标变化且落盘
        mindmap = app.mindmap
        node = mindmap.nodes["N2"]
        x0, y0 = node["x"], node["y"]
        # 直接用世界坐标驱动拖动（不依赖真实像素命中）
        mindmap._drag = {
            "mode": "node",
            "ids": {"N2"},
            "origin": {"N2": (x0, y0)},
            "start": (0, 0),
            "moved": False,
        }
        motion = FakeEvent(x=120, y=140, state=0)
        mindmap._on_motion(motion)
        mindmap._flush()
        mindmap._on_release(FakeEvent(x=120, y=140, state=0))
        app.update()
        node = mindmap.nodes["N2"]
        check("节点拖动改变坐标", (node["x"], node["y"]) != (x0, y0), f"{(x0, y0)} -> {(node['x'], node['y'])}")
        check("拖动坐标为整数", isinstance(node["x"], int) and isinstance(node["y"], int))
        saved = json.loads((view.case_dir / "investigation.json").read_text(encoding="utf-8"))
        saved_node = next(n for n in saved["causal_graph"]["nodes"] if n["id"] == "N2")
        check("拖动结果落盘", (saved_node["x"], saved_node["y"]) == (node["x"], node["y"]), str((saved_node["x"], saved_node["y"])))

        # 7) 添加节点 / 撤销
        mindmap.add_node("新增的测试节点")
        check("添加节点", any(n.get("label") == "新增的测试节点" for n in mindmap.nodes.values()))
        mindmap._on_undo(FakeEvent())
        check("撤销添加", not any(n.get("label") == "新增的测试节点" for n in mindmap.nodes.values()))

        # 8) 缩放（Ctrl+滚轮走卡片区域回调）
        before_percent = app.ui_scale.percent
        app._on_cards_zoom(10)
        app.update()
        check("界面缩放生效", app.ui_scale.percent == before_percent + 10, f"{before_percent} -> {app.ui_scale.percent}")
        app._on_cards_zoom(-10)

        # 9) 思维导图画布自身缩放（界面缩放会整页重建，需要重新拿画布）
        mindmap = app.mindmap
        z0 = mindmap.zoom
        mindmap.zoom_at(100, 100, 1.25)
        app.update()
        check("画布缩放生效", abs(mindmap.zoom - z0 * 1.25) < 1e-6, f"{z0} -> {mindmap.zoom}")

        # 10) 指标卡存在且写入 metrics.json
        check("metrics.json 已生成", (view.case_dir / "metrics.json").is_file())

        # 11) 设置对话框可打开
        app._open_settings()
        app.update()
        open_dialogs = [w for w in app.winfo_children() if isinstance(w, tkinter.Toplevel)]
        check("设置对话框打开", len(open_dialogs) >= 1)
        for dialog in open_dialogs:
            dialog.destroy()

        # 12) 备份 / 恢复
        backup_path = None
        from bugcompass.backup import create_backup, restore_backup

        backup_path = create_backup(view.workspace_path, dest=Path(work) / "backup.zip")
        restored = restore_backup(backup_path, Path(work) / "restore")
        check("备份与恢复", backup_path.is_file() and (restored / "project.json").is_file(), str(restored))

        # 13) 诊断包自检（不得包含密钥/源码）
        from bugcompass.diagnostics import export_bundle, verify_bundle

        bundle = export_bundle(Path(work), root=app, case_dir=view.case_dir, include_case_structure=True)
        verify_bundle(bundle)
        names = subprocess.run(
            [sys.executable, "-c", f"import zipfile;print('\\n'.join(zipfile.ZipFile(r'{bundle}').namelist()))"],
            capture_output=True, text=True,
        ).stdout.split()
        check("诊断包不含源码/正文", all(not n.endswith((".py", ".md")) for n in names), str(names))

        app._on_close() if not app.codex_active else app.destroy()
    except Exception as exc:  # pragma: no cover
        import traceback

        traceback.print_exc()
        check("无异常跑完全部步骤", False, repr(exc))
    finally:
        try:
            app.destroy()
        except Exception:
            pass
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
    return report()


def report() -> int:
    print()
    if FAILURES:
        print(f"冒烟测试失败 {len(FAILURES)} 项：{FAILURES}")
        return 1
    print("冒烟测试全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
