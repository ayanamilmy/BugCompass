"""面向普通 Blender 用户的独立报告编辑窗口。"""

from __future__ import annotations

import webbrowser
from typing import Any, Callable

from .i18n import tr
from .report_mode import ReportStore
from .repro_report import REPORT_GUIDE, format_official_body, review_draft
from .workspace import BugCompassError


ISSUES_URL = "https://projects.blender.org/blender/blender/issues"
NEW_ISSUE_URL = "https://projects.blender.org/blender/blender/issues/new"


def open_report_editor(parent: Any, store: ReportStore, report_id: str, *, on_saved: Callable[[], None]) -> Any:
    """不创建调查 Case，不运行 Blender、命令或 AI。"""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    draft = store.load(report_id)
    dialog = tk.Toplevel(parent)
    dialog.title(tr("报告 Bug · Blender"))
    dialog.geometry("860x760")
    dialog.minsize(720, 620)
    dialog.configure(background=parent.BACKGROUND)
    dialog.transient(parent)
    dialog.grab_set()

    shell = ttk.Frame(dialog, style="App.TFrame", padding=16)
    shell.pack(fill="both", expand=True)
    ttk.Label(shell, text=tr("报告 Blender Bug"), style="Title.TLabel").pack(anchor="w")
    ttk.Label(
        shell,
        text=tr("不需要源码、终端或 AI。请填写亲自看到的情况；草稿仅保存在本机。适用于 Blender 程序本身。"),
        style="PageSubtitle.TLabel", wraplength=780,
    ).pack(anchor="w", pady=(3, 12))

    tabs = ttk.Notebook(shell)
    tabs.pack(fill="both", expand=True)
    problem = ttk.Frame(tabs, style="Surface.TFrame", padding=14)
    materials = ttk.Frame(tabs, style="Surface.TFrame", padding=14)
    preview = ttk.Frame(tabs, style="Surface.TFrame", padding=14)
    tabs.add(problem, text=tr("1  描述问题"))
    tabs.add(materials, text=tr("2  复现材料"))
    tabs.add(preview, text=tr("3  核对与提交"))

    def label(container: Any, value: str) -> None:
        ttk.Label(container, text=value, style="Heading.TLabel", font=parent._font("SF Pro Text", 10, "bold")).pack(anchor="w", pady=(8, 3))

    fields: dict[str, Any] = {}
    editors: dict[str, Any] = {}

    label(problem, tr("简短标题：什么操作出现了什么问题？"))
    title_var = tk.StringVar(value=draft["title"])
    ttk.Entry(problem, textvariable=title_var, style="Dark.TEntry").pack(fill="x")
    fields["title"] = title_var

    for name, prompt, height in (
        ("steps", tr("从默认场景或所附 .blend 开始，按顺序写出每一步"), 8),
        ("expected", tr("原本应当发生什么？"), 3),
        ("actual", tr("实际发生了什么？"), 3),
    ):
        label(problem, prompt)
        editor = tk.Text(problem, height=height, wrap="word", background=parent.SURFACE_ALT, foreground=parent.TEXT,
                         insertbackground=parent.TEXT, relief="flat", padx=8, pady=6)
        editor.insert("1.0", draft[name])
        editor.pack(fill="both", expand=name == "steps")
        editors[name] = editor

    for name, prompt in (
        ("broken_version", tr("出错的 Blender 完整版本（可从启动画面查看）")),
        ("working_version", tr("最后正常的版本（不知道可留空）")),
        ("latest_tested_version", tr("最近复测的版本（如已复测）")),
        ("system_info", tr("系统、显卡和驱动摘要；或在下方加入 system-info.txt")),
    ):
        label(materials, prompt)
        variable = tk.StringVar(value=draft[name])
        ttk.Entry(materials, textvariable=variable, style="Dark.TEntry").pack(fill="x")
        fields[name] = variable

    ttk.Label(materials, text=tr("在 Blender 中可用“帮助 → 保存系统信息”取得 system-info.txt。"), style="Muted.TLabel").pack(anchor="w", pady=(5, 8))
    flags: dict[str, Any] = {}
    for name, prompt in (
        ("reproduced", tr("我已按所写步骤再次复现")),
        ("factory_startup", tr("无需 .blend，从默认场景就能复现")),
        ("tested_latest", tr("我已用最新稳定版或开发版复测")),
        ("duplicate_checked", tr("我已搜索开放及已关闭的报告")),
        ("simplified_file", tr("所附 .blend 已删去无关内容")),
        ("crash", tr("这是崩溃问题")),
    ):
        variable = tk.BooleanVar(value=draft[name])
        tk.Checkbutton(materials, text=prompt, variable=variable, background=parent.SURFACE, foreground=parent.TEXT,
                       activebackground=parent.SURFACE, activeforeground=parent.TEXT,
                       selectcolor=parent.SURFACE_ALT, anchor="w").pack(anchor="w")
        flags[name] = variable

    for editor in editors.values():
        editor.edit_modified(False)

    def invalidate_reproduction(event: Any) -> None:
        if event.widget.edit_modified():
            flags["reproduced"].set(False)
            event.widget.edit_modified(False)

    for editor in editors.values():
        editor.bind("<<Modified>>", invalidate_reproduction)
    fields["broken_version"].trace_add("write", lambda *_args: flags["reproduced"].set(False))
    flags["factory_startup"].trace_add("write", lambda *_args: flags["reproduced"].set(False))

    paths = list(draft["attachments"])
    label(materials, tr("准备公开的附件（简化 .blend、截图、system-info.txt、崩溃日志）"))
    attachment_list = tk.Listbox(materials, height=4, background=parent.SURFACE_ALT, foreground=parent.TEXT,
                                 selectbackground=parent.BORDER, borderwidth=0)
    attachment_list.pack(fill="x")

    def refresh_attachments() -> None:
        attachment_list.delete(0, "end")
        for path in paths:
            attachment_list.insert("end", path)

    def add_attachments() -> None:
        added_blend = False
        for path in filedialog.askopenfilenames(title=tr("选择准备公开的附件"), parent=dialog):
            if path not in paths:
                paths.append(path)
                added_blend = added_blend or path.casefold().endswith(".blend")
        if added_blend:
            flags["simplified_file"].set(False)
            flags["reproduced"].set(False)
        refresh_attachments()

    def remove_attachment() -> None:
        removed_blend = any(paths[index].casefold().endswith(".blend") for index in attachment_list.curselection())
        for index in reversed(attachment_list.curselection()):
            paths.pop(index)
        if removed_blend:
            flags["reproduced"].set(False)
        refresh_attachments()

    refresh_attachments()
    attachment_actions = ttk.Frame(materials, style="Surface.TFrame")
    attachment_actions.pack(anchor="w", pady=(5, 0))
    ttk.Button(attachment_actions, text=tr("添加附件…"), command=add_attachments, style="Action.TButton").pack(side="left")
    ttk.Button(attachment_actions, text=tr("移除所选"), command=remove_attachment, style="Ghost.TButton").pack(side="left", padx=(8, 0))

    ttk.Label(preview, text=tr("提交时标题与正文分别填写。附件需在官方页面单独上传。"), style="Muted.TLabel").pack(anchor="w", pady=(0, 7))
    preview_title = tk.StringVar()
    ttk.Label(preview, textvariable=preview_title, style="Heading.TLabel", wraplength=760).pack(anchor="w", pady=(0, 7))
    body_box = ttk.Frame(preview, style="Surface.TFrame")
    body_box.pack(fill="both", expand=True)
    preview_body = tk.Text(body_box, height=17, wrap="word", background=parent.SURFACE_ALT, foreground=parent.TEXT,
                           relief="flat", padx=10, pady=8)
    preview_body.pack(side="left", fill="both", expand=True)
    body_scroll = ttk.Scrollbar(body_box, orient="vertical", command=preview_body.yview)
    body_scroll.pack(side="right", fill="y")
    preview_body.configure(yscrollcommand=body_scroll.set)
    ttk.Label(preview, text=tr("提交前核对"), style="Heading.TLabel").pack(anchor="w", pady=(9, 4))
    review_box = ttk.Frame(preview, style="Surface.TFrame")
    review_box.pack(fill="x")
    review_text = tk.Text(review_box, height=7, wrap="word", background=parent.SURFACE_ALT, foreground=parent.MUTED,
                          relief="flat", padx=10, pady=8)
    review_text.pack(side="left", fill="both", expand=True)
    review_scroll = ttk.Scrollbar(review_box, orient="vertical", command=review_text.yview)
    review_scroll.pack(side="right", fill="y")
    review_text.configure(yscrollcommand=review_scroll.set)

    def current_draft() -> dict[str, Any]:
        data = dict(draft)
        data.update({name: variable.get() for name, variable in fields.items()})
        data.update({name: editor.get("1.0", "end-1c") for name, editor in editors.items()})
        data.update({name: bool(variable.get()) for name, variable in flags.items()})
        data["attachments"] = list(paths)
        return data

    def readonly(widget: Any, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    def refresh_preview(_event: Any = None) -> None:
        data = current_draft()
        review = review_draft(data)
        preview_title.set(tr("标题：") + (data["title"].strip() or tr("（待填写）")))
        readonly(preview_body, format_official_body(data))
        lines = ([tr("尚需补充：")] + [f"• {item}" for item in review.missing_required]) if review.missing_required else [tr("必填材料已填写；请仍以实际复现为准。")]
        lines += ["", tr("建议核对：")] + [f"• {item}" for item in review.suggestions]
        lines += ["", tr("附件：")] + [f"• {path}" for path in paths]
        readonly(review_text, "\n".join(lines))

    tabs.bind("<<NotebookTabChanged>>", lambda _event: refresh_preview() if tabs.select() == str(preview) else None)

    def save() -> bool:
        try:
            store.save(report_id, current_draft())
        except (BugCompassError, OSError) as exc:
            messagebox.showerror(tr("保存失败"), str(exc), parent=dialog)
            return False
        on_saved()
        refresh_preview()
        return True

    def copy_value(which: str) -> None:
        if not save():
            return
        _, body, review = store.preview(report_id)
        if not review.ready_for_review:
            tabs.select(preview)
            messagebox.showwarning(tr("报告尚未齐全"), tr("请先补充预览中列出的必填材料。草稿已经保存。"), parent=dialog)
            return
        content = current_draft()["title"].strip() if which == "title" else body
        dialog.clipboard_clear()
        dialog.clipboard_append(content)
        messagebox.showinfo(tr("已复制"), tr("标题已复制，请粘贴到官方表单标题栏。") if which == "title" else tr("正文已复制，请粘贴到官方表单描述栏。"), parent=dialog)

    def export() -> None:
        if not save():
            return
        review = review_draft(current_draft())
        if review.missing_required and not messagebox.askyesno(
            tr("尚缺材料"), tr("报告仍有必填材料未补齐。可以导出草稿留档，但不建议现在提交。继续导出？"), parent=dialog,
        ):
            tabs.select(preview)
            return
        target = filedialog.asksaveasfilename(title=tr("保存报告包"), defaultextension=".zip",
                                              initialfile=tr('{}-报告包.zip').format(report_id), filetypes=[(tr("ZIP 压缩包"), "*.zip")], parent=dialog)
        if not target:
            return
        names = "\n".join(f"• {path}" for path in paths) or tr("（无附件）")
        if not messagebox.askyesno(tr("核对公开内容"), tr("请检查预览正文和以下附件是否含私人或项目敏感内容：\n") + names +
                                   tr("\n\n只有手动选择的附件会进入 ZIP；不会自动上传。继续？"), parent=dialog):
            return
        try:
            store.export(report_id, target)
        except (BugCompassError, OSError) as exc:
            messagebox.showerror(tr("导出失败"), str(exc), parent=dialog)
            return
        messagebox.showinfo(tr("已导出"), tr('报告包已保存到：\n{}\n\n请在官方表单单独上传所需附件。').format(target), parent=dialog)

    def open_submission() -> None:
        if not save():
            return
        if not review_draft(current_draft()).ready_for_review:
            tabs.select(preview)
            messagebox.showwarning(tr("报告尚未齐全"), tr("请先补充预览中列出的必填材料。"), parent=dialog)
            return
        webbrowser.open(NEW_ISSUE_URL)

    def close() -> None:
        if save():
            dialog.destroy()

    actions = ttk.Frame(shell, style="App.TFrame")
    actions.pack(fill="x", pady=(10, 0))
    ttk.Button(actions, text=tr("保存草稿"), command=save, style="Action.TButton").pack(side="left")
    ttk.Button(actions, text=tr("查看预览"), command=lambda: tabs.select(preview), style="Action.TButton").pack(side="left", padx=6)
    ttk.Button(actions, text=tr("复制标题"), command=lambda: copy_value("title"), style="Action.TButton").pack(side="left", padx=6)
    ttk.Button(actions, text=tr("复制正文"), command=lambda: copy_value("body"), style="Action.TButton").pack(side="left")
    ttk.Button(actions, text=tr("导出 ZIP…"), command=export, style="Action.TButton").pack(side="right")
    ttk.Button(actions, text=tr("打开官方提交页"), command=open_submission, style="Primary.TButton").pack(side="right", padx=(0, 6))

    links = ttk.Frame(shell, style="App.TFrame")
    links.pack(fill="x", pady=(6, 0))
    ttk.Label(links, text=tr("推荐从 Blender 的“帮助 → 报告 Bug”进入，系统与版本信息会预填。"), style="PageSubtitle.TLabel", font=parent._font("SF Pro Text", 9)).pack(side="left")
    ttk.Button(links, text=tr("查找已有报告"), command=lambda: webbrowser.open(ISSUES_URL), style="Ghost.TButton").pack(side="right")
    ttk.Button(links, text=tr("官方指南"), command=lambda: webbrowser.open(REPORT_GUIDE), style="Ghost.TButton").pack(side="right")
    dialog.protocol("WM_DELETE_WINDOW", close)
    refresh_preview()
    return dialog
