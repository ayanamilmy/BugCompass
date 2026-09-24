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

        # 普通用户从独立报告模式起步，无需源码仓库或调查进程。
        check("默认进入报告 Bug 模式", app.report_page.winfo_ismapped())

        # 设置入口必须在默认页就可见（曾只存在于案件工作台，新用户找不到）
        import tkinter as _tk
        from tkinter import ttk as _ttk

        def _walk_all(widget):
            for child in widget.winfo_children():
                yield child
                yield from _walk_all(child)

        check(
            "默认页可见 ⚙ 设置入口",
            any(
                isinstance(w, _ttk.Button) and "设置" in str(w.cget("text")) and w.winfo_ismapped()
                for w in _walk_all(app)
            ),
        )
        app._new_standalone_report()
        app.update()
        check("无源码可创建报告草稿", len(app.report_store.list_reports()) == 1)
        check("报告模式未启动调查引擎", not app.codex_active and app.active_case_id is None)
        standalone_dialogs = [w for w in app.winfo_children() if isinstance(w, tkinter.Toplevel)]
        check("独立报告编辑窗口打开", any("报告 Bug" in w.title() for w in standalone_dialogs))
        for dialog in standalone_dialogs:
            dialog.destroy()
        with mock.patch.object(CodexRunner, "find_executable", staticmethod(lambda: None)):
            available, detail = gui.check_gui()
        check("没有 Codex 仍可使用报告模式", available and "报告 Bug 模式可用" in detail)

        # 普通用户的最后一步：复制官方表单正文并导出附件包。
        report_id = app.report_store.list_reports()[0].report_id
        report_draft = app.report_store.load(report_id)
        report_draft.update({
            "title": "默认场景移动立方体后崩溃", "broken_version": "5.0.1", "system_info": "Windows 11；RTX 4060",
            "steps": "1. 打开默认场景\n2. 移动立方体", "expected": "物体移动", "actual": "Blender 退出",
            "reproduced": True, "factory_startup": True,
        })
        app.report_store.save(report_id, report_draft)
        app._open_report(report_id)
        app.update()
        editor = next(w for w in app.winfo_children() if isinstance(w, tkinter.Toplevel))

        def find_button(widget, title):  # noqa: ANN001
            for child in widget.winfo_children():
                if isinstance(child, tkinter.ttk.Button) and child.cget("text") == title:
                    return child
                found = find_button(child, title)
                if found is not None:
                    return found
            return None

        def find_widgets(widget, kind):  # noqa: ANN001
            found = []
            for child in widget.winfo_children():
                if isinstance(child, kind):
                    found.append(child)
                found.extend(find_widgets(child, kind))
            return found

        reproduced_box = next(box for box in find_widgets(editor, tkinter.Checkbutton)
                              if box.cget("text") == "我已按所写步骤再次复现")
        actual_editor = next(box for box in find_widgets(editor, tkinter.Text)
                             if box.cget("state") == "normal" and box.get("1.0", "end-1c") == "Blender 退出")
        actual_editor.insert("end", "。")
        app.update()
        check("修改现象后要求重新确认复现", not bool(editor.getvar(reproduced_box.cget("variable"))))
        reproduced_box.invoke()

        with mock.patch("tkinter.messagebox.showinfo"):
            find_button(editor, "复制正文").invoke()
        check("报告正文可复制到官方表单", "**Blender Version**" in editor.clipboard_get())
        report_zip = Path(work) / "standalone-report.zip"
        with mock.patch("tkinter.filedialog.asksaveasfilename", return_value=str(report_zip)), \
             mock.patch("tkinter.messagebox.askyesno", return_value=True), \
             mock.patch("tkinter.messagebox.showinfo"):
            find_button(editor, "导出 ZIP…").invoke()
        check("独立报告包可导出", report_zip.is_file())
        editor.destroy()

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

        # 1b) 调查引擎选择器：默认 Codex，大模型服务已加载
        check("调查引擎选择器默认 Codex", app.engine_var.get().startswith("Codex CLI"), app.engine_var.get())
        check("LLM 预设已加载", isinstance(app.llm_providers, list) and len(app.llm_providers) >= 5, f"{len(app.llm_providers)} 个")
        from bugcompass.resources import user_root as _user_root

        check("首次启动自动生成 providers.json（零命令行）", _user_root().joinpath("providers.json").is_file())
        app.settings["active_engine"] = app.llm_providers[0].id
        check("引擎切换为 LLM", app._engine_label() == app.llm_providers[0].label, app._engine_label())
        app.settings["active_engine"] = "codex"

        # 1c) 密钥存储（GUI 导入的后端）
        from bugcompass import key_store
        mode_used = key_store.save_key(app.llm_providers[0].id, "sk-smoke-test")
        check("密钥保存", key_store.get_key(app.llm_providers[0].id) == "sk-smoke-test", mode_used)
        key_store.delete_key(app.llm_providers[0].id)
        check("密钥删除", key_store.get_key(app.llm_providers[0].id) is None)

        # 1d-pre) 筛选缓存恢复：重开对话框能看到上一轮结果与 AI 评价（曾只填表格、记录列表为空）
        from bugcompass import issue_scout as _scout_mod
        from bugcompass.issue_scout import IssueRecord as _SIR, IssueScore as _SIS

        _scout_mod.save_scan(
            [_SIR(101, "缓存 issue", "u", "正文内容", ["Type/Bug"], "2026-09-01", 0)],
            {101: _SIS(101, 8, "入门", "复现清晰")},
            {},
        )
        app._open_issue_scout_dialog()
        app.update()
        _cache_dialogs = [w for w in app.winfo_children() if isinstance(w, tkinter.Toplevel) and "AI" in w.title()]
        check("筛选缓存恢复：记录可交互", len(app._scout_records) == 1, str(len(app._scout_records)))
        app._scout_tree.selection_set("101")
        app._scout_tree.event_generate("<<TreeviewSelect>>")
        app.update()
        _detail = app._scout_detail_text.get("1.0", "end")
        check("筛选缓存恢复：AI 评价可见", "复现清晰" in _detail, _detail[:60])
        for _d in _cache_dialogs:
            _d.destroy()

        # 1d) AI 挑选 Issue 对话框（打开不联网；显示离线缓存或空态）
        app._open_issue_scout_dialog()
        app.update()
        scout_dialogs = [w for w in app.winfo_children() if isinstance(w, __import__("tkinter").Toplevel) and w.title().startswith("AI 挑选")]
        check("Issue 筛选对话框可打开", len(scout_dialogs) == 1)
        check("筛选对话框含结果表格", hasattr(app, "_scout_tree"))
        # 占用过滤：默认隐藏「已有人接手」的 issue
        from bugcompass.issue_scout import IssueRecord, IssueScore
        demo_records = [
            IssueRecord(101, "空闲 issue", "u", "b", ["Type/Bug", "Meta/Good First Issue"], "2026-09-01", 0),
            IssueRecord(102, "被占用 issue", "u", "b", ["Type/Bug"], "2026-09-01", 3),
        ]
        demo_scores = {
            101: IssueScore(101, 8, "入门", "复现清晰"),
            102: IssueScore(102, 9, "进阶", "r", taken=True, taken_evidence="已指派给 dev"),
        }
        app._scout_populate(demo_records, demo_scores)
        visible = app._scout_tree.get_children()
        check("占用过滤：默认只显示空闲 issue", list(visible) == ["101"], str(visible))
        app._scout_hide_taken_var.set(False)
        app._scout_populate(demo_records, demo_scores)
        visible = app._scout_tree.get_children()
        check("取消勾选后显示全部", set(visible) == {"101", "102"}, str(visible))
        for d in scout_dialogs:
            d.destroy()

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
        # 真正的桌面鼠标可能停在测试窗口外；固定命中目标以验证路由本身。
        with mock.patch.object(app, "winfo_containing", return_value=scroll.canvas):
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

        # 9b) 滚轮路由转发（macOS 触控板修复：画布自身绑定拿不到事件时由路由器转发）
        adapters = getattr(app.wheel_router, "_canvas_adapters", {})
        check("思维导图已注册滚轮路由", bool(adapters))
        fresh_mm = app.mindmap  # 界面缩放会重建页面，必须取当前实例
        current_adapter = adapters.get(str(fresh_mm.canvas))
        check("当前思维导图已注册滚轮路由", current_adapter is not None)
        if current_adapter is not None:
            oy = fresh_mm.offset[1]
            current_adapter.scroll_pixels(-120)
            check("滚轮转发平移生效", abs(fresh_mm.offset[1] - oy + 60) < 1e-6, f"{oy} -> {fresh_mm.offset[1]}")

        # 10) 指标卡存在且写入 metrics.json
        check("metrics.json 已生成", (view.case_dir / "metrics.json").is_file())

        # 11) 设置对话框可打开
        app._open_settings()
        app.update()
        open_dialogs = [w for w in app.winfo_children() if isinstance(w, tkinter.Toplevel)]
        check("设置对话框打开", len(open_dialogs) >= 1)
        # 下拉必须是 ttk.Combobox（tk.OptionMenu 在 macOS 上无法着色，曾致白字不可读）
        from tkinter import ttk as _ttk_smoke

        def _walk_smoke(widget):
            for child in widget.winfo_children():
                yield child
                yield from _walk_smoke(child)

        combos = [w for d in open_dialogs for w in _walk_smoke(d) if isinstance(w, _ttk_smoke.Combobox)]
        check("设置含深色 Combobox 下拉", len(combos) >= 2 and all(str(c.cget("style")) == "Dark.TCombobox" for c in combos))
        known_models = {p.model for p in app.llm_providers}
        check("设置含模型下拉", any(c.get() in known_models for c in combos), str([c.get() for c in combos]))
        check("设置含拉取模型按钮", any(isinstance(w, _ttk_smoke.Button) and "拉取" in str(w.cget("text")) for d in open_dialogs for w in _walk_smoke(d)))
        for dialog in open_dialogs:
            dialog.update_idletasks()
            check(
                "设置对话框高度足够容纳内容",
                dialog.winfo_height() >= dialog.winfo_reqheight() - 10,
                f"{dialog.winfo_height()} < 需要 {dialog.winfo_reqheight()}",
            )
            dialog.destroy()

        # 报告包入口在当前 Case 中可用，且草稿可离线打开。
        app._open_repro_report_dialog()
        app.update()
        report_dialogs = [w for w in app.winfo_children() if isinstance(w, tkinter.Toplevel)]
        check("可复现报告包窗口打开", any("可复现报告包" in w.title() for w in report_dialogs))
        for dialog in report_dialogs:
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

    # 3) 英文模式：写入 language=en 后重新启动，检查整窗文案
    try:
        from bugcompass.settings import load_settings, save_settings
        from bugcompass import i18n

        settings = load_settings()
        settings["language"] = "en"
        save_settings(settings)
        apps.clear()
        with mock.patch.object(CodexRunner, "find_executable", staticmethod(lambda: "/bin/true")), \
             mock.patch("tkinter.Tk.mainloop", capture_mainloop):
            rc = gui.run_gui()
        check("英文模式启动", rc == 0 and len(apps) == 1)
        if apps:
            import tkinter as tkmod
            from tkinter import ttk as ttkmod

            en_app = apps[0]
            en_app.update()
            def walk(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from walk(child)

            texts = [en_app.title()]
            for w in walk(en_app):
                if isinstance(w, (ttkmod.Button, tkmod.Label)):
                    texts.append(str(w.cget("text")))
            all_text = " ".join(texts)
            check("英文模式标题", "Blender Bug Assistant" in all_text)
            check("英文模式含 Settings", "Settings" in all_text)
            check("英文模式含 Issue Scout", "Issue Scout" in all_text)
            check("英文模式无中文残留(顶栏)", not any("\u4e00" <= ch <= "\u9fff" for ch in all_text), all_text[:160])
            en_app.destroy()
        i18n.set_language("zh")
    finally:
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
