from __future__ import annotations

import json
import os
import sys
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .i18n import set_language, tr
from . import __version__
from .backup import BackupError, create_backup, inspect_backup, restore_backup
from .codex_runner import CodexRunBusyError, CodexRunResult, CodexRunner
from .diagnostics import export_bundle, install_crash_handler, install_tk_handler
from .dpi import apply_scaling, enable_windows_dpi_awareness, scale_percent
from .gui_controller import CaseView, GuiController
from .investigation import FLOW_STEPS, flow_step, order_hypotheses
from .issue_scout import IssueRecord, IssueScore, ScoutError, deterministic_taken, enrich_with_comments, export_markdown as export_scan_markdown, fetch_open_issues, filter_issues, issue_to_bug_text, load_last_scan, save_scan, scan_records_from_cache, scan_scores_from_cache
from .key_store import delete_key, get_key, save_key, storage_hint
from .llm import LLMError, LLMProviderConfig, ensure_providers, resolve_api_key, test_connection
from .llm_runner import LLMInvestigator
from .metrics import (
    collect_case_metrics,
    format_cost,
    format_duration,
    load_pricing,
    write_case_metrics,
)
from .mindmap import MindMapCanvas
from .practice import PracticeCase, PracticeManager, PracticeReveal
from . import qa
from .report_gui import open_report_editor
from .report_mode import ReportStore, SavedReport
from .repro_report import review_draft
from .settings import load_settings, save_settings
from .telemetry import (
    clear_pending as clear_telemetry_pending,
    export_pending as export_telemetry_pending,
    is_enabled as telemetry_enabled,
    pending as telemetry_pending,
    record_run_metrics,
    set_enabled as set_telemetry_enabled,
)
from .widgets import MouseWheelRouter, ScrollableFrame, UiScale, fit_dialog
from .llm import list_models, provider_by_id, set_provider_model
from .workspace import BugCompassError


@dataclass(frozen=True)
class CodexStatusPresentation:
    state: str
    label: str
    detail: str
    can_stop: bool


def codex_status_for_case(
    *,
    active_case_id: str | None,
    run_state: str,
    run_detail: str,
    viewed_case_id: str | None,
    viewed_status: str = "unknown",
    operation_busy: bool = False,
    engine_label: str = "Codex",
) -> CodexStatusPresentation:
    """Return a case-scoped status so one run is never painted onto every case."""
    labels = {
        "idle": tr('● {} 空闲').format(engine_label),
        "working": tr('● {} 工作中').format(engine_label),
        "stopping": tr('● {} 正在停止').format(engine_label),
        "complete": tr('● {} 已完成').format(engine_label),
        "failed": tr('● {} 调查失败').format(engine_label),
        "stopped": tr('● {} 已停止').format(engine_label),
    }
    if active_case_id and run_state in {"working", "stopping"}:
        if viewed_case_id == active_case_id:
            return CodexStatusPresentation(
                run_state,
                labels[run_state],
                tr('案件 {}\n{}').format(active_case_id, run_detail),
                run_state == "working",
            )
        return CodexStatusPresentation(
            "other",
            tr("● 其他案件调查中"),
            tr('{} 正在处理 {}。\n当前案件没有在运行。').format(engine_label, active_case_id),
            False,
        )

    if operation_busy:
        return CodexStatusPresentation(
            run_state,
            labels.get(run_state, f"● Codex {run_state}"),
            run_detail,
            False,
        )

    persisted = {
        "complete": ("complete", tr("上一次 Codex 调查已经完成，可以继续补充调查。")),
        "failed": ("failed", tr("上一次调查失败，案件内容仍然保留。")),
        "cancelled": ("stopped", tr("上一次调查已停止，可以从当前 Case 继续。")),
        "investigating": ("stopped", tr("没有检测到这个案件的 Codex 进程；上一次运行可能已中断。")),
        "new": ("idle", tr("案件尚未开始或尚未产生调查结果。")),
    }
    state, detail = persisted.get(viewed_status, (run_state, run_detail))
    return CodexStatusPresentation(state, labels.get(state, f"● Codex {state}"), detail, False)


def check_gui() -> tuple[bool, str]:
    try:
        import tkinter  # noqa: F401
        from tkinter import ttk  # noqa: F401
    except (ImportError, ModuleNotFoundError) as exc:
        return False, tr('Tkinter 不可用：{}').format(exc)
    if not CodexRunner.find_executable():
        return True, tr("Tkinter 与报告 Bug 模式可用；源码调查需要 Codex CLI 或已配置的模型服务。")
    return True, tr("Tkinter、报告 Bug 模式和 Codex CLI 均可用。")


def run_gui() -> int:
    try:
        import tkinter as tk  # noqa: F401
        from tkinter import filedialog, messagebox, simpledialog, ttk
    except (ImportError, ModuleNotFoundError) as exc:
        print(tr('错误：Tkinter 不可用：{}').format(exc))
        return 2

    # 必须在创建任何 Tk 窗口之前声明 DPI aware，否则 Windows 会用位图拉伸
    # 整个窗口，在 150%/200% 缩放下出现发虚、拖影和断层（见 dpi.py）。
    enable_windows_dpi_awareness()
    install_crash_handler()

    # 报告模式始终可离线启动；调查引擎只在用户进入调查流程时需要。
    class BugCompassApp(tk.Tk):
        BACKGROUND = "#0B0D10"
        SIDEBAR = "#101217"
        SURFACE = "#15181E"
        SURFACE_ALT = "#1B1F27"
        BORDER = "#292E38"
        TEXT = "#F4F5F7"
        MUTED = "#8D95A3"
        SUBTLE = "#59616E"
        ORANGE = "#FF8A3D"
        ORANGE_HOVER = "#FF9D5C"
        GREEN = "#53D769"
        YELLOW = "#F5B942"
        RED = "#FF6363"
        # 三条路径：编号用圆圈数字，颜色即优先级（已否定路径另行置灰）。
        PATH_MARKS = "①②③④⑤⑥⑦⑧⑨⑩"
        PATH_ACCENTS = {"high": ORANGE, "medium": YELLOW, "low": GREEN}

        def __init__(self) -> None:
            super().__init__()
            # 语言要在第一个 tr()（窗口标题）之前就位。
            self.settings = load_settings()
            set_language(str(self.settings.get("language", "zh")))
            self.title(tr('BugCompass — Blender Bug 助手 · v{}').format(__version__))
            self.geometry("1120x760")
            self.minsize(940, 650)
            self.configure(background=self.BACKGROUND)

            self.controller = GuiController()
            self.report_store = ReportStore()
            self.saved_reports: list[SavedReport] = []
            # 资源根目录在源码树 / PyInstaller / 便携版下各不相同（见 resources.py），
            # 打包成安装包后 parents[2] 不再存在，必须走统一的资源定位。
            from .resources import data_root

            self._data_root_path = data_root()
            self.codex_runner = CodexRunner(self._data_root_path)
            self.practice_manager = PracticeManager(self._data_root_path, self.controller.workspace_path)
            # 大模型 API 引擎：默认不启用（active_engine=codex），配置见 llm.py。
            try:
                self.llm_providers: list[LLMProviderConfig] = ensure_providers()
                self.llm_provider_error = ""
            except LLMError as exc:
                self.llm_providers = []
                self.llm_provider_error = str(exc)
            self._llm_investigators: dict[str, LLMInvestigator] = {}
            self._active_runner: Any = None
            # 工作线程 → 主线程的安全回调通道（测试连接等异步操作用）。
            self._main_queue: queue.SimpleQueue = queue.SimpleQueue()
            self.repo_var = tk.StringVar()
            self.repo_status_var = tk.StringVar(value=tr("请选择本地 Blender 源码文件夹。"))
            self.char_count_var = tk.StringVar(value=tr("0 个字符"))
            self.progress_var = tk.StringVar()
            self.result_summary_var = tk.StringVar()
            self.result_hint_var = tk.StringVar()
            self.copy_status_var = tk.StringVar()
            self.codex_status_var = tk.StringVar(value=tr("● Codex 空闲"))
            self.codex_status_detail_var = tk.StringVar(value=tr("当前没有正在运行的调查。"))
            self.qa_status_var = tk.StringVar()
            self.qa_engine_var = tk.StringVar()
            self.details_visible = False
            self.busy = False
            self.codex_active = False
            self.active_case_id: str | None = None
            self.codex_state = "idle"
            self.codex_state_detail = tr("当前没有正在运行的调查。")
            self.current_case: CaseView | None = None
            self.current_reveal: PracticeReveal | None = None
            self.practice_cases: list[PracticeCase] = []
            self.recent_case_ids: list[str] = []
            self.events: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
            # 追问与调查是两条独立的异步线：追问失败不该动案件状态，也不该被当成调查失败。
            self.ask_busy = False
            self.ask_case_id: str | None = None
            self.path_cards: list[Any] = []
            self.mindmap: MindMapCanvas | None = None
            self.causal_graph: dict[str, Any] = {"nodes": [], "edges": []}

            # 设置 / 缩放 / 滚轮路由（Windows 滚动与拖影修复的一部分）。
            # （settings 已在 __init__ 开头加载，用于提前设置界面语言。）
            self.ui_scale = UiScale(self, tk)
            self.wheel_router = MouseWheelRouter(self)
            self._tk_module = tk
            self._ttk_module = ttk
            self._apply_saved_ui_scale()
            self.option_add("*Font", self._font("SF Pro Text", 11))
            self.option_add("*insertBackground", self.TEXT)
            self.engine_var = tk.StringVar(value=self._engine_display_name())

            self._configure_styles(ttk)
            self._build_layout(tk, ttk, filedialog, messagebox)
            self.simple_dialog = simpledialog
            # Tk 回调里的异常也要写进崩溃日志（安装版没有控制台可看）。
            install_tk_handler(self)
            self._refresh_recent_cases()
            self.show_report_page()
            self.after(200, self._poll_async)
            self.protocol("WM_DELETE_WINDOW", self._on_close)

        def _configure_styles(self, ttk_module: Any) -> None:
            style = ttk_module.Style(self)
            for theme in ("clam", "aqua", "default"):
                if theme in style.theme_names():
                    style.theme_use(theme)
                    break
            style.configure(".", background=self.BACKGROUND, foreground=self.TEXT, fieldbackground=self.SURFACE_ALT, bordercolor=self.BORDER, lightcolor=self.BORDER, darkcolor=self.BORDER, focuscolor=self.ORANGE)
            style.configure("App.TFrame", background=self.BACKGROUND)
            style.configure("Surface.TFrame", background=self.SURFACE)
            style.configure("Elevated.TFrame", background=self.SURFACE_ALT)
            style.configure("Card.TFrame", background=self.SURFACE)
            style.configure("Sidebar.TFrame", background=self.SIDEBAR)
            style.configure("Title.TLabel", background=self.BACKGROUND, foreground=self.TEXT, font=self._font("SF Pro Display", 28, "bold"))
            style.configure("Eyebrow.TLabel", background=self.BACKGROUND, foreground=self.ORANGE, font=self._font("SF Pro Text", 9, "bold"))
            style.configure("PageSubtitle.TLabel", background=self.BACKGROUND, foreground=self.MUTED, font=self._font("SF Pro Text", 12))
            style.configure("PageStatus.TLabel", background=self.BACKGROUND, foreground=self.ORANGE, font=self._font("SF Pro Text", 10, "bold"))
            style.configure("Heading.TLabel", background=self.SURFACE, foreground=self.TEXT, font=self._font("SF Pro Display", 15, "bold"))
            style.configure("Body.TLabel", background=self.SURFACE, foreground=self.TEXT, font=self._font("SF Pro Text", 11))
            style.configure("Muted.TLabel", background=self.SURFACE, foreground=self.MUTED, font=self._font("SF Pro Text", 10))
            style.configure("Status.TLabel", background=self.SURFACE, foreground=self.ORANGE, font=self._font("SF Pro Text", 10, "bold"))
            style.configure("Primary.TButton", font=self._font("SF Pro Text", 11, "bold"), padding=(20, 13), background=self.ORANGE, foreground="#17120E", borderwidth=0, relief="flat")
            style.map("Primary.TButton", foreground=[("disabled", self.SUBTLE), ("!disabled", "#17120E")], background=[("active", self.ORANGE_HOVER), ("disabled", self.BORDER), ("!disabled", self.ORANGE)])
            style.configure("Action.TButton", font=self._font("SF Pro Text", 10, "bold"), padding=(13, 9), background=self.SURFACE_ALT, foreground=self.TEXT, borderwidth=0, relief="flat")
            style.map("Action.TButton", background=[("active", self.BORDER), ("!disabled", self.SURFACE_ALT)], foreground=[("disabled", self.SUBTLE), ("!disabled", self.TEXT)])
            style.configure("Ghost.TButton", font=self._font("SF Pro Text", 10), padding=(10, 7), background=self.SURFACE, foreground=self.MUTED, borderwidth=0, relief="flat")
            style.map("Ghost.TButton", background=[("active", self.SURFACE_ALT)], foreground=[("active", self.TEXT), ("!disabled", self.MUTED)])
            style.configure("SidebarTitle.TLabel", background=self.SIDEBAR, foreground=self.TEXT, font=self._font("SF Pro Display", 16, "bold"))
            style.configure("SidebarHint.TLabel", background=self.SIDEBAR, foreground=self.MUTED, font=self._font("SF Pro Text", 9))
            style.configure("Brand.TLabel", background=self.SIDEBAR, foreground=self.ORANGE, font=self._font("SF Pro Text", 9, "bold"))
            style.configure("Step.TLabel", background=self.SURFACE_ALT, foreground=self.MUTED, padding=(10, 5), font=self._font("SF Pro Text", 9, "bold"))
            style.configure("StepActive.TLabel", background="#332319", foreground=self.ORANGE, padding=(10, 5), font=self._font("SF Pro Text", 9, "bold"))
            # 已经走过的步骤：绿底绿字，与「当前步骤」的橙、未开始步骤的灰区分开。
            style.configure("StepDone.TLabel", background="#16281B", foreground=self.GREEN, padding=(10, 5), font=self._font("SF Pro Text", 9, "bold"))
            style.configure("Dark.TEntry", fieldbackground=self.SURFACE_ALT, foreground=self.TEXT, insertcolor=self.TEXT, padding=10, borderwidth=1)
            style.configure("Dark.TLabelframe", background=self.SURFACE, bordercolor=self.BORDER, relief="solid", borderwidth=1)
            style.configure("Dark.TLabelframe.Label", background=self.SURFACE, foreground=self.MUTED, font=self._font("SF Pro Text", 9, "bold"))
            # macOS(Aqua) 会无视自定义深色 fieldbackground、却应用前景色——
            # 深底白字在 Mac 上会变成“浅底白字”不可读。因此分平台：
            # macOS 顺应系统浅色底 + 黑字；Windows/Linux 背景设置生效，用深底白字。
            if sys.platform == "darwin":
                style.configure(
                    "Dark.TCombobox",
                    fieldbackground="#FFFFFF", background="#E9E9EC",
                    foreground="#111111", arrowcolor="#111111",
                    bordercolor="#C9C9CE", insertcolor="#111111",
                )
                self.option_add("*TCombobox*Listbox*Background", "#FFFFFF")
                self.option_add("*TCombobox*Listbox*Foreground", "#111111")
                self.option_add("*TCombobox*Listbox*selectBackground", "#C7E0FF")
                self.option_add("*TCombobox*Listbox*selectForeground", "#111111")
            else:
                style.configure("Dark.TCombobox", fieldbackground=self.SURFACE_ALT, background=self.SURFACE_ALT, foreground=self.TEXT, arrowcolor=self.TEXT, bordercolor=self.BORDER, insertcolor=self.TEXT)
                # 下拉弹出列表是独立 Listbox，用 option_add 全局着色。
                self.option_add("*TCombobox*Listbox*Background", self.SURFACE_ALT)
                self.option_add("*TCombobox*Listbox*Foreground", self.TEXT)
                self.option_add("*TCombobox*Listbox*selectBackground", "#654127")
                self.option_add("*TCombobox*Listbox*selectForeground", self.TEXT)
            style.configure("Scout.Treeview", background=self.SURFACE_ALT, fieldbackground=self.SURFACE_ALT, foreground=self.TEXT, borderwidth=0, rowheight=30)
            style.configure("Scout.Treeview.Heading", background=self.SURFACE, foreground=self.MUTED, borderwidth=0, relief="flat", font=self._font("SF Pro Text", 9, "bold"))
            style.map("Scout.Treeview", background=[("selected", "#654127")], foreground=[("selected", self.TEXT)])

        def _build_layout(self, tk_module: Any, ttk_module: Any, file_dialog: Any, message_box: Any) -> None:
            self.file_dialog = file_dialog
            self.message_box = message_box
            shell = ttk_module.Frame(self, style="App.TFrame")
            shell.pack(fill="both", expand=True)
            shell.columnconfigure(1, weight=1)
            shell.rowconfigure(0, weight=1)

            sidebar = ttk_module.Frame(shell, style="Sidebar.TFrame", padding=(20, 24), width=255)
            sidebar.grid(row=0, column=0, sticky="nsew")
            sidebar.grid_propagate(False)
            brand = ttk_module.Frame(sidebar, style="Sidebar.TFrame")
            brand.pack(fill="x", pady=(0, 28))
            logo = tk_module.Canvas(brand, width=34, height=34, background=self.SIDEBAR, highlightthickness=0)
            logo.create_oval(2, 2, 32, 32, fill=self.ORANGE, outline="")
            logo.create_text(17, 17, text="◈", fill="#17120E", font=self._font("SF Pro Display", 17, "bold"))
            logo.pack(side="left", padx=(0, 10))
            brand_text = ttk_module.Frame(brand, style="Sidebar.TFrame")
            brand_text.pack(side="left")
            ttk_module.Label(brand_text, text="BUGCOMPASS", style="Brand.TLabel").pack(anchor="w")
            ttk_module.Label(brand_text, text=tr("Blender Bug 助手"), style="SidebarTitle.TLabel").pack(anchor="w")
            ttk_module.Button(sidebar, text=tr("＋  报告 Bug"), command=self.show_report_page, style="Primary.TButton").pack(fill="x", pady=(0, 8))
            ttk_module.Button(sidebar, text=tr("◈  调查 Bug"), command=self.show_new_page, style="Action.TButton").pack(fill="x", pady=(0, 8))
            ttk_module.Button(sidebar, text=tr("◫  历史 PR 练习"), command=self.show_practice_page, style="Action.TButton").pack(fill="x", pady=(0, 8))
            ttk_module.Button(sidebar, text=tr("🔭  AI 挑选 Issue"), command=self._open_issue_scout_dialog, style="Action.TButton").pack(fill="x", pady=(0, 28))
            ttk_module.Label(sidebar, text=tr("最近案件"), style="SidebarTitle.TLabel", font=self._font("SF Pro Text", 11, "bold")).pack(anchor="w")
            ttk_module.Label(sidebar, text=tr("保存在本地 · 最多显示 10 个"), style="SidebarHint.TLabel").pack(anchor="w", pady=(3, 12))
            self.recent_list = tk_module.Listbox(
                sidebar,
                borderwidth=0,
                highlightthickness=0,
                background=self.SIDEBAR,
                foreground=self.MUTED,
                selectbackground=self.SURFACE_ALT,
                selectforeground=self.TEXT,
                activestyle="none",
                font=self._font("SF Pro Text", 10),
                selectborderwidth=0,
            )
            self.recent_list.pack(fill="both", expand=True)
            self.recent_list.bind("<<ListboxSelect>>", self._open_selected_recent)
            # 设置入口必须全局可见（此前只存在于案件工作台，新用户在默认页找不到）。
            ttk_module.Button(sidebar, text=tr("⚙ 设置"), command=self._open_settings, style="Ghost.TButton").pack(side="bottom", fill="x", pady=(12, 0))

            self.page_host = ttk_module.Frame(shell, style="App.TFrame", padding=(34, 28, 34, 26))
            self.page_host.grid(row=0, column=1, sticky="nsew")
            self.page_host.rowconfigure(0, weight=1)
            self.page_host.columnconfigure(0, weight=1)

            self.new_page = ttk_module.Frame(self.page_host, style="App.TFrame")
            self.report_page = ttk_module.Frame(self.page_host, style="App.TFrame")
            self.result_page = ttk_module.Frame(self.page_host, style="App.TFrame")
            self.practice_page = ttk_module.Frame(self.page_host, style="App.TFrame")
            for page in (self.new_page, self.report_page, self.result_page, self.practice_page):
                page.grid(row=0, column=0, sticky="nsew")

            self._build_report_page(tk_module, ttk_module)
            self._build_new_page(tk_module, ttk_module)
            self._build_practice_page(tk_module, ttk_module)
            self._build_result_page(tk_module, ttk_module)

        def _build_report_page(self, tk_module: Any, ttk_module: Any) -> None:
            page = self.report_page
            page.columnconfigure(0, weight=1)
            page.rowconfigure(2, weight=1)
            header = ttk_module.Frame(page, style="App.TFrame")
            header.grid(row=0, column=0, sticky="ew", pady=(0, 18))
            ttk_module.Label(header, text="REPORT A BUG", style="Eyebrow.TLabel").pack(anchor="w")
            ttk_module.Label(header, text=tr("报告 Blender Bug"), style="Title.TLabel").pack(anchor="w", pady=(4, 3))
            ttk_module.Label(header, text=tr("从发现问题到准备好官方报告。不需要 Blender 源码、终端或 AI。"), style="PageSubtitle.TLabel", wraplength=740).pack(anchor="w")

            intro = ttk_module.LabelFrame(page, text=tr("开始"), style="Dark.TLabelframe", padding=18)
            intro.grid(row=1, column=0, sticky="ew", pady=(0, 14))
            ttk_module.Label(intro, text=tr("填写亲自看到的现象，准备最小复现文件，核对后复制到 Blender 官方表单。"), style="Body.TLabel", wraplength=710).pack(anchor="w")
            ttk_module.Label(intro, text=tr("适用于 Blender 程序本身。扩展和文档问题请先查看官方报告指南。"), style="Muted.TLabel", wraplength=710).pack(anchor="w", pady=(5, 12))
            buttons = ttk_module.Frame(intro, style="Surface.TFrame")
            buttons.pack(anchor="w")
            ttk_module.Button(buttons, text=tr("新建 Bug 报告  →"), command=self._new_standalone_report, style="Primary.TButton").pack(side="left")
            self.import_case_button = ttk_module.Button(buttons, text=tr("从当前调查导入"), command=self._import_case_report, style="Action.TButton", state="disabled")
            self.import_case_button.pack(side="left", padx=(10, 0))

            saved = ttk_module.LabelFrame(page, text=tr("本地报告草稿"), style="Dark.TLabelframe", padding=14)
            saved.grid(row=2, column=0, sticky="nsew")
            saved.rowconfigure(0, weight=1)
            saved.columnconfigure(0, weight=1)
            self.report_list = tk_module.Listbox(saved, background=self.SURFACE_ALT, foreground=self.TEXT,
                                                  selectbackground=self.BORDER, borderwidth=0, font=self._font("SF Pro Text", 11))
            self.report_list.grid(row=0, column=0, sticky="nsew")
            self.report_list.bind("<Double-Button-1>", self._open_selected_report)
            ttk_module.Button(saved, text=tr("继续编辑所选报告"), command=self._open_selected_report, style="Action.TButton").grid(row=1, column=0, sticky="e", pady=(10, 0))

        def _build_practice_page(self, tk_module: Any, ttk_module: Any) -> None:
            page = self.practice_page
            page.columnconfigure(0, weight=2)
            page.columnconfigure(1, weight=3)
            page.rowconfigure(2, weight=1)
            header = ttk_module.Frame(page, style="App.TFrame")
            header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 16))
            ttk_module.Label(header, text="HISTORICAL PRACTICE", style="Eyebrow.TLabel").pack(anchor="w")
            ttk_module.Label(header, text=tr("历史 PR 练习场"), style="Title.TLabel").pack(anchor="w", pady=(4, 3))
            ttk_module.Label(header, text=tr("在不知道答案的前提下调查真实 Blender Bug，然后与真实修复对照。"), style="PageSubtitle.TLabel").pack(anchor="w")

            steps = ttk_module.Frame(page, style="App.TFrame")
            steps.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 16))
            for index, label in enumerate((tr("选择案例"), tr("调查"), tr("提交判断"), tr("揭晓"), tr("评分"))):
                ttk_module.Label(steps, text=f"{index + 1}  {label}", style="StepActive.TLabel" if index == 0 else "Step.TLabel").pack(side="left", padx=(0, 7))

            list_card = ttk_module.LabelFrame(page, text=tr("案例库 · 10"), style="Dark.TLabelframe", padding=12)
            list_card.grid(row=2, column=0, sticky="nsew", padx=(0, 12))
            list_card.rowconfigure(0, weight=1)
            list_card.columnconfigure(0, weight=1)
            self.practice_list = tk_module.Listbox(
                list_card,
                borderwidth=0,
                highlightthickness=0,
                background=self.SURFACE,
                foreground=self.MUTED,
                selectbackground=self.SURFACE_ALT,
                selectforeground=self.TEXT,
                activestyle="none",
                font=self._font("SF Pro Text", 10),
            )
            self.practice_list.grid(row=0, column=0, sticky="nsew")
            self.practice_list.bind("<<ListboxSelect>>", self._select_practice_case)

            detail = ttk_module.LabelFrame(page, text=tr("练习题面"), style="Dark.TLabelframe", padding=20)
            detail.grid(row=2, column=1, sticky="nsew")
            detail.columnconfigure(0, weight=1)
            detail.rowconfigure(4, weight=1)
            self.practice_meta_var = tk_module.StringVar()
            self.practice_title_var = tk_module.StringVar(value=tr("选择左侧案例"))
            self.practice_symptom_var = tk_module.StringVar(value=tr("这里会显示练习时允许看到的问题信息。"))
            self.practice_steps_var = tk_module.StringVar()
            self.practice_status_var = tk_module.StringVar()
            ttk_module.Label(detail, textvariable=self.practice_meta_var, style="Status.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Label(detail, textvariable=self.practice_title_var, style="Heading.TLabel", font=self._font("SF Pro Display", 18, "bold"), wraplength=520, justify="left").grid(row=1, column=0, sticky="w", pady=(7, 8))
            ttk_module.Label(detail, textvariable=self.practice_symptom_var, style="Body.TLabel", wraplength=520, justify="left").grid(row=2, column=0, sticky="w")
            ttk_module.Label(detail, text=tr("复现轮廓"), style="Muted.TLabel", font=self._font("SF Pro Text", 9, "bold")).grid(row=3, column=0, sticky="w", pady=(18, 5))
            ttk_module.Label(detail, textvariable=self.practice_steps_var, style="Body.TLabel", wraplength=520, justify="left").grid(row=4, column=0, sticky="nw")
            ttk_module.Label(detail, textvariable=self.practice_status_var, style="Status.TLabel").grid(row=5, column=0, sticky="w", pady=(12, 8))
            self.practice_start_button = ttk_module.Button(detail, text=tr("进入修复前版本并开始调查  →"), command=self._start_practice, style="Primary.TButton", state="disabled")
            self.practice_start_button.grid(row=6, column=0, sticky="ew")

        def _build_new_page(self, tk_module: Any, ttk_module: Any) -> None:
            page = self.new_page
            page.columnconfigure(0, weight=1)
            page.rowconfigure(3, weight=1)
            header = ttk_module.Frame(page, style="App.TFrame")
            header.grid(row=0, column=0, sticky="ew", pady=(0, 18))
            ttk_module.Label(header, text="NEW INVESTIGATION", style="Eyebrow.TLabel").pack(anchor="w")
            ttk_module.Label(header, text=tr("开始一次新的调查"), style="Title.TLabel").pack(anchor="w", pady=(4, 3))
            ttk_module.Label(header, text=tr("选择源码，粘贴问题。剩下的交给 BugCompass。"), style="PageSubtitle.TLabel").pack(anchor="w")

            steps = ttk_module.Frame(page, style="App.TFrame")
            steps.grid(row=1, column=0, sticky="w", pady=(0, 18))
            for index, label in enumerate((tr("1  选择源码"), tr("2  描述问题"), tr("3  开始调查"))):
                ttk_module.Label(steps, text=label, style="StepActive.TLabel" if index == 0 else "Step.TLabel").pack(side="left", padx=(0, 8))

            repo_card = ttk_module.LabelFrame(page, text=tr("01  /  BLENDER 源码"), style="Dark.TLabelframe", padding=20)
            repo_card.grid(row=2, column=0, sticky="ew", pady=(0, 14))
            repo_card.columnconfigure(0, weight=1)
            ttk_module.Label(repo_card, text=tr("本地源码仓库"), style="Heading.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Label(repo_card, text=tr("只会读取源码和 Git 信息，创建案件时不会修改 Blender。"), style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 0))
            path_row = ttk_module.Frame(repo_card, style="Card.TFrame")
            path_row.grid(row=2, column=0, sticky="ew", pady=(13, 8))
            path_row.columnconfigure(0, weight=1)
            ttk_module.Entry(path_row, textvariable=self.repo_var, state="readonly", style="Dark.TEntry").grid(row=0, column=0, sticky="ew", padx=(0, 10), ipady=3)
            ttk_module.Button(path_row, text=tr("选择源码文件夹"), command=self._choose_repo, style="Action.TButton").grid(row=0, column=1)
            self.repo_status_label = ttk_module.Label(repo_card, textvariable=self.repo_status_var, style="Muted.TLabel")
            self.repo_status_label.grid(row=3, column=0, sticky="w")

            bug_card = ttk_module.LabelFrame(page, text=tr("02  /  BUG 描述"), style="Dark.TLabelframe", padding=20)
            bug_card.grid(row=3, column=0, sticky="nsew", pady=(0, 14))
            bug_card.columnconfigure(0, weight=1)
            bug_card.rowconfigure(1, weight=1)
            title_row = ttk_module.Frame(bug_card, style="Card.TFrame")
            title_row.grid(row=0, column=0, sticky="ew", pady=(0, 10))
            title_row.columnconfigure(0, weight=1)
            title_text = ttk_module.Frame(title_row, style="Card.TFrame")
            title_text.grid(row=0, column=0, sticky="w")
            ttk_module.Label(title_text, text=tr("把问题原样贴进来"), style="Heading.TLabel").pack(anchor="w")
            ttk_module.Label(title_text, text=tr("标题、复现步骤、日志都可以。这里的内容不会被当成命令执行。"), style="Muted.TLabel").pack(anchor="w", pady=(3, 0))
            ttk_module.Button(title_row, text=tr("↑  导入 .md / .txt"), command=self._import_issue, style="Ghost.TButton").grid(row=0, column=1)
            self.bug_text = tk_module.Text(
                bug_card,
                wrap="word",
                undo=True,
                relief="flat",
                borderwidth=0,
                highlightthickness=1,
                highlightbackground=self.BORDER,
                highlightcolor=self.ORANGE,
                background=self.SURFACE_ALT,
                foreground=self.TEXT,
                selectbackground="#654127",
                selectforeground=self.TEXT,
                padx=14,
                pady=12,
                font=self._font("SF Pro Text", 11),
            )
            self.bug_text.grid(row=1, column=0, sticky="nsew")
            self.bug_text.bind("<<Modified>>", self._update_char_count)
            ttk_module.Label(bug_card, textvariable=self.char_count_var, style="Muted.TLabel").grid(row=2, column=0, sticky="e", pady=(6, 0))

            engine_row = ttk_module.Frame(page, style="App.TFrame")
            engine_row.grid(row=4, column=0, sticky="ew", pady=(0, 12))
            ttk_module.Label(engine_row, text=tr("调查引擎"), style="Muted.TLabel", font=self._font("SF Pro Text", 9, "bold")).pack(side="left", padx=(0, 8))
            self.engine_combo = ttk_module.Combobox(
                engine_row,
                textvariable=self.engine_var,
                values=self._engine_options(),
                state="readonly",
                width=32,
                style="Dark.TCombobox",
            )
            self.engine_combo.pack(side="left")
            self.engine_combo.bind("<<ComboboxSelected>>", self._on_engine_changed)
            ttk_module.Label(engine_row, text=tr("默认 Codex CLI；大模型 API 可在设置里配置与测试"), style="Muted.TLabel").pack(side="left", padx=(10, 0))
            self._engine_key_button = ttk_module.Button(engine_row, text=tr("导入密钥…"), command=self._import_key_for_current_engine, style="Primary.TButton")
            self._refresh_engine_key_button()

            action_row = ttk_module.Frame(page, style="App.TFrame")
            action_row.grid(row=5, column=0, sticky="ew")
            action_row.columnconfigure(0, weight=1)
            self.progress_label = ttk_module.Label(action_row, textvariable=self.progress_var, style="PageStatus.TLabel")
            self.progress_label.grid(row=0, column=0, sticky="w")
            self.create_button = ttk_module.Button(action_row, text=tr("开始调查  →"), command=self._start_create, style="Primary.TButton")
            self.create_button.grid(row=0, column=1, sticky="e")

        def _build_result_page(self, tk_module: Any, ttk_module: Any) -> None:
            page = self.result_page
            page.columnconfigure(0, weight=1)
            page.rowconfigure(4, weight=1)
            header = ttk_module.Frame(page, style="App.TFrame")
            header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
            ttk_module.Label(header, text="INVESTIGATION WORKSPACE", style="Eyebrow.TLabel").pack(anchor="w")
            ttk_module.Label(header, text=tr("调查工作台"), style="Title.TLabel").pack(anchor="w", pady=(4, 8))
            self.result_flow = ttk_module.Frame(header, style="App.TFrame")
            self.result_flow.pack(anchor="w")
            # 「下一步」引导条：随时只推荐一个此刻最该做的动作（内容随案件状态刷新）。
            self.next_step_frame = ttk_module.Frame(header, style="Elevated.TFrame", padding=(14, 11))
            self.next_step_frame.pack(fill="x", pady=(12, 0))
            self.next_step_label = ttk_module.Label(
                self.next_step_frame, text="", background=self.SURFACE_ALT, foreground=self.MUTED,
                font=self._font("SF Pro Text", 10, "bold"), justify="left", wraplength=560, anchor="w",
            )
            self.next_step_label.pack(side="left", fill="x", expand=True)
            self.next_step_button = ttk_module.Button(self.next_step_frame, text="", command=self._run_next_step, style="Primary.TButton")

            summary_card = ttk_module.LabelFrame(page, text=tr("案件概览"), style="Dark.TLabelframe", padding=16)
            summary_card.grid(row=1, column=0, sticky="ew", pady=(0, 10))
            summary_card.columnconfigure(0, weight=1)
            summary_card.columnconfigure(1, minsize=210)
            ttk_module.Label(summary_card, textvariable=self.result_summary_var, style="Body.TLabel", justify="left", wraplength=560).grid(row=0, column=0, sticky="w")
            codex_panel = ttk_module.Frame(summary_card, style="Elevated.TFrame", padding=(14, 11))
            codex_panel.grid(row=0, column=1, rowspan=2, sticky="ne", padx=(18, 0))
            self.codex_status_label = ttk_module.Label(codex_panel, textvariable=self.codex_status_var, background=self.SURFACE_ALT, foreground=self.MUTED, font=self._font("SF Pro Text", 11, "bold"))
            self.codex_status_label.pack(anchor="w")
            ttk_module.Label(codex_panel, textvariable=self.codex_status_detail_var, background=self.SURFACE_ALT, foreground=self.MUTED, font=self._font("SF Pro Text", 9), wraplength=190, justify="left").pack(anchor="w", pady=(4, 0))
            ttk_module.Label(summary_card, textvariable=self.result_hint_var, style="Status.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
            self.details_button = ttk_module.Button(summary_card, text=tr("查看环境详情"), command=self._toggle_details, style="Ghost.TButton")
            self.details_button.grid(row=2, column=0, sticky="w", pady=(8, 0))
            ttk_module.Button(summary_card, text=tr("整理可复现报告包…"), command=self._open_repro_report_dialog, style="Action.TButton").grid(row=2, column=1, sticky="e", pady=(8, 0))
            self.details_frame = ttk_module.Frame(summary_card, style="Card.TFrame")
            self.details_text = tk_module.Text(self.details_frame, height=7, wrap="none", font=self._font("SF Mono", 9), relief="flat", borderwidth=0, background=self.SURFACE_ALT, foreground=self.MUTED, padx=10, pady=10)
            self.details_text.pack(fill="both", expand=True, pady=(6, 0))

            actions = ttk_module.Frame(page, style="App.TFrame")
            actions.grid(row=2, column=0, sticky="ew", pady=(0, 10))
            self.copy_button = ttk_module.Button(actions, text=tr("复制调查指令（备用）"), command=self._copy_instruction, style="Action.TButton")
            self.copy_button.pack(side="left")
            ttk_module.Button(actions, text=tr("刷新结果"), command=self._refresh_current_case, style="Action.TButton").pack(side="left", padx=10)
            ttk_module.Button(actions, text=tr("新建另一个调查"), command=self._new_another, style="Action.TButton").pack(side="left")
            self.continue_button = ttk_module.Button(actions, text=tr("继续调查  ▶"), command=self._continue_investigation, style="Primary.TButton", state="disabled")
            self.continue_button.pack(side="left", padx=(10, 0))
            self.cancel_button = ttk_module.Button(actions, text=tr("停止调查  ■"), command=self._cancel_investigation, style="Action.TButton", state="disabled")
            self.cancel_button.pack(side="left", padx=(8, 0))
            ttk_module.Button(actions, text=tr("备份"), command=self._backup_workspace, style="Action.TButton").pack(side="left", padx=(8, 0))
            ttk_module.Button(actions, text=tr("恢复备份…"), command=self._restore_backup_dialog, style="Action.TButton").pack(side="left", padx=(8, 0))
            ttk_module.Button(actions, text=tr("导出诊断包"), command=self._export_diagnostics, style="Action.TButton").pack(side="left", padx=(8, 0))
            ttk_module.Button(actions, text=tr("⚙ 设置"), command=self._open_settings, style="Ghost.TButton").pack(side="left", padx=(8, 0))
            self.submit_judgment_button = ttk_module.Button(actions, text=tr("提交根因判断"), command=self._open_judgment_dialog, style="Primary.TButton")
            self.reveal_button = ttk_module.Button(actions, text=tr("揭晓真实修复"), command=self._reveal_practice, style="Action.TButton")
            ttk_module.Label(actions, textvariable=self.copy_status_var, style="PageStatus.TLabel").pack(side="left", padx=12)

            timeline_row = ttk_module.Frame(page, style="App.TFrame")
            timeline_row.grid(row=3, column=0, sticky="ew", pady=(0, 10))
            ttk_module.Label(timeline_row, text="LIVE", style="Eyebrow.TLabel").pack(side="left", padx=(0, 12))
            self.timeline = tk_module.Listbox(timeline_row, height=2, borderwidth=0, highlightthickness=0, background=self.SURFACE_ALT, foreground=self.MUTED, selectbackground=self.SURFACE_ALT, activestyle="none", font=self._font("SF Pro Text", 9))
            self.timeline.pack(side="left", fill="x", expand=True, ipady=4)

            # 滚动容器：统一滚轮路由 + 整像素滚动 + 自动换行（Windows 拖影/断层/裁切的修复）。
            self.cards_scroll = ScrollableFrame(
                page,
                tk_module,
                ttk_module,
                self.wheel_router,
                background=self.BACKGROUND,
            )
            page.rowconfigure(4, weight=1)
            self.cards_scroll.grid(row=4, column=0, sticky="nsew")
            self.cards_canvas = self.cards_scroll.canvas  # 兼容旧引用
            self.cards_host = self.cards_scroll.content
            self.cards_scroll.set_zoom_callback(self._on_cards_zoom)
            page.columnconfigure(1, weight=0, minsize=330)
            self._build_qa_panel(tk_module, ttk_module)

        def _build_qa_panel(self, tk_module: Any, ttk_module: Any) -> None:
            """右侧常驻的追问面板：就当前案件反复问引擎，只存对话不改调查结果。"""
            panel = ttk_module.LabelFrame(self.result_page, text=tr("追问引擎"), style="Dark.TLabelframe", padding=14)
            panel.grid(row=1, column=1, rowspan=4, sticky="nsew", padx=(14, 0))
            head = ttk_module.Frame(panel, style="Card.TFrame")
            head.pack(fill="x")
            ttk_module.Label(head, text="ASK THE ENGINE", style="Eyebrow.TLabel").pack(side="left")
            self.qa_clear_button = ttk_module.Button(head, text=tr("清空对话"), command=self._clear_conversation, style="Ghost.TButton")
            self.qa_clear_button.pack(side="right")
            ttk_module.Label(
                panel, text=tr("就当前案件追问引擎。追问只保存对话（qa.json），不会改动调查结果。"),
                style="Muted.TLabel", wraplength=280, justify="left",
            ).pack(anchor="w", pady=(6, 2))
            # 引擎名单独一行常驻：状态行只报当下发生的事，不该把它顶掉。
            ttk_module.Label(panel, textvariable=self.qa_engine_var, style="Muted.TLabel", wraplength=280, justify="left").pack(
                anchor="w", pady=(0, 8)
            )

            self.qa_transcript = tk_module.Text(
                panel, wrap="word", state="disabled", relief="flat", borderwidth=0, highlightthickness=1,
                highlightbackground=self.BORDER, background=self.SURFACE_ALT, foreground=self.TEXT,
                padx=12, pady=10, font=self._font("SF Pro Text", 10), cursor="arrow",
            )
            self.qa_transcript.pack(fill="both", expand=True)
            self.qa_transcript.tag_configure("user", foreground=self.ORANGE, font=self._font("SF Pro Text", 10, "bold"))
            self.qa_transcript.tag_configure("assistant", foreground=self.TEXT, font=self._font("SF Pro Text", 10))
            self.qa_transcript.tag_configure("meta", foreground=self.SUBTLE, font=self._font("SF Pro Text", 9))
            self.qa_transcript.tag_configure("error", foreground=self.RED, font=self._font("SF Pro Text", 10))

            self.qa_input = tk_module.Text(
                panel, height=3, wrap="word", relief="flat", borderwidth=0, highlightthickness=1,
                highlightbackground=self.BORDER, highlightcolor=self.ORANGE, background=self.SURFACE_ALT,
                foreground=self.TEXT, insertbackground=self.TEXT, selectbackground="#654127",
                padx=10, pady=8, font=self._font("SF Pro Text", 10),
            )
            self.qa_input.pack(fill="x", pady=(8, 6))
            self.qa_input.bind("<Return>", self._on_qa_return)
            self.qa_input.bind("<Shift-Return>", lambda _event: None)
            row = ttk_module.Frame(panel, style="Card.TFrame")
            row.pack(fill="x")
            self.qa_send_button = ttk_module.Button(row, text=tr("发送  →"), command=self._ask_question, style="Primary.TButton")
            self.qa_send_button.pack(side="right")
            ttk_module.Label(row, textvariable=self.qa_status_var, style="Muted.TLabel", wraplength=200, justify="left").pack(side="left")

        def show_new_page(self) -> None:
            self.new_page.tkraise()
            self.copy_status_var.set("")
            if not CodexRunner.find_executable() and self.settings.get("active_engine") == "codex":
                self.progress_var.set(tr("源码调查需要先安装并登录 Codex，或在设置中选择已配置的模型服务。"))

        def show_report_page(self) -> None:
            self._refresh_report_list()
            can_import = self.current_case is not None and self.current_case.practice_session_id is None
            self.import_case_button.configure(state="normal" if can_import else "disabled")
            self.report_page.tkraise()

        def _refresh_report_list(self) -> None:
            self.saved_reports = self.report_store.list_reports()
            self.report_list.delete(0, "end")
            for item in self.saved_reports:
                status = tr("材料待补") if not item.ready_for_review else tr("待人工核对")
                self.report_list.insert("end", f"{item.title}  ·  {status}  ·  {item.report_id}")
            if not self.saved_reports:
                self.report_list.insert("end", tr("还没有报告草稿；点击上方新建。"))

        def _open_report(self, report_id: str) -> None:
            try:
                open_report_editor(self, self.report_store, report_id, on_saved=self._refresh_report_list)
            except (BugCompassError, OSError) as exc:
                self.message_box.showerror(tr("无法打开报告"), str(exc), parent=self)

        def _new_standalone_report(self) -> None:
            try:
                report_id = self.report_store.create()
            except (BugCompassError, OSError) as exc:
                self.message_box.showerror(tr("无法新建报告"), str(exc), parent=self)
                return
            self._refresh_report_list()
            self._open_report(report_id)

        def _import_case_report(self) -> None:
            if self.current_case is None or self.current_case.practice_session_id is not None:
                return
            try:
                report_id = self.report_store.create(from_case=self.current_case.case_dir)
            except (BugCompassError, OSError) as exc:
                self.message_box.showerror(tr("无法导入调查"), str(exc), parent=self)
                return
            self._refresh_report_list()
            self.message_box.showinfo(tr("调查内容已导入"), tr("请逐项核对事实和附件；调查摘要不能替代亲自复现。"), parent=self)
            self._open_report(report_id)

        def _open_selected_report(self, _event: Any = None) -> None:
            selected = self.report_list.curselection()
            if not selected or selected[0] >= len(self.saved_reports):
                return
            self._open_report(self.saved_reports[selected[0]].report_id)

        def show_practice_page(self) -> None:
            if self.busy:
                self.message_box.showwarning(tr("任务仍在进行"), tr("请先等待当前任务完成，或在正在运行的案件中点击“停止调查”。"), parent=self)
                return
            try:
                self.practice_cases = self.practice_manager.list_cases()
            except BugCompassError as exc:
                self.message_box.showerror(tr("无法读取练习案例"), str(exc), parent=self)
                return
            self.practice_list.delete(0, "end")
            difficulty = {"easy": tr("入门"), "medium": tr("进阶"), "hard": tr("挑战")}
            for index, case in enumerate(self.practice_cases, 1):
                self.practice_list.insert("end", f"{index:02d}   #{case.issue_id}  {difficulty.get(case.difficulty, case.difficulty)}\n      {case.title}")
            self.practice_title_var.set(tr("选择左侧案例"))
            self.practice_meta_var.set(tr("10 个真实修复 · 答案已隐藏"))
            self.practice_symptom_var.set(tr("选择一个案例，BugCompass 会创建隔离源码副本并切换到修复前版本。"))
            self.practice_steps_var.set("")
            self.practice_status_var.set("")
            self.practice_start_button.configure(state="disabled")
            self.practice_page.tkraise()

        def _select_practice_case(self, _event: Any = None) -> None:
            selection = self.practice_list.curselection()
            if not selection:
                return
            index = int(selection[0])
            if index >= len(self.practice_cases):
                return
            case = self.practice_cases[index]
            difficulty = {"easy": tr("入门"), "medium": tr("进阶"), "hard": tr("挑战")}
            self.practice_meta_var.set(
                tr('#{}  ·  {}  ·  建议 {} 分钟  ·  {} 次 AI').format(case.issue_id, difficulty.get(case.difficulty, case.difficulty), case.suggested_minutes, case.suggested_ai_runs)
            )
            self.practice_title_var.set(case.title)
            self.practice_symptom_var.set(case.symptom)
            self.practice_steps_var.set("\n".join(f"{i}.  {step}" for i, step in enumerate(case.reproduction, 1)))
            self.practice_status_var.set(tr('修复前 commit  {}  ·  真实修复保持隐藏').format(case.pre_fix_commit[:12]))
            self.practice_start_button.configure(state="normal")

        def _start_practice(self) -> None:
            if self.busy:
                return
            selection = self.practice_list.curselection()
            if not selection:
                return
            case = self.practice_cases[int(selection[0])]
            repo_to_initialize: str | None = None
            if not (self.controller.workspace_path / "project.json").is_file():
                selected = self.file_dialog.askdirectory(title=tr("选择 Blender 源码文件夹"), mustexist=True)
                if not selected:
                    return
                validation = self.controller.validate_repository(selected)
                if not validation.valid:
                    self.message_box.showerror(tr("Blender 源码无效"), validation.message, parent=self)
                    return
                repo_to_initialize = str(validation.repo_path)
                self.repo_var.set(repo_to_initialize)
            self.busy = True
            self.practice_start_button.configure(state="disabled")
            self.practice_status_var.set(tr("正在准备修复前源码副本……"))
            self.codex_runner.reset_cancellation()

            def worker() -> None:
                view: CaseView | None = None
                try:
                    if repo_to_initialize is not None:
                        self.controller.prepare_workspace(
                            repo_to_initialize,
                            progress=lambda text: self.events.put(("practice_progress", text)),
                        )
                    case_id, _checkout = self.practice_manager.start_session(
                        case.case_id, progress=lambda text: self.events.put(("practice_progress", text))
                    )
                    self.controller.set_case_status(case_id, "investigating")
                    view = self.controller.load_case(case_id)
                    self.practice_manager.increment_ai_runs(case_id)
                    self.events.put(("practice_created", view))
                    result = self.codex_runner.run(
                        view,
                        progress=lambda text, case_id=view.case_id: self.events.put(("codex_progress", (case_id, text))),
                        action="practice_initial",
                    )
                    self._finish_codex_run(view, result)
                except CodexRunBusyError as exc:
                    self.events.put(("error", (view.case_id if view else None, str(exc))))
                except Exception as exc:
                    if view is not None:
                        try:
                            self.controller.set_case_status(view.case_id, "failed")
                        except BugCompassError:
                            pass
                    message = str(exc) if isinstance(exc, (BugCompassError, OSError)) else tr("创建历史练习时发生意外错误。")
                    self.events.put(("error", (view.case_id if view else None, message)))

            threading.Thread(target=worker, daemon=False).start()
            self.after(100, self._poll_events)

        def _choose_repo(self) -> None:
            selected = self.file_dialog.askdirectory(title=tr("选择 Blender 源码文件夹"), mustexist=True)
            if not selected:
                return
            validation = self.controller.validate_repository(selected)
            self.repo_var.set(str(validation.repo_path))
            self.repo_status_var.set(validation.message)
            self.repo_status_label.configure(style="Status.TLabel" if validation.valid else "Muted.TLabel")

        def _import_issue(self) -> None:
            selected = self.file_dialog.askopenfilename(
                title=tr("导入 Bug 描述"),
                filetypes=((tr("Markdown 或文本文件"), "*.md *.txt"), (tr("Markdown 文件"), "*.md"), (tr("文本文件"), "*.txt")),
            )
            if not selected:
                return
            try:
                content = Path(selected).read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                self.message_box.showerror(tr("无法导入文件"), tr('无法读取所选文件：\n{}').format(exc), parent=self)
                return
            self.bug_text.delete("1.0", "end")
            self.bug_text.insert("1.0", content)
            self._set_char_count()

        def _update_char_count(self, _event: Any = None) -> None:
            if self.bug_text.edit_modified():
                self._set_char_count()
                self.bug_text.edit_modified(False)

        def _set_char_count(self) -> None:
            count = len(self.bug_text.get("1.0", "end-1c"))
            self.char_count_var.set(tr('{} 个字符').format(count))

        def _start_create(self) -> None:
            if self.busy:
                return
            repo = self.repo_var.get().strip()
            bug_text = self.bug_text.get("1.0", "end-1c")
            if not repo:
                self.message_box.showwarning(tr("请选择源码"), tr("请先选择本地 Blender 源码文件夹。"), parent=self)
                return
            if not bug_text.strip():
                self.message_box.showwarning(tr("请填写 Bug 描述"), tr("Bug 描述不能为空。"), parent=self)
                return
            validation = self.controller.validate_repository(repo)
            if not validation.valid:
                self.message_box.showerror(tr("Blender 源码无效"), validation.message, parent=self)
                return

            self.busy = True
            runner = self._engine_runner()
            self._active_runner = runner
            runner.reset_cancellation()
            self.create_button.configure(state="disabled")
            self.progress_var.set(tr("正在创建案件并准备调查引擎……"))

            def worker() -> None:
                view: CaseView | None = None
                try:
                    view = self.controller.create_investigation(
                        repo,
                        bug_text,
                        progress=lambda text: self.events.put(("progress", text)),
                    )
                    self.controller.set_case_status(view.case_id, "investigating")
                    view = self.controller.load_case(view.case_id)
                    self.events.put(("case_created", view))
                    result = runner.run(
                        view,
                        progress=lambda text, case_id=view.case_id: self.events.put(("codex_progress", (case_id, text))),
                    )
                    self._finish_codex_run(view, result)
                except CodexRunBusyError as exc:
                    self.events.put(("error", (view.case_id if view else None, str(exc))))
                except Exception as exc:
                    if view is not None:
                        try:
                            self.controller.set_case_status(view.case_id, "failed")
                        except BugCompassError:
                            pass
                    message = str(exc) if isinstance(exc, (BugCompassError, OSError)) else tr("创建案件时发生意外错误。")
                    self.events.put(("error", (view.case_id if view else None, message)))

            threading.Thread(target=worker, daemon=False).start()
            self.after(100, self._poll_events)

        def _poll_events(self) -> None:
            while True:
                try:
                    kind, payload = self.events.get_nowait()
                except queue.Empty:
                    break
                if kind == "progress":
                    self.progress_var.set(str(payload))
                elif kind == "practice_progress":
                    self.practice_status_var.set(str(payload))
                elif kind == "case_created":
                    self.codex_active = True
                    self.active_case_id = payload.case_id
                    self.current_case = payload
                    self._show_case(payload)
                    self._set_codex_state("working", tr("正在读取案件和 Blender 源码。"))
                    self.result_hint_var.set(tr("案件已创建，{} 正在自动调查……").format(self._engine_display_name()))
                    self._refresh_recent_cases()
                elif kind == "practice_created":
                    self.codex_active = True
                    self.active_case_id = payload.case_id
                    self.current_case = payload
                    self.current_reveal = None
                    self._show_case(payload)
                    self._set_codex_state("working", tr("正在调查修复前版本，答案仍隐藏。"))
                    self.result_hint_var.set(tr("历史练习已开始：当前是修复前源码，真实答案保持隐藏。"))
                    self._refresh_recent_cases()
                elif kind == "codex_progress":
                    case_id, message = payload
                    if case_id == self.active_case_id and self.codex_state != "stopping":
                        self._set_codex_state("working", str(message))
                    if self.current_case is not None and self.current_case.case_id == case_id:
                        self.result_hint_var.set(str(message))
                        self._add_timeline(str(message))
                elif kind == "experiment_complete":
                    view, experiment_id, record = payload
                    self.busy = False
                    if self.current_case is not None and self.current_case.case_id == view.case_id:
                        self._add_timeline(
                            tr('实验 {} 执行完成（返回码 {}），正在请 {} 更新假设。').format(experiment_id, record['return_code'], self._engine_display_name())
                        )
                    self._run_followup("experiment", experiment_id, case_id=view.case_id)
                elif kind == "success":
                    finished_case_id = payload.case_id
                    self.busy = False
                    self.codex_active = False
                    self.active_case_id = None
                    self.create_button.configure(state="normal")
                    if self.practice_list.curselection():
                        self.practice_start_button.configure(state="normal")
                    self.progress_var.set("")
                    if self.current_case is not None and self.current_case.case_id == finished_case_id:
                        self._show_case(payload)
                        self.result_hint_var.set(tr("{} 调查完成，结果已自动刷新。").format(self._engine_display_name()))
                    elif self.current_case is not None:
                        self._show_case(self.controller.load_case(self.current_case.case_id))
                        self.copy_status_var.set(tr('案件 {} 的调查已完成。').format(finished_case_id))
                    self._refresh_recent_cases()
                elif kind == "cancelled":
                    finished_case_id = payload.case_id
                    self.busy = False
                    self.codex_active = False
                    self.active_case_id = None
                    self.create_button.configure(state="normal")
                    if self.practice_list.curselection():
                        self.practice_start_button.configure(state="normal")
                    self.progress_var.set("")
                    if self.current_case is not None and self.current_case.case_id == finished_case_id:
                        self._show_case(payload)
                        self.result_hint_var.set(tr("调查已取消；案件内容已保留，可以稍后重试。"))
                    elif self.current_case is not None:
                        self._show_case(self.controller.load_case(self.current_case.case_id))
                        self.copy_status_var.set(tr('案件 {} 的调查已停止。').format(finished_case_id))
                    self._refresh_recent_cases()
                elif kind == "error":
                    failed_case_id, message = payload
                    self.busy = False
                    if failed_case_id is None or failed_case_id == self.active_case_id:
                        self.codex_active = False
                        self.active_case_id = None
                    self.create_button.configure(state="normal")
                    if self.practice_list.curselection():
                        self.practice_start_button.configure(state="normal")
                    self.progress_var.set("")
                    if self.current_case is not None:
                        try:
                            self._show_case(self.controller.load_case(self.current_case.case_id))
                            if self.current_case.case_id == failed_case_id:
                                self.result_hint_var.set(tr("Codex 调查未完成；案件已经保存在本地。"))
                            elif failed_case_id:
                                self.copy_status_var.set(tr('案件 {} 的操作失败。').format(failed_case_id))
                        except BugCompassError:
                            pass
                    self._refresh_recent_cases()
                    self.message_box.showerror(tr("操作未完成"), str(message), parent=self)
                elif kind == "ask_progress":
                    case_id, message = payload
                    if self.ask_case_id == case_id:
                        self.qa_status_var.set(str(message))
                elif kind == "ask_done":
                    asked_view, _question, answer = payload
                    self.ask_busy = False
                    self.ask_case_id = None
                    try:
                        qa.append_turn(asked_view.case_dir, "assistant", answer, engine=self._engine_display_name())
                    except BugCompassError as exc:
                        self.qa_status_var.set(str(exc))
                    if self.current_case is not None and self.current_case.case_id == asked_view.case_id:
                        self._render_conversation(asked_view.case_dir)
                        self.qa_status_var.set(tr("回答完成，追问记录已保存在案件里。"))
                    self._set_qa_busy_state()
                elif kind == "ask_error":
                    failed_ask_case, message, question = payload
                    self.ask_busy = False
                    self.ask_case_id = None
                    self._set_qa_busy_state()
                    if self.current_case is not None and self.current_case.case_id == failed_ask_case:
                        self._append_qa_error(str(message))
                        # 没答上来的问题放回输入框，别让用户重打一遍。
                        if not self.qa_input.get("1.0", "end-1c").strip():
                            self.qa_input.insert("1.0", question)
                        self.qa_status_var.set(tr("追问没有成功。"))
            if self.busy or self.ask_busy:
                self.after(100, self._poll_events)

        def _finish_codex_run(self, view: CaseView, result: CodexRunResult) -> None:
            # 无论成败都先落盘指标（本地）；上报仅在用户开启后记录计数。
            try:
                self._record_run_metrics(view)
            except Exception:
                pass
            if result.cancelled:
                self.controller.set_case_status(view.case_id, "cancelled")
                self.events.put(("cancelled", self.controller.load_case(view.case_id)))
                return
            if result.returncode != 0:
                self.controller.set_case_status(view.case_id, "failed")
                self.events.put(("error", (view.case_id, self._friendly_codex_error(result))))
                return
            refreshed = self.controller.load_case(view.case_id)
            if not result.investigation_updated or refreshed.awaiting_investigation:
                self.controller.set_case_status(view.case_id, "failed")
                self.events.put(("error", (view.case_id, tr("Codex 已结束，但没有写入调查结果。案件已保留，请检查 Codex 登录状态后重试。"))))
                return
            self.controller.set_case_status(view.case_id, "complete")
            self.events.put(("success", self.controller.load_case(view.case_id)))

        def _friendly_codex_error(self, result: CodexRunResult) -> str:
            if getattr(result, "engine", "") == "llm":
                # 大模型引擎的错误信息在生成时已是用户友好的中文指引。
                return result.error_detail or tr("大模型调查未完成。")
            detail = result.error_detail.lower()
            error_message = self.codex_runner.extract_error_message(result.error_detail)
            if result.timed_out:
                return (
                    tr('Codex 在 {} 秒内没有完成，BugCompass 已自动停止它，避免继续消耗额度。案件和已收集内容都已保留，可以稍后继续。').format(int(self.codex_runner.timeout_seconds))
                )
            if "invalid_json_schema" in detail:
                return tr("BugCompass 的结构化输出格式不兼容，Codex 尚未开始实际调查。案件已经保存在本地，修复格式后可点击“继续调查”。")
            if any(word in detail for word in ("login", "auth", "unauthorized", "credential")):
                return tr("Codex 尚未登录或登录已失效。请先在终端完成 Codex 登录，然后重试；案件已经保存在本地。")
            if any(word in detail for word in ("network", "connect", "dns", "timed out")):
                return tr("Codex 暂时无法连接服务。请检查网络后重试；案件已经保存在本地。")
            if error_message:
                return tr('Codex 返回错误：{}\n详细记录已保存到 Case 的 codex-last-run.json。').format(error_message[:500])
            return tr('Codex 调查未完成（退出码 {}）。案件已经保存在本地，可以使用备用复制按钮继续。').format(result.returncode)

        def _cancel_investigation(self) -> None:
            if (
                not self.busy
                or not self.codex_active
                or self.current_case is None
                or self.current_case.case_id != self.active_case_id
            ):
                return
            self.result_hint_var.set(tr("正在取消当前调查……"))
            self._set_codex_state("stopping", tr("正在安全终止当前调查进程。"))
            runner = self._active_runner or self._engine_runner()
            runner.cancel()

        def _continue_investigation(self) -> None:
            if self.busy or self.current_case is None:
                return
            try:
                view = self.controller.load_case(self.current_case.case_id)
                self.controller.set_case_status(view.case_id, "investigating")
            except BugCompassError as exc:
                self.message_box.showerror(tr("无法继续调查"), str(exc), parent=self)
                return
            self.current_case = view
            self.busy = True
            self.codex_active = True
            self.active_case_id = view.case_id
            runner = self._engine_runner()
            self._active_runner = runner
            runner.reset_cancellation()
            self._refresh_recent_cases()
            self._set_codex_state("working", tr("正在继续同一个案件，不会重新创建 Case。"))
            self.result_hint_var.set(tr("正在继续调查当前案件……"))
            self._add_timeline(tr("已继续当前案件，保留原有证据、预测和用户编辑。"))

            def worker() -> None:
                try:
                    if view.practice_session_id:
                        self.practice_manager.increment_ai_runs(view.case_id)
                    result = runner.run(
                        view,
                        progress=lambda text, case_id=view.case_id: self.events.put(("codex_progress", (case_id, text))),
                        action="continue",
                    )
                    self._finish_codex_run(view, result)
                except CodexRunBusyError as exc:
                    self.events.put(("error", (view.case_id, str(exc))))
                except Exception as exc:
                    try:
                        self.controller.set_case_status(view.case_id, "failed")
                    except BugCompassError:
                        pass
                    message = str(exc) if isinstance(exc, (BugCompassError, OSError)) else tr("继续调查时发生意外错误。")
                    self.events.put(("error", (view.case_id, message)))

            threading.Thread(target=worker, daemon=False).start()
            self.after(100, self._poll_events)

        def _set_codex_state(self, state: str, detail: str) -> None:
            self.codex_state = state
            self.codex_state_detail = detail
            self._render_codex_state()

        def _render_codex_state(self) -> None:
            viewed_case_id = self.current_case.case_id if self.current_case else None
            viewed_status = self.current_case.status if self.current_case else "unknown"
            presentation = codex_status_for_case(
                active_case_id=self.active_case_id if self.codex_active else None,
                run_state=self.codex_state,
                run_detail=self.codex_state_detail,
                viewed_case_id=viewed_case_id,
                viewed_status=viewed_status,
                operation_busy=self.busy,
                engine_label=self._engine_label(),
            )
            colors = {
                "idle": self.MUTED,
                "working": self.GREEN,
                "stopping": self.YELLOW,
                "complete": self.GREEN,
                "failed": self.RED,
                "stopped": self.YELLOW,
                "other": self.YELLOW,
            }
            self.codex_status_var.set(presentation.label)
            self.codex_status_detail_var.set(presentation.detail)
            self.codex_status_label.configure(foreground=colors.get(presentation.state, self.MUTED))
            self.cancel_button.configure(state="normal" if presentation.can_stop else "disabled")
            can_continue = self.current_case is not None and not self.busy
            self.continue_button.configure(state="normal" if can_continue else "disabled")
            next_button = getattr(self, "next_step_button", None)
            if next_button is not None and getattr(self, "_next_step_action", ""):
                next_button.configure(state="disabled" if self.busy else "normal")

        def _on_close(self) -> None:
            if self.codex_active:
                should_close = self.message_box.askyesno(
                    tr("调查仍在进行"),
                    tr("关闭窗口会取消当前 Codex 调查。确定要关闭吗？"),
                    parent=self,
                )
                if not should_close:
                    return
                self.codex_runner.cancel()
            elif self.busy:
                should_close = self.message_box.askyesno(
                    tr("实验仍在进行"),
                    tr("本地实验仍在运行，当前不能用“停止调查”中止它。确定要关闭窗口吗？"),
                    parent=self,
                )
                if not should_close:
                    return
            self.destroy()

        def _show_case(self, view: CaseView) -> None:
            # 同一个案件重渲染（否定路径、记录结论、刷新结果）时保留滚动位置：
            # 长页面里点一下就被弹回页首，是上一版最硌手的地方。
            previous_case_id = self.current_case.case_id if self.current_case is not None else None
            keep_scroll = previous_case_id == view.case_id
            scroll_fraction = self.cards_scroll.canvas.yview()[0] if keep_scroll else 0.0
            self.current_case = view
            # 每次打开案件都刷新本地指标文件（metrics.json），不联网。
            self._record_run_metrics(view)
            if view.practice_session_id:
                self.submit_judgment_button.pack(side="left", padx=(10, 0))
                self.reveal_button.pack(side="left", padx=(8, 0))
                session = self.practice_manager.session_for_case(view.case_id) or {}
                self.reveal_button.configure(state="normal" if session.get("status") in {"submitted", "revealed"} else "disabled")
                self.current_reveal = self.practice_manager.load_reveal(view.case_id)
            else:
                self.submit_judgment_button.pack_forget()
                self.reveal_button.pack_forget()
                self.current_reveal = None
            status_names = {"ok": tr("正常"), "warning": tr("有提醒"), "error": tr("存在错误"), "unknown": tr("未知")}
            capability_value = view.environment.get("checks", {}).get("capabilities", {}).get("value", {})
            capability_names = {"ok": tr("可以"), "warning": tr("条件不足"), "error": tr("不可用")}
            capabilities = (
                tr('可调查：{} · 可构建：{} · 可运行测试：{}').format(capability_names.get(capability_value.get('investigate'), '未知'), capability_names.get(capability_value.get('build'), '未知'), capability_names.get(capability_value.get('test'), '未知'))
            )
            self.result_summary_var.set(
                tr('案件：{}\nBlender 仓库：{}\n当前 commit：{}\n环境状态：{}\n{}\n案件文件夹：{}').format(view.case_id, view.repo_path, view.short_commit, status_names.get(view.environment_status, view.environment_status), capabilities, view.case_dir)
            )
            self.result_hint_var.set(tr("案件已经创建，下一步请开始调查。") if view.awaiting_investigation else tr("已读取调查结果。"))
            if view.practice_session_id and self.current_reveal is None:
                self.result_hint_var.set(tr("历史练习进行中 · 真实修复和答案仍然隐藏。"))
            self._render_codex_state()
            step = flow_step(view.investigation, status=view.status)
            self._render_result_flow(step)
            self._render_next_step(view, step)
            self._render_conversation(view.case_dir)
            self._refresh_qa_state(view)
            self._render_investigation(view)
            if keep_scroll and scroll_fraction > 0:
                self.cards_scroll.canvas.yview_moveto(scroll_fraction)
            self.details_text.configure(state="normal")
            self.details_text.delete("1.0", "end")
            self.details_text.insert("1.0", json.dumps(view.environment, ensure_ascii=False, indent=2))
            self.details_text.configure(state="disabled")
            if self.details_visible:
                self._hide_details()
            self.result_page.tkraise()

        # ------------------------------------------------ 流程条 / 下一步 / 收口
        def _render_result_flow(self, step: str) -> None:
            """顶部流程条：按案件实际走到哪一步上色（已完成绿 · 当前橙 · 未到灰）。"""
            labels = (tr("Bug 描述"), tr("调查中"), tr("三条路径"), tr("实验"), tr("结论"))
            current = FLOW_STEPS.index(step) if step in FLOW_STEPS else 0
            for child in self.result_flow.winfo_children():
                child.destroy()
            for index, label in enumerate(labels):
                if index < current:
                    style, text = "StepDone.TLabel", f"✓  {label}"
                elif index == current:
                    style, text = "StepActive.TLabel", f"{index + 1}  {label}"
                else:
                    style, text = "Step.TLabel", f"{index + 1}  {label}"
                ttk.Label(self.result_flow, text=text, style=style).pack(side="left", padx=(0, 7))

        def _render_next_step(self, view: CaseView, step: str) -> None:
            """引导条：说清「你走到哪了、现在点哪」，整页只推荐一个动作。"""
            data = view.investigation
            preferred = next(
                (item for item in order_hypotheses(data.get("hypotheses", [])) if item.get("status") != "rejected"),
                None,
            )
            action = ""
            label = ""
            if step == "intake":
                text = tr("案件已经建好，还没有调查结果。下一步：让 {} 开始调查。").format(self._engine_display_name())
                action, label = "start", tr("开始调查  →")
            elif step == "investigating":
                text = tr("正在调查中，结果会自动刷新到这一页，不用重复点「开始调查」。")
            elif step == "paths":
                text = tr("先看路径 ①：{}。照它的「下一步」验证完，再回来收口。").format((preferred or {}).get("title") or tr("未命名路径"))
                action, label = "conclude", tr("确定根因  ✓")
            elif step == "experiment":
                text = tr("实验已经跑过。核对结果之后，就可以写下你的根因判断了。")
                action, label = "conclude", tr("确定根因  ✓")
            else:
                text = tr("结论已经记录。下一步：整理可复现报告包，或到「报告 Bug」页导入这份调查。")
                action, label = "package", tr("整理可复现报告包…")
            if view.practice_session_id:
                # 练习案件的收口是「提交根因判断 → 揭晓真实修复」，与真实案件不是同一套。
                action, label = "", ""
                if step not in ("intake", "investigating"):
                    text = tr("这是历史练习：先提交根因判断，再揭晓真实修复。")
            self._next_step_action = action
            self.next_step_label.configure(text=text)
            if label:
                self.next_step_button.configure(text=label, state="disabled" if self.busy else "normal")
                self.next_step_button.pack(side="right", padx=(12, 0))
            else:
                self.next_step_button.pack_forget()

        def _run_next_step(self) -> None:
            action = getattr(self, "_next_step_action", "")
            if action == "start":
                self._continue_investigation()
            elif action == "package":
                self._open_repro_report_dialog()
            elif action == "conclude":
                self._open_conclusion_dialog()

        def _open_conclusion_dialog(self) -> None:
            """收口：选一条采信的路径，写下根因判断。只记录结论，不动调查数据。"""
            if self.current_case is None:
                return
            case_id = self.current_case.case_id
            hypotheses = order_hypotheses(self.current_case.investigation.get("hypotheses", []))
            if not hypotheses:
                self.message_box.showinfo(tr("还没有调查路径"), tr("先让调查引擎给出三条路径，再来收口。"), parent=self)
                return
            existing = self.current_case.investigation.get("conclusion") or {}
            priority_names = {"high": tr("高优先级"), "medium": tr("中优先级"), "low": tr("低优先级")}
            dialog = tk.Toplevel(self)
            dialog.title(tr("确定根因"))
            self.after_idle(lambda d=dialog: self._fit_dialog(d, 640, 470))
            dialog.configure(background=self.BACKGROUND)
            dialog.transient(self)
            dialog.grab_set()
            shell = ttk.Frame(dialog, style="App.TFrame", padding=24)
            shell.pack(fill="both", expand=True)
            ttk.Label(shell, text="YOUR CONCLUSION", style="Eyebrow.TLabel").pack(anchor="w")
            ttk.Label(shell, text=tr("确定根因"), style="Title.TLabel", font=self._font("SF Pro Display", 22, "bold")).pack(anchor="w", pady=(4, 4))
            ttk.Label(shell, text=tr("选一条你采信的路径，用一句话写下判断。这一步只记录你的结论，不会改动调查结果。"), style="PageSubtitle.TLabel", wraplength=590, justify="left").pack(anchor="w", pady=(0, 12))
            ttk.Label(shell, text=tr("采信路径"), style="Muted.TLabel", font=self._font("SF Pro Text", 9, "bold")).pack(anchor="w")
            choices = tk.Listbox(
                shell, height=4, borderwidth=0, highlightthickness=1, highlightbackground=self.BORDER,
                background=self.SURFACE_ALT, foreground=self.TEXT, selectbackground="#654127",
                selectforeground=self.TEXT, activestyle="none", font=self._font("SF Pro Text", 10),
            )
            choices.pack(fill="x", pady=(4, 12))
            for item in hypotheses:
                mark = tr("（已否定）") if item.get("status") == "rejected" else ""
                choices.insert("end", f"{item.get('title') or tr('未命名路径')}  ·  {priority_names.get(item.get('priority'), '')}{mark}")
            selected = next((index for index, item in enumerate(hypotheses) if item.get("status") != "rejected"), 0)
            if isinstance(existing.get("hypothesis_id"), str):
                selected = next((index for index, item in enumerate(hypotheses) if item.get("id") == existing["hypothesis_id"]), selected)
            choices.selection_set(selected)
            ttk.Label(shell, text=tr("根因判断"), style="Muted.TLabel", font=self._font("SF Pro Text", 9, "bold")).pack(anchor="w")
            editor = tk.Text(
                shell, height=6, wrap="word", undo=True, relief="flat", borderwidth=0, highlightthickness=1,
                highlightbackground=self.BORDER, highlightcolor=self.ORANGE, background=self.SURFACE_ALT,
                foreground=self.TEXT, insertbackground=self.TEXT, selectbackground="#654127",
                padx=14, pady=12, font=self._font("SF Pro Text", 11),
            )
            editor.pack(fill="both", expand=True, pady=(4, 14))
            if existing.get("statement"):
                editor.insert("1.0", str(existing["statement"]))
            row = ttk.Frame(shell, style="App.TFrame")
            row.pack(fill="x")
            ttk.Button(row, text=tr("取消"), command=dialog.destroy, style="Ghost.TButton").pack(side="right")

            def submit() -> None:
                selection = choices.curselection()
                if not selection:
                    self.message_box.showinfo(tr("请选择路径"), tr("先在列表里选中一条你采信的路径。"), parent=dialog)
                    return
                try:
                    view = self.controller.record_conclusion(case_id, hypotheses[selection[0]].get("id"), editor.get("1.0", "end-1c"))
                except BugCompassError as exc:
                    self.message_box.showerror(tr("无法记录结论"), str(exc), parent=dialog)
                    return
                dialog.destroy()
                self._show_case(view)
                self.result_hint_var.set(tr("结论已记录到案件，流程条已经走到「结论」。"))
                self._add_timeline(tr("已记录根因判断。"))

            ttk.Button(row, text=tr("保存结论  →"), command=submit, style="Primary.TButton").pack(side="right", padx=(0, 8))
            editor.focus_set()

        def _render_conclusion(self, data: dict[str, Any]) -> None:
            conclusion = data.get("conclusion")
            if not isinstance(conclusion, dict) or not str(conclusion.get("statement") or "").strip():
                return
            chosen = next((item for item in data.get("hypotheses", []) if item.get("id") == conclusion.get("hypothesis_id")), None)
            panel = ttk.LabelFrame(self.cards_host, text=tr("结论"), style="Dark.TLabelframe", padding=18)
            panel.pack(fill="x", pady=(0, 12))
            chip_row = ttk.Frame(panel, style="Card.TFrame")
            chip_row.pack(fill="x")
            self._path_chip(chip_row, tr("已收口"), background=self.GREEN, foreground="#17120E", side="left")
            ttk.Label(chip_row, text=tr("采信路径：{}").format((chosen or {}).get("title") or tr("未命名路径")), style="Muted.TLabel").pack(side="left", padx=(10, 0))
            self._wrap_label(panel, text=str(conclusion.get("statement", "")), style="Body.TLabel", justify="left", font=self._font("SF Pro Display", 13, "bold")).pack(anchor="w", pady=(10, 6))
            ttk.Label(panel, text=tr("记录时间：{}").format(conclusion.get("recorded_at", "")), style="Muted.TLabel").pack(anchor="w")
            ttk.Button(panel, text=tr("修改结论"), command=self._open_conclusion_dialog, style="Ghost.TButton").pack(anchor="w", pady=(10, 0))

        # ------------------------------------------------------ 追问引擎（右侧面板）
        def _on_qa_return(self, _event: Any) -> str:
            self._ask_question()
            return "break"

        def _render_conversation(self, case_dir: Any) -> None:
            """把落盘的问答画进面板。引擎回答与你的提问用不同颜色，正文可以选中复制。"""
            transcript = self.qa_transcript
            transcript.configure(state="normal")
            transcript.delete("1.0", "end")
            turns = qa.load_turns(case_dir)
            if not turns:
                transcript.insert("end", tr("还没有问答。可以问「为什么路径 ① 排在前面？」这类问题。"), "meta")
            for turn in turns:
                is_user = turn["role"] == "user"
                speaker = tr("你") if is_user else (turn.get("engine") or self._engine_display_name())
                if transcript.index("end-1c") != "1.0":
                    transcript.insert("end", "\n\n")
                transcript.insert("end", f"{speaker}\n", "meta")
                transcript.insert("end", turn["content"] + "\n", "user" if is_user else "assistant")
            transcript.configure(state="disabled")
            transcript.see("end")

        def _set_qa_busy_state(self) -> None:
            send_button = getattr(self, "qa_send_button", None)
            if send_button is None:
                return
            send_button.configure(state="disabled" if (self.ask_busy or self.busy) else "normal")
            self.qa_clear_button.configure(state="disabled" if self.ask_busy else "normal")

        def _refresh_qa_state(self, view: CaseView) -> None:
            """面板状态跟着案件走：练习案件不开放追问，免得读到真实修复。"""
            self.qa_engine_var.set(tr("当前引擎：{}").format(self._engine_display_name()))
            if view.practice_session_id:
                self.qa_input.configure(state="disabled")
                if not self.ask_busy:
                    self.qa_status_var.set(tr("历史练习不开放追问：避免读到真实修复。"))
            else:
                self.qa_input.configure(state="normal")
                if not self.ask_busy:
                    self.qa_status_var.set(tr("按 Enter 发送，Shift+Enter 换行。"))
            self._set_qa_busy_state()

        def _ask_question(self) -> None:
            if self.ask_busy or self.current_case is None:
                return
            question = self.qa_input.get("1.0", "end-1c").strip()
            if not question:
                self.qa_status_var.set(tr("请先写下你的问题。"))
                return
            view = self.current_case
            if view.practice_session_id:
                self.qa_status_var.set(tr("历史练习不开放追问：避免读到真实修复。"))
                return
            if self.busy:
                self.qa_status_var.set(tr("调查还在跑，等它结束再追问。"))
                return
            try:
                qa.append_turn(view.case_dir, "user", question)
            except BugCompassError as exc:
                self.qa_status_var.set(str(exc))
                return
            self.qa_input.delete("1.0", "end")
            self._render_conversation(view.case_dir)
            turns = qa.load_turns(view.case_dir)
            runner = self._engine_runner()
            runner.reset_cancellation()
            self.ask_busy = True
            self.ask_case_id = view.case_id
            self.qa_status_var.set(tr("正在回答……（{}）").format(self._engine_display_name()))
            self._set_qa_busy_state()

            def worker() -> None:
                try:
                    answer = runner.ask(
                        view,
                        question,
                        turns,
                        progress=lambda text, case_id=view.case_id: self.events.put(("ask_progress", (case_id, text))),
                    )
                    self.events.put(("ask_done", (view, question, answer)))
                except Exception as exc:
                    message = str(exc) if isinstance(exc, (BugCompassError, OSError)) else tr("追问时发生意外错误。")
                    self.events.put(("ask_error", (view.case_id, message, question)))

            threading.Thread(target=worker, daemon=False).start()
            self.after(100, self._poll_events)

        def _append_qa_error(self, message: str) -> None:
            transcript = self.qa_transcript
            transcript.configure(state="normal")
            if transcript.index("end-1c") != "1.0":
                transcript.insert("end", "\n\n")
            transcript.insert("end", tr("追问失败：{}").format(message), "error")
            transcript.configure(state="disabled")
            transcript.see("end")

        def _clear_conversation(self) -> None:
            if self.current_case is None or self.ask_busy:
                return
            confirmed = self.message_box.askyesno(
                tr("清空对话"),
                tr("只删除问答记录（qa.json），调查结果不受影响。确定清空吗？"),
                parent=self,
            )
            if not confirmed:
                return
            try:
                qa.clear_turns(self.current_case.case_dir)
            except BugCompassError as exc:
                self.qa_status_var.set(str(exc))
                return
            self._render_conversation(self.current_case.case_dir)
            self.qa_status_var.set(tr("对话已清空，调查结果原样保留。"))

        def _render_investigation(self, view: CaseView) -> None:
            # 统一走滚动容器的 clear()：销毁旧控件 + 复位滚动，避免残影。
            self.cards_scroll.clear()
            data = view.investigation
            summary = data.get("summary", {})
            intro = ttk.LabelFrame(self.cards_host, text=tr("问题整理"), style="Dark.TLabelframe", padding=18)
            intro.pack(fill="x", pady=(0, 10))
            self._wrap_label(intro, text=summary.get("problem") or tr("案件已创建，{} 正在整理问题。").format(self._engine_display_name()), style="Body.TLabel", justify="left", font=self._font("SF Pro Display", 14, "bold")).pack(anchor="w")
            self._render_metrics_card(view)
            self._render_causal_graph(data.get("causal_graph", {"nodes": [], "edges": []}))
            self._render_semantic_diff(data.get("semantic_diff", {}))
            hypotheses = order_hypotheses(data.get("hypotheses", []))
            if hypotheses:
                self._render_paths_header(hypotheses)
                # 首选＝排序后第一条未被否定的路径：编号 ① 就是最该先做的那条。
                preferred_id = next((item.get("id") for item in hypotheses if item.get("status") != "rejected"), None)
                for index, item in enumerate(hypotheses):
                    self._render_path_card(data, item, index, preferred_id)
            else:
                self._render_empty_paths()
            self._render_conclusion(data)
            facts = [e.get("statement", "") for e in data.get("evidence", []) if e.get("kind") == "fact"]
            inferences = [e.get("statement", "") for e in data.get("evidence", []) if e.get("kind") == "inference"]
            unknowns = [u.get("question", "") for u in data.get("unknowns", [])]
            for title, values in ((tr("事实"), facts), (tr("推测"), inferences), (tr("未知"), unknowns)):
                box = ttk.LabelFrame(self.cards_host, text=title, style="Dark.TLabelframe", padding=14)
                box.pack(fill="x", pady=(0, 8))
                self._wrap_label(box, text="\n".join(f"•  {value}" for value in values) or tr("暂无"), style="Body.TLabel", justify="left").pack(anchor="w")
            if self.current_reveal is not None:
                self._render_practice_reveal(self.current_reveal)
            # 内容与换行宽度最终同步（修复窄窗口/DPI 变化下长文本被裁切）。
            self.cards_scroll.refresh()

        def _path_chip(self, parent: Any, text: str, *, background: str, foreground: str, **pack_options: Any) -> None:
            """路径卡片上的小色块：颜色即含义（优先级 / 状态）。"""
            ttk.Label(
                parent, text=text, background=background, foreground=foreground,
                font=self._font("SF Pro Text", 9, "bold"), padding=(8, 3),
            ).pack(**pack_options)

        def _render_paths_header(self, hypotheses: list[dict[str, Any]]) -> None:
            """三条路径的分区标题：先讲清楚颜色与顺序，再列卡片。"""
            header = ttk.Frame(self.cards_host, style="Surface.TFrame", padding=(18, 16))
            header.pack(fill="x", pady=(0, 12))
            ttk.Label(header, text="THREE INVESTIGATION PATHS", background=self.SURFACE, foreground=self.ORANGE, font=self._font("SF Pro Text", 9, "bold")).pack(anchor="w")
            ttk.Label(header, text=tr("三条路径"), style="Heading.TLabel").pack(anchor="w", pady=(4, 6))
            self._wrap_label(header, text=tr("由调查引擎给出的三条根因假设，按优先级从高到低排列；首选路径已标出。"), style="Muted.TLabel", justify="left").pack(anchor="w")
            legend = ttk.Frame(header, style="Surface.TFrame")
            legend.pack(fill="x", pady=(10, 0))
            for name, color in ((tr("高优先级"), self.ORANGE), (tr("中优先级"), self.YELLOW), (tr("低优先级"), self.GREEN)):
                self._path_chip(legend, name, background=color, foreground="#17120E", side="left", padx=(0, 6))
            refuted = sum(1 for item in hypotheses if item.get("status") == "rejected")
            ttk.Label(legend, text=tr("已否定 {} 条").format(refuted), style="Muted.TLabel").pack(side="left", padx=(10, 0))

        def _render_empty_paths(self) -> None:
            """结果还没到：先把三条路径的位置摆出来，结构一眼可见。"""
            empty = ttk.Frame(self.cards_host, style="Surface.TFrame", padding=24)
            empty.pack(fill="x", pady=12)
            ttk.Label(empty, text=tr("◌  正在等待调查路径"), style="Heading.TLabel").pack(anchor="w")
            ttk.Label(empty, text=tr("调查完成后，这里会出现三条有证据、可否定、可继续深入的路径。"), style="Muted.TLabel").pack(anchor="w", pady=(5, 0))
            slots = ttk.Frame(empty, style="Surface.TFrame")
            slots.pack(fill="x", pady=(16, 0))
            for column, mark in enumerate(self.PATH_MARKS[:3]):
                slots.columnconfigure(column, weight=1, uniform="path")
                slot = ttk.Frame(slots, style="Elevated.TFrame", padding=(14, 12))
                slot.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0))
                ttk.Label(slot, text=mark, background=self.SURFACE_ALT, foreground=self.SUBTLE, font=self._font("SF Pro Display", 20, "bold")).pack(anchor="w")
                ttk.Label(slot, text=tr("等待调查结果…"), background=self.SURFACE_ALT, foreground=self.SUBTLE, font=self._font("SF Pro Text", 10)).pack(anchor="w", pady=(6, 0))

        def _render_path_card(self, data: dict[str, Any], item: dict[str, Any], index: int, preferred_id: str | None) -> None:
            """单条路径：左侧色条＝优先级配色，徽章行＝优先级 / 首选 / 状态。"""
            rejected = item.get("status") == "rejected"
            accent = self.BORDER if rejected else self.PATH_ACCENTS.get(item.get("priority"), self.SUBTLE)
            mark = self.PATH_MARKS[index] if index < len(self.PATH_MARKS) else str(index + 1)
            card = ttk.LabelFrame(self.cards_host, text=tr("路径 {}").format(mark), style="Dark.TLabelframe", padding=18)
            card.pack(fill="x", pady=(0, 12))
            body = ttk.Frame(card, style="Card.TFrame")
            body.pack(fill="x")
            self._tk_module.Frame(body, background=accent, width=4, highlightthickness=0, borderwidth=0).pack(side="left", fill="y")
            content = ttk.Frame(body, style="Card.TFrame")
            content.pack(side="left", fill="x", expand=True, padx=(14, 0))

            badges = ttk.Frame(content, style="Card.TFrame")
            badges.pack(fill="x")
            priority_name = {"high": tr("高优先级"), "medium": tr("中优先级"), "low": tr("低优先级")}.get(item.get("priority"))
            if priority_name:
                self._path_chip(badges, priority_name, background=self.SURFACE_ALT if rejected else accent, foreground=self.MUTED if rejected else "#17120E", side="left")
            if not rejected and item.get("id") == preferred_id:
                ttk.Label(badges, text=tr("首选路径"), style="StepActive.TLabel").pack(side="left", padx=(8, 0))
            status_chip = {"rejected": (tr("已否定"), self.RED), "supported": (tr("已支持"), self.GREEN), "weakened": (tr("已削弱"), self.YELLOW)}.get(item.get("status"))
            if status_chip:
                self._path_chip(badges, status_chip[0], background=self.SURFACE_ALT, foreground=status_chip[1], side="right")

            title_row = ttk.Frame(content, style="Card.TFrame")
            title_row.pack(fill="x", pady=(10, 0))
            title_font = self._font("SF Pro Display", 15, "bold", "overstrike") if rejected else self._font("SF Pro Display", 15, "bold")
            ttk.Label(title_row, text=item.get("title", tr("未命名路径")), style="Heading.TLabel", font=title_font, foreground=self.MUTED if rejected else self.TEXT).pack(side="left")
            self._wrap_label(content, text=tr("假设：{}").format(item.get("claim", "")), style="Body.TLabel", justify="left", foreground=self.MUTED if rejected else self.TEXT).pack(anchor="w", pady=(8, 12))
            ttk.Label(content, text=tr("当前依据"), style="Muted.TLabel", font=self._font("SF Pro Text", 9, "bold")).pack(anchor="w")
            for basis in item.get("basis", []):
                self._wrap_label(content, text=f"•  {basis}", style="Body.TLabel", justify="left", foreground=self.MUTED if rejected else self.TEXT).pack(anchor="w", pady=1)
            for reference in item.get("source_references", []):
                line = reference.get("line")
                label = f"↗ {reference.get('path', '')}{':' + str(line) if line else ''}"
                ttk.Button(content, text=label, command=lambda ref=reference: self._open_reference(ref), style="Ghost.TButton").pack(anchor="w", pady=2)
            for value, heading, color in (
                (item.get("supporting_result"), tr("支持结果"), self.GREEN),
                (item.get("weakening_result"), tr("削弱结果"), self.RED),
            ):
                if not str(value or "").strip():
                    continue
                ttk.Label(content, text=heading, background=self.SURFACE, foreground=color, font=self._font("SF Pro Text", 9, "bold")).pack(anchor="w", pady=(10, 0))
                self._wrap_label(content, text=str(value), style="Body.TLabel", justify="left").pack(anchor="w", pady=(2, 0))
            cost_names = {"high": tr("高"), "medium": tr("中"), "low": tr("低")}
            meta: list[str] = []
            if str(item.get("risk") or "").strip():
                meta.append(tr("风险：{}").format(item["risk"]))
            if str(item.get("estimated_cost") or "").strip():
                cost = str(item["estimated_cost"])
                meta.append(tr("预计成本：{}").format(cost_names.get(cost, cost)))
            if meta:
                self._wrap_label(content, text="  ·  ".join(meta), style="Muted.TLabel", justify="left").pack(anchor="w", pady=(10, 0))
            next_box = ttk.Frame(content, style="Elevated.TFrame", padding=10)
            next_box.pack(fill="x", pady=(10, 8))
            self._wrap_label(next_box, text=tr('下一步  →  {}').format(item.get('next_step', '')), background=self.SURFACE_ALT, foreground=self.TEXT, justify="left", font=self._font("SF Pro Text", 10, "bold")).pack(anchor="w")
            buttons = ttk.Frame(content, style="Card.TFrame")
            buttons.pack(anchor="w", pady=(4, 0))
            ttk.Button(buttons, text=tr("查看证据"), command=lambda h=item: self._show_evidence(h), style="Action.TButton").pack(side="left")
            ttk.Button(buttons, text=tr("深入调查  →"), command=lambda h=item: self._run_followup("deepen", h.get("id")), style="Primary.TButton").pack(side="left", padx=8)
            ttk.Button(buttons, text=tr("否定路径"), command=lambda h=item: self._reject_path(h.get("id")), style="Ghost.TButton").pack(side="left")
            for experiment in [e for e in data.get("suggested_experiments", []) if e.get("hypothesis_id") == item.get("id")]:
                self._render_experiment_card(content, experiment)

        def _render_causal_graph(self, graph: dict[str, Any]) -> None:
            panel = ttk.LabelFrame(self.cards_host, text=tr("因果链 · AI 初稿，可编辑"), style="Dark.TLabelframe", padding=14)
            panel.pack(fill="x", pady=(0, 10))
            title_row = ttk.Frame(panel, style="Card.TFrame")
            title_row.pack(fill="x", pady=(0, 8))
            ttk.Label(title_row, text=tr("拖动卡片整理逻辑；从橙色圆点拖到另一张卡片连线；空白处拖动可平移；Ctrl+滚轮缩放。"), style="Muted.TLabel").pack(side="left")
            buttons = ttk.Frame(title_row, style="Card.TFrame")
            buttons.pack(side="right")
            ttk.Button(buttons, text=tr("＋ 添加节点"), command=self._add_causal_node, style="Ghost.TButton").pack(side="left", padx=(6, 0))
            ttk.Button(buttons, text=tr("⛶ 自动布局"), command=self._auto_layout_causal, style="Ghost.TButton").pack(side="left", padx=(6, 0))
            ttk.Button(buttons, text=tr("⊞ 适应内容"), command=self._fit_causal_view, style="Ghost.TButton").pack(side="left", padx=(6, 0))
            ttk.Button(buttons, text="1:1", command=self._reset_causal_view, style="Ghost.TButton").pack(side="left", padx=(6, 0))
            nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
            edges = graph.get("edges", []) if isinstance(graph, dict) else []
            self.causal_graph = {
                "nodes": [dict(item) for item in nodes if isinstance(item, dict)],
                "edges": [dict(item) for item in edges if isinstance(item, dict)],
            }
            if not nodes:
                self.mindmap = None
                self.causal_canvas = None
                ttk.Label(panel, text=tr("Codex 完成调查后会在这里生成“触发条件 → 状态变化 → 可见故障”的因果链。"), style="Body.TLabel", justify="left").pack(anchor="w", pady=8)
                return
            colors = {
                "surface": self.SURFACE,
                "surface_alt": self.SURFACE_ALT,
                "border": self.BORDER,
                "text": self.TEXT,
                "muted": self.MUTED,
                "subtle": self.SUBTLE,
                "orange": self.ORANGE,
                "green": self.GREEN,
                "yellow": self.YELLOW,
                "font_family": "SF Pro Text",
            }
            self.mindmap = MindMapCanvas(
                panel,
                tk,
                colors=colors,
                height=380,
                on_change=self._on_mindmap_change,
                on_status=self._on_mindmap_status,
            )
            self.mindmap.canvas.pack(fill="both", expand=True)
            self.causal_canvas = self.mindmap.canvas
            self.mindmap.set_graph(self.causal_graph)
            self.wheel_router.register_canvas(self.mindmap.canvas, self.mindmap)
            legend = ttk.Frame(panel, style="Card.TFrame")
            legend.pack(fill="x", pady=(7, 0))
            ttk.Label(legend, text=tr("实线＝事实   虚线＝推测/未知   双击节点就地改名   空白处框选多节点   Delete 删除选中   Ctrl+Z/Ctrl+Y 撤销重做   按住 Alt 拖动临时关闭网格吸附"), style="Muted.TLabel").pack(side="left")

        def _auto_layout_causal(self) -> None:
            if self.mindmap is not None:
                self.mindmap.auto_layout()

        def _fit_causal_view(self) -> None:
            if self.mindmap is not None:
                self.mindmap.fit_to_content()

        def _reset_causal_view(self) -> None:
            if self.mindmap is not None:
                self.mindmap.reset_view()

        def _on_mindmap_change(self, graph: dict[str, Any]) -> None:
            """思维导图的每次结构变化都会落盘；不整页重渲染，避免打断操作。"""
            self.causal_graph = graph
            if self.current_case is None:
                return
            try:
                self.current_case = self.controller.save_causal_graph(self.current_case.case_id, graph)
            except BugCompassError as exc:
                self.message_box.showerror(tr("无法保存因果链"), str(exc), parent=self)

        def _on_mindmap_status(self, text: str) -> None:
            self.result_hint_var.set(text)

        def _add_causal_node(self) -> None:
            if self.current_case is None or self.mindmap is None:
                return
            label = self.simple_dialog.askstring(tr("添加因果节点"), tr("用一句话描述新的因果环节："), parent=self)
            if not label or not label.strip():
                return
            self.mindmap.add_node(label)

        def _render_semantic_diff(self, semantic: dict[str, Any]) -> None:
            status = semantic.get("status", "not_available")
            names = {"not_available": tr("尚无相关代码改动"), "proposed": tr("预期语义变化"), "observed": tr("已观察到的语义变化")}
            panel = ttk.LabelFrame(self.cards_host, text=tr("语义 Diff · 行为规则变化"), style="Dark.TLabelframe", padding=16)
            panel.pack(fill="x", pady=(0, 10))
            header = ttk.Frame(panel, style="Card.TFrame")
            header.pack(fill="x")
            ttk.Label(header, text=names.get(status, status), style="Status.TLabel").pack(side="left")
            ttk.Button(header, text=tr("分析当前代码改动"), command=lambda: self._run_followup("semantic_diff", "working-tree"), style="Action.TButton").pack(side="right")
            if status == "not_available":
                self._wrap_label(panel, text=tr("当前还没有可解释的相关改动。代码发生变化后点击右侧按钮，调查引擎会只读分析 git diff。"), style="Muted.TLabel", justify="left").pack(anchor="w", pady=(8, 0))
                return
            self._wrap_label(panel, text=semantic.get("summary", ""), style="Body.TLabel", justify="left", font=self._font("SF Pro Display", 12, "bold")).pack(anchor="w", pady=(10, 8))
            rules = ttk.Frame(panel, style="Card.TFrame")
            rules.pack(fill="x")
            for title, value, column in ((tr("旧规则"), semantic.get("old_rule", ""), 0), (tr("新规则"), semantic.get("new_rule", ""), 1)):
                box = ttk.Frame(rules, style="Elevated.TFrame", padding=11)
                box.grid(row=0, column=column, sticky="nsew", padx=(0, 5) if column == 0 else (5, 0))
                rules.columnconfigure(column, weight=1)
                ttk.Label(box, text=title, background=self.SURFACE_ALT, foreground=self.ORANGE, font=self._font("SF Pro Text", 9, "bold")).pack(anchor="w")
                self._wrap_label(box, text=value or tr("尚未明确"), background=self.SURFACE_ALT, foreground=self.TEXT, justify="left").pack(anchor="w", pady=(4, 0))
            for title, key in ((tr("改变的不变量"), "changed_invariants"), (tr("受影响路径"), "affected_paths"), (tr("剩余风险"), "remaining_risks")):
                values = semantic.get(key, [])
                if values:
                    ttk.Label(panel, text=title, style="Muted.TLabel", font=self._font("SF Pro Text", 9, "bold")).pack(anchor="w", pady=(10, 2))
                    self._wrap_label(panel, text="\n".join(f"•  {value}" for value in values), style="Body.TLabel", justify="left").pack(anchor="w")
            references = semantic.get("source_references", [])
            if references:
                ttk.Label(panel, text=tr("源码依据"), style="Muted.TLabel", font=self._font("SF Pro Text", 9, "bold")).pack(anchor="w", pady=(10, 2))
                for reference in references:
                    line = reference.get("line")
                    label = f"↗ {reference.get('path', '')}{':' + str(line) if line else ''}"
                    ttk.Button(panel, text=label, command=lambda ref=reference: self._open_reference(ref), style="Ghost.TButton").pack(anchor="w")

        def _ask_experiment_prediction(self, experiment: dict[str, Any]) -> tuple[str, str] | None:
            dialog = tk.Toplevel(self)
            dialog.title(tr("运行前预测"))
            self.after_idle(lambda d=dialog: self._fit_dialog(d, 560, 440))
            dialog.configure(background=self.BACKGROUND)
            dialog.transient(self)
            dialog.grab_set()
            shell = ttk.Frame(dialog, style="App.TFrame", padding=24)
            shell.pack(fill="both", expand=True)
            ttk.Label(shell, text="PREDICT BEFORE RUN", style="Eyebrow.TLabel").pack(anchor="w")
            ttk.Label(shell, text=tr("先预测，再看结果"), style="Title.TLabel", font=self._font("SF Pro Display", 22, "bold")).pack(anchor="w", pady=(4, 4))
            ttk.Label(shell, text=tr("预测会被锁定，实验完成后 BugCompass 会把它和实际输出对照，避免事后解释。"), style="PageSubtitle.TLabel", wraplength=590, justify="left").pack(anchor="w", pady=(0, 14))
            choice_var = tk.StringVar()
            options = [
                experiment.get("expected_support") or tr("结果支持当前假设"),
                experiment.get("expected_weakening") or tr("结果削弱当前假设"),
                tr("会得到其他或无法判断的结果"),
            ]
            for option in options:
                tk.Radiobutton(shell, text=option, variable=choice_var, value=option, background=self.BACKGROUND, foreground=self.TEXT, activebackground=self.BACKGROUND, activeforeground=self.ORANGE, selectcolor=self.SURFACE_ALT, anchor="w", justify="left", wraplength=570, font=self._font("SF Pro Text", 10)).pack(fill="x", pady=4)
            ttk.Label(shell, text=tr("为什么这样预测？"), style="PageSubtitle.TLabel", font=self._font("SF Pro Text", 10, "bold")).pack(anchor="w", pady=(14, 6))
            rationale = tk.Text(shell, height=5, wrap="word", background=self.SURFACE_ALT, foreground=self.TEXT, insertbackground=self.TEXT, relief="flat", highlightthickness=1, highlightbackground=self.BORDER, highlightcolor=self.ORANGE, padx=12, pady=10, font=self._font("SF Pro Text", 11))
            rationale.pack(fill="both", expand=True)
            result: list[tuple[str, str]] = []
            row = ttk.Frame(shell, style="App.TFrame")
            row.pack(fill="x", pady=(14, 0))

            def save() -> None:
                choice, reason = choice_var.get().strip(), rationale.get("1.0", "end-1c").strip()
                if not choice or not reason:
                    self.message_box.showwarning(tr("预测还不完整"), tr("请选择一个结果，并用一句话说明理由。"), parent=dialog)
                    return
                result.append((choice, reason))
                dialog.destroy()

            ttk.Button(row, text=tr("取消"), command=dialog.destroy, style="Ghost.TButton").pack(side="right")
            ttk.Button(row, text=tr("锁定预测并继续  →"), command=save, style="Primary.TButton").pack(side="right", padx=(0, 8))
            dialog.wait_window()
            return result[0] if result else None

        def _open_judgment_dialog(self) -> None:
            if self.current_case is None or not self.current_case.practice_session_id:
                return
            dialog = tk.Toplevel(self)
            dialog.title(tr("提交根因判断"))
            self.after_idle(lambda d=dialog: self._fit_dialog(d, 540, 360))
            dialog.configure(background=self.BACKGROUND)
            dialog.transient(self)
            dialog.grab_set()
            shell = ttk.Frame(dialog, style="App.TFrame", padding=24)
            shell.pack(fill="both", expand=True)
            ttk.Label(shell, text="YOUR DIAGNOSIS", style="Eyebrow.TLabel").pack(anchor="w")
            ttk.Label(shell, text=tr("提交你的根因判断"), style="Title.TLabel", font=self._font("SF Pro Display", 22, "bold")).pack(anchor="w", pady=(4, 4))
            ttk.Label(shell, text=tr("写清楚你认为哪里错了、为什么，以及最重要的证据。提交后才能揭晓真实修复。"), style="PageSubtitle.TLabel", wraplength=570, justify="left").pack(anchor="w", pady=(0, 14))
            editor = tk.Text(shell, wrap="word", background=self.SURFACE_ALT, foreground=self.TEXT, insertbackground=self.TEXT, selectbackground="#654127", relief="flat", highlightthickness=1, highlightbackground=self.BORDER, highlightcolor=self.ORANGE, padx=14, pady=12, font=self._font("SF Pro Text", 11))
            editor.pack(fill="both", expand=True)
            row = ttk.Frame(shell, style="App.TFrame")
            row.pack(fill="x", pady=(14, 0))
            ttk.Button(row, text=tr("取消"), command=dialog.destroy, style="Ghost.TButton").pack(side="right")

            def submit() -> None:
                try:
                    self.practice_manager.submit_judgment(self.current_case.case_id, editor.get("1.0", "end-1c"))
                except BugCompassError as exc:
                    self.message_box.showerror(tr("无法提交"), str(exc), parent=dialog)
                    return
                dialog.destroy()
                self.reveal_button.configure(state="normal")
                self.result_hint_var.set(tr("根因判断已锁定。现在可以揭晓真实修复。"))

            ttk.Button(row, text=tr("锁定判断  →"), command=submit, style="Primary.TButton").pack(side="right", padx=(0, 8))
            editor.focus_set()

        def _reveal_practice(self) -> None:
            if self.current_case is None or not self.current_case.practice_session_id:
                return
            if not self.message_box.askyesno(tr("揭晓真实修复"), tr("揭晓后会显示真实根因、修复 commit 和评分，当前判断不能再伪装成未揭晓状态。继续吗？"), parent=self):
                return
            try:
                self.current_reveal = self.practice_manager.reveal(self.current_case.case_id)
                self._show_case(self.controller.load_case(self.current_case.case_id))
                self.result_hint_var.set(tr("真实修复已揭晓，评分已保存到本地练习会话。"))
            except BugCompassError as exc:
                self.message_box.showerror(tr("无法揭晓"), str(exc), parent=self)

        def _render_practice_reveal(self, reveal: PracticeReveal) -> None:
            panel = ttk.LabelFrame(self.cards_host, text=tr("真实修复 · 练习评分"), style="Dark.TLabelframe", padding=20)
            panel.pack(fill="x", pady=(12, 8))
            score_row = ttk.Frame(panel, style="Card.TFrame")
            score_row.pack(fill="x")
            ttk.Label(score_row, text=f"{reveal.quality_total} / {reveal.quality_max}", style="Heading.TLabel", font=self._font("SF Pro Display", 28, "bold"), foreground=self.ORANGE).pack(side="left")
            ttk.Label(score_row, text=tr('用时 {} 分 {} 秒  ·  AI 运行 {} 次').format(reveal.elapsed_seconds // 60, reveal.elapsed_seconds % 60, reveal.ai_runs), style="Muted.TLabel").pack(side="right")
            labels = {
                "true_subsystem_in_top3": tr("前三路径命中真实子系统"),
                "relevant_files_found": tr("找到真实相关文件"),
                "valid_evidence": tr("引用有效证据"),
                "revealing_experiment": tr("提出有效实验"),
                "premature_lock_in": tr("避免过早锁定"),
            }
            for key, label in labels.items():
                row = ttk.Frame(panel, style="Elevated.TFrame", padding=(10, 7))
                row.pack(fill="x", pady=3)
                ttk.Label(row, text=label, background=self.SURFACE_ALT, foreground=self.TEXT).pack(side="left")
                ttk.Label(row, text=f"{reveal.scores.get(key, 0)} / 2", background=self.SURFACE_ALT, foreground=self.ORANGE, font=self._font("SF Pro Text", 10, "bold")).pack(side="right")
            answer = reveal.answer
            ttk.Label(panel, text=tr("你的判断"), style="Muted.TLabel", font=self._font("SF Pro Text", 9, "bold")).pack(anchor="w", pady=(16, 4))
            self._wrap_label(panel, text=reveal.submission, style="Body.TLabel", justify="left").pack(anchor="w")
            ttk.Label(panel, text=tr("真实根因"), style="Muted.TLabel", font=self._font("SF Pro Text", 9, "bold")).pack(anchor="w", pady=(16, 4))
            self._wrap_label(panel, text=answer.get("root_cause", ""), style="Body.TLabel", justify="left").pack(anchor="w")
            ttk.Label(panel, text=tr('修复 commit  {}  ·  PR #{}').format(answer.get('fix_commit', '')[:12], answer.get('pull_request', '')), style="Status.TLabel").pack(anchor="w", pady=(12, 4))
            self._wrap_label(panel, text=tr("真实相关文件\n") + "\n".join(f"•  {path}" for path in answer.get("relevant_files", [])), style="Body.TLabel", justify="left").pack(anchor="w", pady=(5, 0))

        def _add_timeline(self, message: str) -> None:
            self.timeline.insert("end", message)
            self.timeline.yview_moveto(1.0)

        def _open_reference(self, reference: dict[str, Any]) -> None:
            try:
                self.controller.open_source_reference(reference, self.current_case.repo_path if self.current_case else None)
            except BugCompassError as exc:
                self.message_box.showerror(tr("无法打开源码"), str(exc), parent=self)

        def _show_evidence(self, hypothesis: dict[str, Any]) -> None:
            if self.current_case is None:
                return
            wanted = set(hypothesis.get("evidence_ids", []))
            entries = [e for e in self.current_case.investigation.get("evidence", []) if e.get("id") in wanted]
            text = "\n\n".join(f"[{(tr('事实') if e.get('kind') == 'fact' else tr('推测'))}] {e.get('statement', '')}" for e in entries) or tr("这条路径还没有关联证据。")
            self.message_box.showinfo(tr("路径证据"), text, parent=self)

        def _reject_path(self, hypothesis_id: str | None) -> None:
            if not self.current_case or not hypothesis_id:
                return
            if not self.message_box.askyesno(tr("否定调查路径"), tr("确认将这条路径标记为已否定？后续 Codex 更新会保留这个决定。"), parent=self):
                return
            try:
                self._show_case(self.controller.reject_path(self.current_case.case_id, hypothesis_id))
            except BugCompassError as exc:
                self.message_box.showerror(tr("无法更新路径"), str(exc), parent=self)

        def _render_experiment_card(self, parent: Any, experiment: dict[str, Any]) -> None:
            permission_names = {"green": tr("绿色 · 只读"), "yellow": tr("黄色 · 构建/测试/运行"), "red": tr("红色 · 会修改数据")}
            colors = {"green": "#25813B", "yellow": "#A46000", "red": "#B42318"}
            permission = experiment.get("permission", "red")
            frame = ttk.LabelFrame(parent, text=tr('实验 {}').format(experiment.get('id', '')), style="Dark.TLabelframe", padding=14)
            frame.pack(fill="x", pady=(12, 0))
            ttk.Label(frame, text=experiment.get("title", tr("未命名实验")), style="Heading.TLabel", font=self._font("SF Pro Display", 12, "bold")).pack(anchor="w")
            self._wrap_label(frame, text=tr('目的  {}').format(experiment.get('purpose') or experiment.get('description', '')), style="Body.TLabel", justify="left").pack(anchor="w", pady=(5, 8))
            command = experiment.get("command", [])
            command_box = ttk.Frame(frame, style="Elevated.TFrame", padding=10)
            command_box.pack(fill="x")
            self._wrap_label(command_box, text=f"$ {' '.join(command)}", background=self.SURFACE_ALT, foreground="#D6DAE2", font=self._font("SF Mono", 9), justify="left").pack(anchor="w")
            self._wrap_label(frame, text=tr('{}\n工作目录  {}  ·  预计 {} 秒').format(experiment.get('description', ''), experiment.get('cwd', '.'), experiment.get('estimated_seconds', '?')), style="Muted.TLabel", justify="left").pack(anchor="w", pady=8)
            ttk.Label(frame, text=f"●  {permission_names.get(permission, permission)}", background=self.SURFACE, foreground=colors.get(permission, colors["red"]), font=self._font("SF Pro Text", 10, "bold")).pack(anchor="w")
            if experiment.get("result"):
                self._wrap_label(frame, text=tr('上次结果  {}\n影响  {}').format(experiment.get('result'), experiment.get('effect', 'pending')), style="Body.TLabel", justify="left").pack(anchor="w", pady=(8, 0))
            prediction = experiment.get("prediction", {})
            if prediction.get("predicted_at"):
                prediction_box = ttk.Frame(frame, style="Elevated.TFrame", padding=10)
                prediction_box.pack(fill="x", pady=(8, 0))
                self._wrap_label(prediction_box, text=tr('你的运行前预测  {}').format(prediction.get('choice', '')), background=self.SURFACE_ALT, foreground=self.ORANGE, font=self._font("SF Pro Text", 10, "bold"), justify="left").pack(anchor="w")
                self._wrap_label(prediction_box, text=prediction.get("rationale", ""), background=self.SURFACE_ALT, foreground=self.MUTED, justify="left").pack(anchor="w", pady=(3, 0))
                if experiment.get("prediction_comparison"):
                    self._wrap_label(prediction_box, text=tr('结果对照  {}').format(experiment.get('prediction_comparison')), background=self.SURFACE_ALT, foreground=self.TEXT, justify="left").pack(anchor="w", pady=(5, 0))
            ttk.Button(frame, text=tr("运行实验  ▶"), command=lambda e=experiment: self._approve_and_run_experiment(e), style="Action.TButton").pack(anchor="w", pady=(10, 0))

        def _approve_and_run_experiment(self, experiment: dict[str, Any]) -> None:
            if self.busy or self.current_case is None:
                return
            try:
                plan = self.controller.prepare_experiment(experiment, self.current_case.repo_path)
            except BugCompassError as exc:
                self.message_box.showerror(tr("实验不可执行"), str(exc), parent=self)
                return
            prediction = experiment.get("prediction", {})
            if not prediction.get("predicted_at"):
                captured = self._ask_experiment_prediction(experiment)
                if captured is None:
                    return
                choice, rationale = captured
                try:
                    self.current_case = self.controller.record_prediction(
                        self.current_case.case_id, plan.experiment_id, choice, rationale
                    )
                except BugCompassError as exc:
                    self.message_box.showerror(tr("无法保存预测"), str(exc), parent=self)
                    return
                self.result_hint_var.set(tr("预测已锁定。实验结束后会自动对照实际结果。"))
            detail = tr('将执行：\n{}\n\n工作目录：\n{}\n\n预计耗时：{} 秒').format(plan.command_text, plan.cwd, plan.estimated_seconds)
            if plan.permission == "yellow":
                approved = self.message_box.askyesno(tr("确认黄色实验"), detail + tr("\n\n该实验会构建、测试或启动 Blender。确认执行吗？"), parent=self)
                if not approved:
                    return
            elif plan.permission == "red":
                answer = self.simple_dialog.askstring(tr("明确授权红色实验"), detail + tr("\n\n该操作可能修改源码、删除文件或影响 Git。请输入“明确授权”继续："), parent=self)
                if answer != tr("明确授权"):
                    self.result_hint_var.set(tr("红色实验未获得明确授权，未执行。"))
                    return
            view = self.current_case
            self.busy = True
            self.codex_active = False
            self.active_case_id = None
            self._set_codex_state("idle", tr("正在运行本地实验，Codex 当前没有工作。"))
            self.result_hint_var.set(tr('正在运行实验 {}……').format(plan.experiment_id))
            self._add_timeline(tr('实验 {} 已获用户批准，正在执行。').format(plan.experiment_id))
            def worker() -> None:
                try:
                    record = self.controller.run_experiment(view.case_id, plan)
                    self.events.put(("experiment_complete", (view, plan.experiment_id, record)))
                except Exception as exc:
                    message = str(exc) if isinstance(exc, (BugCompassError, OSError)) else tr("实验执行时发生意外错误。")
                    self.events.put(("error", (view.case_id, message)))
            threading.Thread(target=worker, daemon=False).start()
            self.after(100, self._poll_events)

        def _run_followup(self, action: str, target_id: str | None, *, case_id: str | None = None) -> None:
            if self.busy or not target_id or (case_id is None and self.current_case is None):
                return
            selected_case_id = case_id or self.current_case.case_id
            try:
                view = self.controller.load_case(selected_case_id)
                self.controller.set_case_status(view.case_id, "investigating")
            except BugCompassError as exc:
                self.message_box.showerror(tr("无法开始调查"), str(exc), parent=self)
                return
            self.busy = True
            self.codex_active = True
            self.active_case_id = view.case_id
            runner = self._engine_runner()
            self._active_runner = runner
            runner.reset_cancellation()
            self._refresh_recent_cases()
            self._set_codex_state("working", tr("正在读取新增证据并更新同一个案件。"))
            if self.current_case is not None and self.current_case.case_id == view.case_id:
                self.result_hint_var.set(tr("正在更新同一个案件……"))
                self._add_timeline(tr("深入调查已有案件，不会创建新答案。"))
            def worker() -> None:
                try:
                    if view.practice_session_id:
                        self.practice_manager.increment_ai_runs(view.case_id)
                    result = runner.run(
                        view,
                        progress=lambda text, active_id=view.case_id: self.events.put(("codex_progress", (active_id, text))),
                        action=action,
                        target_id=target_id,
                    )
                    self._finish_codex_run(view, result)
                except CodexRunBusyError as exc:
                    self.events.put(("error", (view.case_id, str(exc))))
                except Exception as exc:
                    try:
                        self.controller.set_case_status(view.case_id, "failed")
                    except BugCompassError:
                        pass
                    message = str(exc) if isinstance(exc, (BugCompassError, OSError)) else tr("继续调查时发生意外错误。")
                    self.events.put(("error", (view.case_id, message)))
            threading.Thread(target=worker, daemon=False).start()
            self.after(100, self._poll_events)

        # ---------------------------------------------------------- 缩放与字体
        def _font(self, family: str, size: int, *extra: str) -> tuple[str | Any, ...]:
            """按当前界面缩放生成字体元组（含 DPI 感知的最低可读字号）。"""
            factor = self.ui_scale.factor if hasattr(self, "ui_scale") else 1.0
            scaled = max(6, int(round(size * factor)))
            return (family, scaled, *extra)

        # ------------------------------------------------------ 调查引擎选择
        def _engine_options(self) -> list[str]:
            options = [tr("Codex CLI（默认）")]
            options.extend(provider.display_name for provider in self.llm_providers)
            return options

        def _active_provider(self) -> LLMProviderConfig | None:
            engine = str(self.settings.get("active_engine", "codex") or "codex")
            if engine == "codex":
                return None
            for provider in self.llm_providers:
                if provider.id == engine:
                    return provider
            return None

        def _engine_display_name(self) -> str:
            provider = self._active_provider()
            return tr("Codex CLI（默认）") if provider is None else provider.display_name

        def _engine_label(self) -> str:
            provider = self._active_provider()
            return "Codex" if provider is None else provider.label

        def _engine_runner(self) -> Any:
            """当前引擎对应的运行器（run/cancel/reset_cancellation 接口一致）。"""
            provider = self._active_provider()
            if provider is None:
                return self.codex_runner
            investigator = self._llm_investigators.get(provider.id)
            if investigator is None or investigator.provider != provider:
                investigator = LLMInvestigator(self._data_root_path, provider)
                self._llm_investigators[provider.id] = investigator
            return investigator

        def _import_key_for_current_engine(self) -> None:
            provider = self._active_provider()
            if provider is not None:
                self._import_api_key_dialog(provider)

        def _refresh_engine_key_button(self) -> None:
            """当前引擎缺密钥时，在引擎下拉旁显示内联「导入密钥…」。"""
            button = getattr(self, "_engine_key_button", None)
            if button is None:
                return
            provider = self._active_provider()
            if provider is not None and provider.needs_key:
                try:
                    resolve_api_key(provider)
                except LLMError:
                    button.pack(side="left", padx=(10, 0))
                    return
            button.pack_forget()

        def _on_engine_changed(self, _event: Any = None) -> None:
            selection = self.engine_var.get()
            provider = next(
                (item for item in self.llm_providers if item.display_name == selection),
                None,
            )
            self.settings["active_engine"] = provider.id if provider else "codex"
            save_settings(self.settings)
            name = provider.display_name if provider else "Codex CLI"
            self.progress_var.set(tr('调查引擎已切换为 {}（已保存）。').format(name))
            self._refresh_engine_key_button()
            if provider is not None and provider.needs_key:
                try:
                    resolve_api_key(provider)
                except LLMError:
                    if self.message_box.askyesno(
                        tr("需要 API 密钥"),
                        tr('{} 还没有密钥，现在导入吗？\n（也可以稍后在 ⚙ 设置 → 模型服务里导入）').format(provider.display_name),
                        parent=self,
                    ):
                        self._import_api_key_dialog(provider)

        def _poll_async(self) -> None:
            """把工作线程排进来的 UI 更新放到主线程执行。"""
            while True:
                try:
                    callback = self._main_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    callback()
                except Exception:  # pragma: no cover - 单个回调失败不中断轮询
                    pass
            self.after(200, self._poll_async)

        def _apply_saved_ui_scale(self) -> None:
            saved = int(self.settings.get("ui_scale_percent", 100) or 100)
            self.ui_scale.set_percent(saved)
            # 系统真实 DPI（96=100%）作为基线，用户缩放在其上叠加。
            apply_scaling(self, extra=max(0.5, self.ui_scale.factor))

        def _on_cards_zoom(self, step: int) -> None:
            """Ctrl+滚轮（结果页卡片区域）：整体界面缩放。"""
            self._zoom_ui(step)

        def _zoom_ui(self, step: int) -> None:
            previous = self.ui_scale.percent
            percent = self.ui_scale.step(step)
            if percent == previous:
                return
            self.settings["ui_scale_percent"] = percent
            save_settings(self.settings)
            apply_scaling(self, extra=max(0.5, self.ui_scale.factor))
            self._configure_styles(self._ttk_module)
            self._refresh_zoomable_content()
            self.result_hint_var.set(tr('界面缩放 {}%（已保存，重启后仍生效）').format(percent))

        def _refresh_zoomable_content(self) -> None:
            # 结果页会整体重建；其他页面只更新样式字体，输入内容不受影响。
            if self.current_case is not None:
                try:
                    self._show_case(self.controller.load_case(self.current_case.case_id))
                    return
                except BugCompassError:
                    pass

        def _wrap_label(self, parent: Any, **kwargs: Any) -> Any:
            """创建卡片里的自适应换行标签（窗口变窄/DPI 变化时不会被裁切）。"""
            label = ttk.Label(parent, **kwargs)
            self.cards_scroll.wrap_here(label)
            return label

        # -------------------------------------------------------------- 指标
        def _render_metrics_card(self, view: CaseView) -> None:
            panel = ttk.LabelFrame(self.cards_host, text=tr("运行指标 · 本地保存"), style="Dark.TLabelframe", padding=14)
            panel.pack(fill="x", pady=(0, 10))
            try:
                pricing = load_pricing(view.workspace_path)
                metrics = collect_case_metrics(view.case_dir, pricing=pricing)
                write_case_metrics(view.case_dir, metrics)
            except Exception:
                pricing, metrics = {}, None
            header = ttk.Frame(panel, style="Card.TFrame")
            header.pack(fill="x")
            if metrics is None or not metrics.runs:
                ttk.Label(header, text=tr("还没有 Codex 运行记录；运行后这里会显示模型、耗时、工具轮次与成本。"), style="Muted.TLabel").pack(anchor="w")
                return
            totals = metrics.to_dict()["totals"]
            models = "、".join(metrics.models) or tr("未知")
            runs = len(metrics.runs)
            self._wrap_label(header, text=tr('模型  {}   ·   运行 {} 次').format(models, runs), style="Heading.TLabel").pack(anchor="w")
            grid = ttk.Frame(panel, style="Card.TFrame")
            grid.pack(fill="x", pady=(8, 0))
            columns = (
                (tr("累计耗时"), format_duration(totals["duration_seconds"])),
                (tr("对话轮次"), str(totals["turns"])),
                (tr("工具调用"), str(totals["tool_calls"])),
                (tr("输入 token"), f"{totals['input_tokens']:,}tr(（缓存 ){totals['cached_input_tokens']:,}）"),
                (tr("输出 token"), f"{totals['output_tokens']:,}"),
                (tr("估计成本"), format_cost(totals["estimated_cost_usd"])),
            )
            for index, (name, value) in enumerate(columns):
                box = ttk.Frame(grid, style="Elevated.TFrame", padding=(10, 7))
                box.grid(row=index // 3, column=index % 3, sticky="ew", padx=4, pady=3)
                grid.columnconfigure(index % 3, weight=1)
                ttk.Label(box, text=name, background=self.SURFACE_ALT, foreground=self.MUTED, font=self._font("SF Pro Text", 9, "bold")).pack(anchor="w")
                ttk.Label(box, text=value, background=self.SURFACE_ALT, foreground=self.TEXT, font=self._font("SF Pro Text", 11, "bold")).pack(anchor="w", pady=(2, 0))
            telemetry_state = tr("已开启（仅计数，且需再手动导出才会离开本机）") if telemetry_enabled() else tr("未开启")
            self._wrap_label(
                panel,
                text=tr('成本按 ~/.bugcompass/pricing.json 里你填写的单价估算；未配置的模型显示“未配置单价”，不会凭空估算。统计上报：{}。').format(telemetry_state),
                style="Muted.TLabel",
                justify="left",
            ).pack(anchor="w", pady=(8, 0))

        def _record_run_metrics(self, view: CaseView) -> None:
            """运行结束后把聚合指标写进本地 metrics.json；上报默认关闭。"""
            try:
                pricing = load_pricing(view.workspace_path)
                metrics = collect_case_metrics(view.case_dir, pricing=pricing)
                write_case_metrics(view.case_dir, metrics)
            except Exception:
                return
            if not telemetry_enabled() or not metrics.runs:
                return
            latest = metrics.runs[-1]
            record_run_metrics(
                case_id=latest.case_id or view.case_id,
                model=latest.model,
                duration_seconds=latest.duration_seconds,
                turns=latest.turns,
                tool_calls=latest.tool_calls,
                input_tokens=latest.input_tokens,
                output_tokens=latest.output_tokens,
                estimated_cost_usd=latest.estimated_cost_usd,
                status=latest.status,
            )

        # ------------------------------------------------------ 可复现报告包
        def _open_repro_report_dialog(self) -> None:
            if self.current_case is None:
                return
            if self.current_case.practice_session_id is not None:
                self.message_box.showinfo(tr("历史练习"), tr("历史练习案例不用于向 Blender 提交新报告。"), parent=self)
                return
            case_id = self.current_case.case_id
            try:
                draft = self.controller.load_repro_report_draft(case_id)
            except BugCompassError as exc:
                self.message_box.showerror(tr("报告草稿无法打开"), str(exc), parent=self)
                return

            dialog = tk.Toplevel(self)
            dialog.title(tr('可复现报告包 · {}').format(case_id))
            self.after_idle(lambda d=dialog: self._fit_dialog(d, 650, 620))
            dialog.configure(background=self.BACKGROUND)
            dialog.transient(self)
            dialog.grab_set()
            shell = ttk.Frame(dialog, style="App.TFrame", padding=18)
            shell.pack(fill="both", expand=True)
            ttk.Label(shell, text=tr("可复现报告包"), style="Title.TLabel", font=self._font("SF Pro Display", 21, "bold")).pack(anchor="w")
            ttk.Label(shell, text=tr("请核对预填信息。报告草稿保存在 Case 中；只有手动选择的附件会进入 ZIP。"), style="PageSubtitle.TLabel", wraplength=680).pack(anchor="w", pady=(3, 12))

            notebook = ttk.Notebook(shell)
            notebook.pack(fill="both", expand=True)
            content = ttk.Frame(notebook, style="Surface.TFrame", padding=14)
            review_page = ttk.Frame(notebook, style="Surface.TFrame", padding=14)
            notebook.add(content, text=tr("报告内容"))
            notebook.add(review_page, text=tr("附件与检查"))
            content.columnconfigure(0, weight=1)

            entries: dict[str, Any] = {}
            text_boxes: dict[str, Any] = {}
            for row, (field, label) in enumerate((
                ("title", tr("简明标题")),
                ("broken_version", tr("出现问题的 Blender 版本")),
                ("working_version", tr("最后正常版本（未知可留空）")),
                ("system_info", tr("系统、显卡和驱动摘要（或在附件中加入 system-info.txt）")),
            )):
                ttk.Label(content, text=label, style="Heading.TLabel", font=self._font("SF Pro Text", 10, "bold")).grid(row=row * 2, column=0, sticky="w", pady=(6, 2))
                variable = tk.StringVar(value=draft[field])
                ttk.Entry(content, textvariable=variable, style="Dark.TEntry").grid(row=row * 2 + 1, column=0, sticky="ew")
                entries[field] = variable
            for index, (field, label, height) in enumerate((
                ("steps", tr("逐步复现操作"), 5),
                ("expected", tr("预期行为"), 3),
                ("actual", tr("实际行为"), 3),
            ), start=4):
                ttk.Label(content, text=label, style="Heading.TLabel", font=self._font("SF Pro Text", 10, "bold")).grid(row=index * 2, column=0, sticky="w", pady=(7, 2))
                editor = tk.Text(content, height=height, wrap="word", background=self.SURFACE_ALT, foreground=self.TEXT, insertbackground=self.TEXT, relief="flat", padx=8, pady=6)
                editor.insert("1.0", draft[field])
                editor.grid(row=index * 2 + 1, column=0, sticky="nsew")
                text_boxes[field] = editor
            content.rowconfigure(9, weight=1)

            flags: dict[str, Any] = {}
            for field, label in (
                ("reproduced", tr("已按上述步骤在出错版本再次复现")),
                ("factory_startup", tr("无需 .blend，可从默认场景复现")),
                ("tested_latest", tr("已在最新稳定版或开发版复测")),
                ("duplicate_checked", tr("已搜索开放与已关闭报告，检查重复")),
                ("simplified_file", tr("所选 .blend 已删除无关内容")),
                ("crash", tr("这是崩溃问题")),
            ):
                variable = tk.BooleanVar(value=draft[field])
                tk.Checkbutton(review_page, text=label, variable=variable, background=self.SURFACE, foreground=self.TEXT, activebackground=self.SURFACE, activeforeground=self.TEXT, selectcolor=self.SURFACE_ALT, anchor="w").pack(anchor="w")
                flags[field] = variable

            ttk.Label(review_page, text=tr("最近复测的 Blender 版本（如已复测）"), style="Heading.TLabel", font=self._font("SF Pro Text", 10, "bold")).pack(anchor="w", pady=(8, 3))
            latest_version_var = tk.StringVar(value=draft["latest_tested_version"])
            ttk.Entry(review_page, textvariable=latest_version_var, style="Dark.TEntry").pack(fill="x")
            entries["latest_tested_version"] = latest_version_var

            steps_editor = text_boxes["steps"]
            steps_editor.edit_modified(False)

            def invalidate_reproduction(_event: Any) -> None:
                if steps_editor.edit_modified():
                    flags["reproduced"].set(False)
                    steps_editor.edit_modified(False)

            steps_editor.bind("<<Modified>>", invalidate_reproduction)

            ttk.Label(review_page, text=tr("公开附件（不会自动加入原始报告、源码或 Case 文件）"), style="Heading.TLabel", font=self._font("SF Pro Text", 11, "bold")).pack(anchor="w", pady=(12, 5))
            attachment_paths = list(draft["attachments"])
            attachments = tk.Listbox(review_page, height=6, background=self.SURFACE_ALT, foreground=self.TEXT, selectbackground=self.BORDER, borderwidth=0)
            attachments.pack(fill="x")

            def refresh_attachments() -> None:
                attachments.delete(0, "end")
                for path in attachment_paths:
                    attachments.insert("end", path)

            refresh_attachments()

            def add_attachments() -> None:
                selected = self.file_dialog.askopenfilenames(title=tr("选择准备公开的 .blend、system-info.txt、截图或日志"), parent=dialog)
                for path in selected:
                    if path not in attachment_paths:
                        attachment_paths.append(path)
                refresh_attachments()

            def remove_attachment() -> None:
                for index in reversed(attachments.curselection()):
                    attachment_paths.pop(index)
                refresh_attachments()

            attachment_actions = ttk.Frame(review_page, style="Surface.TFrame")
            attachment_actions.pack(anchor="w", pady=(6, 10))
            ttk.Button(attachment_actions, text=tr("添加附件…"), command=add_attachments, style="Action.TButton").pack(side="left")
            ttk.Button(attachment_actions, text=tr("移除所选"), command=remove_attachment, style="Ghost.TButton").pack(side="left", padx=(8, 0))
            review_var = tk.StringVar()
            ttk.Label(review_page, textvariable=review_var, style="Muted.TLabel", wraplength=650, justify="left").pack(anchor="w")

            def current_draft() -> dict[str, Any]:
                data = dict(draft)
                data.update({name: variable.get() for name, variable in entries.items()})
                data.update({name: editor.get("1.0", "end-1c") for name, editor in text_boxes.items()})
                data.update({name: bool(variable.get()) for name, variable in flags.items()})
                data["attachments"] = list(attachment_paths)
                return data

            def show_review() -> None:
                result = review_draft(current_draft())
                if result.missing_required:
                    status = tr("尚缺材料：") + "、".join(result.missing_required) + tr("。\n可导出草稿，但提交前请补齐。")
                else:
                    status = tr("必填材料已填写。请继续人工核对版本、复现、附件内容和报告真实性。")
                review_var.set(status + tr("\n建议核对：") + "；".join(result.suggestions))

            def save() -> bool:
                try:
                    self.controller.save_repro_report_draft(case_id, current_draft())
                except BugCompassError as exc:
                    self.message_box.showerror(tr("保存失败"), str(exc), parent=dialog)
                    return False
                show_review()
                self.result_hint_var.set(tr("可复现报告草稿已保存到当前 Case。"))
                return True

            def export() -> None:
                if not save():
                    return
                target = self.file_dialog.asksaveasfilename(
                    title=tr("保存可复现报告包"),
                    defaultextension=".zip",
                    initialfile=tr('{}-可复现报告包.zip').format(case_id),
                    filetypes=[(tr("ZIP 压缩包"), "*.zip")],
                    parent=dialog,
                )
                if not target:
                    return
                warning = "\n".join(f"- {path}" for path in attachment_paths) or tr("（没有附件）")
                if not self.message_box.askyesno(
                    tr("核对公开内容"),
                    tr("报告正文和以下附件将写入本地 ZIP：\n") + warning +
                    tr("\n\n请确认附件不含私人或项目敏感内容；程序不会自动脱敏或上传。继续导出？"),
                    parent=dialog,
                ):
                    return
                try:
                    result = self.controller.export_repro_report_package(case_id, target)
                except BugCompassError as exc:
                    self.message_box.showerror(tr("导出失败"), str(exc), parent=dialog)
                    return
                self.message_box.showinfo(
                    tr("报告包已导出"),
                    tr('已保存到：\n{}\n\n请先阅读 ZIP 内的发布前检查清单，再提交报告与所需附件。').format(result.path),
                    parent=dialog,
                )
                self.result_hint_var.set(tr("可复现报告包已导出；提交前请核对检查清单。"))

            show_review()
            actions = ttk.Frame(shell, style="App.TFrame")
            actions.pack(fill="x", pady=(12, 0))
            ttk.Button(actions, text=tr("关闭"), command=dialog.destroy, style="Ghost.TButton").pack(side="right")
            ttk.Button(actions, text=tr("导出 ZIP…"), command=export, style="Primary.TButton").pack(side="right", padx=(0, 8))
            ttk.Button(actions, text=tr("保存草稿"), command=save, style="Action.TButton").pack(side="right", padx=(0, 8))

        # -------------------------------------------------------------- 备份
        def _backup_workspace(self) -> None:
            if self.current_case is None:
                self.message_box.showinfo(tr("备份"), tr("请先打开一个案件（备份会打包整个工作区）。"), parent=self)
                return
            try:
                target = create_backup(self.current_case.workspace_path, note=tr('GUI 导出 · v{}').format(__version__))
            except (BackupError, OSError) as exc:
                self.message_box.showerror(tr("备份失败"), str(exc), parent=self)
                return
            if self.message_box.askyesno(tr("备份完成"), tr('备份已保存：\n{}\n\n是否打开所在文件夹？').format(target), parent=self):
                self._open_path_in_explorer(target)
            self.result_hint_var.set(tr("工作区备份完成。"))

        def _restore_backup_dialog(self) -> None:
            archive = self.file_dialog.askopenfilename(
                title=tr("选择 BugCompass 备份文件"),
                filetypes=[(tr("BugCompass 备份"), "*.zip"), (tr("所有文件"), "*.*")],
                parent=self,
            )
            if not archive:
                return
            try:
                manifest = inspect_backup(archive)
            except (BackupError, OSError) as exc:
                self.message_box.showerror(tr("备份无效"), str(exc), parent=self)
                return
            count = manifest.get("file_count", "?")
            created = str(manifest.get("created_at", tr("未知")))[:19].replace("T", " ")
            version = manifest.get("app_version", tr("未知"))
            target_dir = self.file_dialog.askdirectory(title=tr('恢复到哪个文件夹？（备份：{} 个文件 · v{} · {}）').format(count, version, created), parent=self)
            if not target_dir:
                return
            try:
                destination = restore_backup(archive, target_dir)
            except (BackupError, OSError) as exc:
                self.message_box.showerror(tr("恢复失败"), str(exc), parent=self)
                return
            migrations = ""
            self.message_box.showinfo(
                tr("恢复完成"),
                tr('工作区已恢复到：\n{}\n\n迁移记录见工作区内的 backup-manifest.json。{}').format(destination, migrations),
                parent=self,
            )

        def _export_diagnostics(self) -> None:
            include_case = self.message_box.askyesno(
                tr("诊断包内容"),
                tr("诊断包包含：崩溃日志、环境摘要、设置（已脱敏）。\n\n是否额外包含当前案件的结构信息（只有计数与状态，不含问题描述、证据正文或源码）？"),
                parent=self,
            )
            dest = self.file_dialog.askdirectory(title=tr("诊断包保存到哪个文件夹？"), parent=self)
            if not dest:
                return
            try:
                target = export_bundle(
                    dest,
                    root=self,
                    case_dir=self.current_case.case_dir if self.current_case else None,
                    include_case_structure=include_case,
                )
            except Exception as exc:
                self.message_box.showerror(tr("导出失败"), tr('无法生成诊断包：{}').format(exc), parent=self)
                return
            if self.message_box.askyesno(tr("导出完成"), tr('诊断包已保存（已自动脱敏并通过自检）：\n{}\n\n是否打开所在文件夹？').format(target), parent=self):
                self._open_path_in_explorer(target)

        def _open_path_in_explorer(self, path: Any) -> None:
            import subprocess as _sp
            import sys as _sys
            try:
                if _sys.platform == "win32":
                    _sp.Popen(["explorer", "/select,", str(path)])
                elif _sys.platform == "darwin":
                    _sp.Popen(["open", "-R", str(path)])
                else:
                    _sp.Popen(["xdg-open", str(Path(path).parent)])
            except OSError:
                pass

        # ------------------------------------------------- AI 挑选 Issue
        def _open_issue_scout_dialog(self) -> None:
            """AI 筛选 Blender tracker 上的 issue（抓取 + 打分 + 一键建案），全程图形界面。"""
            from .issue_scout import FALLBACK_MODULES

            dialog = tk.Toplevel(self)
            dialog.title(tr("AI 挑选 Issue — Blender tracker 筛选"))
            self.after_idle(lambda d=dialog: self._fit_dialog(d, 880, 600))
            dialog.configure(background=self.BACKGROUND)
            dialog.transient(self)
            self._scout_dialog = dialog

            shell = ttk.Frame(dialog, style="App.TFrame", padding=(22, 18))
            shell.pack(fill="both", expand=True)
            header = ttk.Frame(shell, style="App.TFrame")
            header.pack(fill="x", pady=(0, 10))
            brand = ttk.Frame(header, style="App.TFrame")
            brand.pack(side="left")
            ttk.Label(brand, text="ISSUE SCOUT", style="Eyebrow.TLabel").pack(anchor="w")
            ttk.Label(brand, text=tr("AI 挑选 Issue"), style="Title.TLabel", font=self._font("SF Pro Display", 20, "bold")).pack(anchor="w", pady=(2, 0))
            self._scout_status_var = tk.StringVar(value=tr("选择条件后点击「开始筛选」。筛选需要联网抓取 tracker，并用你配置的大模型评分。"))
            ttk.Label(header, textvariable=self._scout_status_var, style="PageStatus.TLabel", wraplength=380, justify="left").pack(side="right", anchor="ne")

            # ---- 选项区
            options = ttk.LabelFrame(shell, text=tr("筛选条件"), style="Dark.TLabelframe", padding=14)
            options.pack(fill="x", pady=(0, 10))
            ttk.Label(options, text=tr("模块（可多选；不选＝全部）"), style="Muted.TLabel", font=self._font("SF Pro Text", 9, "bold")).pack(anchor="w")
            modules_box = ttk.Frame(options, style="Card.TFrame")
            modules_box.pack(fill="x", pady=(6, 10))
            module_vars: dict[str, tk.BooleanVar] = {}
            for index, module in enumerate(FALLBACK_MODULES):
                column, row = index % 5, index // 5
                var = tk.BooleanVar(value=False)
                module_vars[module] = var
                tk.Checkbutton(
                    modules_box, text=module.removeprefix("Module/"), variable=var,
                    background=self.SURFACE, foreground=self.TEXT, activebackground=self.SURFACE,
                    activeforeground=self.TEXT, selectcolor=self.SURFACE_ALT,
                    highlightthickness=0, bd=0, font=("SF Pro Text", 9), anchor="w",
                ).grid(row=row, column=column, sticky="w", padx=(0, 14), pady=1)
            row2 = ttk.Frame(options, style="Card.TFrame")
            row2.pack(fill="x")
            ttk.Label(row2, text=tr("类型"), style="Muted.TLabel", font=self._font("SF Pro Text", 9, "bold")).pack(side="left", padx=(0, 8))
            type_var = tk.StringVar(value=tr("全部"))
            for value, label in ((tr("全部"), tr("全部")), ("Type/Bug", "Bug"), ("Type/Report", tr("功能需求")), ("Type/Known Issue", tr("已知问题"))):
                tk.Radiobutton(
                    row2, text=label, variable=type_var, value=value,
                    background=self.SURFACE, foreground=self.TEXT, activebackground=self.SURFACE,
                    activeforeground=self.TEXT, selectcolor=self.SURFACE_ALT, highlightthickness=0, bd=0,
                    font=("SF Pro Text", 10),
                ).pack(side="left", padx=(0, 14))
            ttk.Label(row2, text=tr("数量"), style="Muted.TLabel", font=self._font("SF Pro Text", 9, "bold")).pack(side="left", padx=(16, 6))
            count_spin = tk.Spinbox(
                row2, from_=10, to=100, increment=10, width=5,
                background=self.SURFACE_ALT, foreground=self.TEXT, buttonbackground=self.SURFACE_ALT,
                relief="flat", highlightthickness=1, highlightbackground=self.BORDER, readonlybackground=self.SURFACE_ALT,
            )
            count_spin.delete(0, "end")
            count_spin.insert(0, "40")
            count_spin.pack(side="left")
            self._scout_gfi_var = tk.BooleanVar(value=False)
            tk.Checkbutton(
                row2, text=tr("只看 Good First Issue"), variable=self._scout_gfi_var,
                background=self.SURFACE, foreground=self.ORANGE, activebackground=self.SURFACE,
                activeforeground=self.ORANGE, selectcolor=self.SURFACE_ALT, highlightthickness=0, bd=0,
                font=("SF Pro Text", 10, "bold"),
            ).pack(side="left", padx=(16, 0))
            self._scout_hide_taken_var = tk.BooleanVar(value=True)
            hide_taken_button = tk.Checkbutton(
                row2, text=tr("只显示没人占用的"), variable=self._scout_hide_taken_var,
                background=self.SURFACE, foreground=self.TEXT, activebackground=self.SURFACE,
                activeforeground=self.TEXT, selectcolor=self.SURFACE_ALT, highlightthickness=0, bd=0,
                font=("SF Pro Text", 10),
            )
            hide_taken_button.pack(side="left", padx=(10, 0))
            ttk.Label(row2, text=tr("评分引擎"), style="Muted.TLabel", font=self._font("SF Pro Text", 9, "bold")).pack(side="left", padx=(16, 6))
            self._scout_engine_var = tk.StringVar()
            engine_names = []
            for provider in self.llm_providers:
                missing = provider.needs_key and not get_key(provider.id) and not os.environ.get(provider.api_key_env)
                engine_names.append(provider.display_name + (tr("（未导入密钥）") if missing else ""))
            if engine_names:
                self._scout_engine_var.set(engine_names[0])
                engine_menu = ttk.Combobox(row2, textvariable=self._scout_engine_var, values=engine_names, state="readonly", width=30, style="Dark.TCombobox")
                engine_menu.pack(side="left")
            else:
                ttk.Label(row2, text=tr("尚未配置大模型服务（⚙ 设置 → 模型服务）"), style="Status.TLabel").pack(side="left")
            self._scout_start_button = ttk.Button(
                options, text=tr("🚀 开始筛选（联网抓取 + AI 评分）"), command=lambda: self._scout_run_scan(dialog, module_vars, type_var, count_spin), style="Primary.TButton"
            )
            self._scout_start_button.pack(anchor="w", pady=(12, 0))

            # ---- 结果区
            result_card = ttk.LabelFrame(shell, text=tr("筛选结果（按 AI 评分排序）"), style="Dark.TLabelframe", padding=10)
            result_card.pack(fill="both", expand=True)
            columns = ("score", "number", "title", "difficulty", "taken", "module", "comments")
            self._scout_tree = ttk.Treeview(result_card, columns=columns, show="headings", style="Scout.Treeview", selectmode="browse")
            for cid, text, width, anchor in (
                ("score", tr("评分"), 55, "center"), ("number", "#", 65, "w"),
                ("title", tr("标题"), 420, "w"), ("difficulty", tr("难度"), 65, "center"),
                ("taken", tr("占用"), 75, "center"), ("module", tr("模块"), 150, "w"), ("comments", tr("评论"), 45, "center"),
            ):
                self._scout_tree.heading(cid, text=text)
                self._scout_tree.column(cid, width=width, anchor=anchor, stretch=(cid == "title"))
            tree_scroll = ttk.Scrollbar(result_card, orient="vertical", command=self._scout_tree.yview)
            self._scout_tree.configure(yscrollcommand=tree_scroll.set)
            self._scout_tree.pack(side="left", fill="both", expand=True)
            tree_scroll.pack(side="left", fill="y")
            detail = ttk.Frame(result_card, style="Card.TFrame", width=300)
            detail.pack(side="left", fill="y", padx=(10, 0))
            self._scout_detail_text = tk.Text(
                detail, wrap="word", width=34, relief="flat", background=self.SURFACE_ALT,
                foreground=self.MUTED, padx=10, pady=10, font=("SF Pro Text", 10), state="disabled",
            )
            self._scout_detail_text.pack(fill="both", expand=True)
            actions = ttk.Frame(shell, style="App.TFrame")
            actions.pack(fill="x", pady=(10, 0))
            ttk.Button(actions, text=tr("创建调查案件  →"), command=lambda: self._scout_create_case(), style="Primary.TButton").pack(side="left")
            ttk.Button(actions, text=tr("在浏览器打开"), command=lambda: self._scout_open_browser(), style="Action.TButton").pack(side="left", padx=(10, 0))
            ttk.Button(actions, text=tr("导出结果…"), command=lambda: self._scout_export(), style="Action.TButton").pack(side="left", padx=(10, 0))
            ttk.Label(actions, text=tr("评分标准可用 ~/.bugcompass/scout-prompt.md 私有化"), style="Muted.TLabel").pack(side="right")

            # 状态
            self._scout_records: list[IssueRecord] = []
            self._scout_scores: dict[int, IssueScore] = {}
            self._scout_tree.bind("<<TreeviewSelect>>", self._scout_on_select)
            hide_taken_button.configure(command=self._scout_repopulate)

            # 离线缓存：打开即可看上次结果
            cache = load_last_scan()
            if cache:
                records = scan_records_from_cache(cache)
                scores = scan_scores_from_cache(cache)
                fetched_at = str(cache.get("fetched_at", ""))[:16].replace("T", " ")
                if records:
                    self._scout_records = records
                    self._scout_scores = scores
                    self._scout_populate(records, scores)
                    self._scout_status_var.set(tr('显示上次筛选结果（{}，离线缓存）。可重新筛选获取最新。').format(fetched_at))
            dialog.grab_set()

        def _scout_current_provider(self):
            if not self.llm_providers:
                return None
            chosen = self._scout_engine_var.get()
            for provider in self.llm_providers:
                if chosen.startswith(provider.display_name):
                    return provider
            return self.llm_providers[0]

        def _scout_run_scan(self, dialog: Any, module_vars: dict[str, Any], type_var: Any, count_spin: Any) -> None:
            provider = self._scout_current_provider()
            if provider is None:
                self.message_box.showwarning(tr("缺少评分引擎"), tr("请先在 ⚙ 设置 → 模型服务里配置并导入密钥，再使用 AI 筛选。"), parent=dialog)
                return
            try:
                resolve_api_key(provider)
            except LLMError:
                if self.message_box.askyesno(tr("需要 API 密钥"), tr('{} 还没有密钥，现在导入吗？').format(provider.display_name), parent=dialog):
                    self._import_api_key_dialog(provider)
                return
            modules = [name for name, var in module_vars.items() if var.get()]
            types = [] if type_var.get() == tr("全部") else [type_var.get()]
            good_first_only = bool(self._scout_gfi_var.get())
            try:
                limit = max(10, min(100, int(count_spin.get())))
            except (TypeError, ValueError):
                limit = 40
            self._scout_start_button.configure(state="disabled")
            self._scout_status_var.set(tr("正在抓取 Blender tracker……"))

            def alive() -> bool:
                try:
                    return bool(dialog.winfo_exists())
                except Exception:
                    return False

            def apply_ui(fn: Any) -> None:
                self._main_queue.put(lambda: fn() if alive() else None)

            def work() -> None:
                try:
                    from .issue_scout import score_issues

                    records = fetch_open_issues(limit=limit, progress=lambda text: apply_ui(lambda: self._scout_status_var.set(text)))
                    records = filter_issues(records, modules=modules, types=types, good_first_only=good_first_only)
                    if not records:
                        apply_ui(lambda: (self._scout_status_var.set(tr("没有符合条件的 issue，试着放宽模块或类型。")), self._scout_start_button.configure(state="normal")))
                        return
                    enrich_with_comments(records, progress=lambda text: apply_ui(lambda: self._scout_status_var.set(text)))
                    scores = score_issues(provider, records, progress=lambda text: apply_ui(lambda: self._scout_status_var.set(text)))
                    # 评论区抓取晚于过滤：给确定性判断补一次机会（抓到 PR 链接/指派）。
                    for record in records:
                        score = scores.get(record.number)
                        det_taken, det_evidence = deterministic_taken(record)
                        if det_taken and score is not None and not score.taken:
                            score.taken = True
                            score.taken_evidence = det_evidence

                    def done() -> None:
                        self._scout_records = records
                        self._scout_scores = scores
                        self._scout_populate(records, scores)
                        save_scan(records, scores, {"modules": modules, "types": types, "limit": limit, "engine": provider.id})
                        ranked = sum(1 for s in scores.values() if s.score is not None)
                        taken_count = sum(1 for s in scores.values() if s.taken)
                        self._scout_status_var.set(
                            tr('筛选完成：{} 个 issue，{} 个已评分，{} 个疑似已有人接手（已默认隐藏，可取消勾选查看）。（已缓存，断网可看）').format(len(records), ranked, taken_count)
                        )
                        self._scout_start_button.configure(state="normal")

                    apply_ui(done)
                except ScoutError as exc:
                    message = str(exc)
                    apply_ui(lambda: (self._scout_status_var.set(message), self._scout_start_button.configure(state="normal")))
                except Exception as exc:  # pragma: no cover
                    message = tr('筛选失败：{}').format(exc)
                    apply_ui(lambda: (self._scout_status_var.set(message), self._scout_start_button.configure(state="normal")))

            threading.Thread(target=work, daemon=True).start()

        def _scout_populate(self, records: list[IssueRecord], scores: dict[int, IssueScore]) -> None:
            tree = self._scout_tree
            tree.delete(*tree.get_children())
            hide_taken = bool(getattr(self, "_scout_hide_taken_var", None) and self._scout_hide_taken_var.get())
            ranked = sorted(records, key=lambda r: (scores.get(r.number).score is None if r.number in scores else True, -(scores[r.number].score or 0) if r.number in scores else 0))
            for record in ranked:
                score = scores.get(record.number)
                if hide_taken and score is not None and score.taken:
                    continue
                value = f"{score.score}" if score and score.score is not None else "—"
                module = record.module_labels[0].removeprefix("Module/") if record.module_labels else tr("其他")
                title = ("★ " + record.title) if record.good_first else record.title
                taken_text = tr("已占用") if (score and score.taken) else tr("空闲")
                tree.insert("", "end", iid=str(record.number), values=(
                    value, f"#{record.number}", title[:80],
                    score.difficulty if score else tr("未知"), taken_text, module, record.comments,
                ))

        def _scout_repopulate(self) -> None:
            if self._scout_records:
                self._scout_populate(self._scout_records, self._scout_scores)

        def _scout_selected_record(self) -> IssueRecord | None:
            selection = self._scout_tree.selection()
            if not selection:
                return None
            number = int(selection[0])
            return next((r for r in self._scout_records if r.number == number), None)

        def _scout_on_select(self, _event: Any = None) -> None:
            record = self._scout_selected_record()
            if record is None:
                return
            score = self._scout_scores.get(record.number)
            lines = [f"#{record.number} {record.title}", ""]
            if score:
                lines.append(tr('AI 评分：{}/10 · {}').format(score.score if score.score is not None else '未评分', score.difficulty))
                lines.append(tr('理由：{}').format(score.reason))
                if score.taken:
                    lines.append(tr('⚠️ 疑似已有人接手：{}').format(score.taken_evidence or '证据见 tracker'))
                lines.append("")
            lines.append(tr('标签：{}').format(', '.join(record.labels) or '无'))
            lines.append(tr('创建：{} · 评论 {}').format(record.created_at[:10], record.comments))
            lines.append("")
            lines.append(record.body[:1500] or tr("（正文为空）"))
            self._scout_detail_text.configure(state="normal")
            self._scout_detail_text.delete("1.0", "end")
            self._scout_detail_text.insert("1.0", "\n".join(lines))
            self._scout_detail_text.configure(state="disabled")

        def _scout_create_case(self) -> None:
            record = self._scout_selected_record()
            if record is None:
                self.message_box.showinfo(tr("选择 Issue"), tr("先在列表里选中一个 issue。"), parent=self._scout_dialog)
                return
            score = self._scout_scores.get(record.number)
            if score is not None and score.taken:
                if not self.message_box.askyesno(
                    tr("可能已有人接手"),
                    tr('#{} 疑似已有人在做：\n{}\n\n仍然要为它创建调查案件吗？（练习调查本身没问题，别提交重复修复即可）').format(record.number, score.taken_evidence or '评论中出现 PR/认领迹象'),
                    parent=self._scout_dialog,
                ):
                    return
            text = issue_to_bug_text(record, score)
            workspace_repo = None
            try:
                from .workspace import load_workspace

                workspace_repo = load_workspace(self.controller.workspace_path).repo_path
            except BugCompassError:
                workspace_repo = None
            self._scout_dialog.destroy()
            if workspace_repo is not None and self.controller.validate_repository(workspace_repo).valid:
                self.repo_var.set(str(workspace_repo))
                self.bug_text.delete("1.0", "end")
                self.bug_text.insert("1.0", text)
                self._set_char_count()
                if not self.busy:
                    self._start_create()
                else:
                    self.show_new_page()
                    self.progress_var.set(tr("已填入 issue，等当前任务结束后点「开始调查」。"))
            else:
                self.bug_text.delete("1.0", "end")
                self.bug_text.insert("1.0", text)
                self._set_char_count()
                self.show_new_page()
                self.progress_var.set(tr("已填入选中的 issue；请先选择本地 Blender 源码文件夹，再点「开始调查」。"))

        def _scout_open_browser(self) -> None:
            record = self._scout_selected_record()
            if record is None:
                return
            import subprocess as _sp
            import sys as _sys
            try:
                if _sys.platform == "darwin":
                    _sp.Popen(["open", record.url])
                elif os.name == "nt":
                    os.startfile(record.url)  # type: ignore[attr-defined]
                else:
                    _sp.Popen(["xdg-open", record.url])
            except OSError:
                pass

        def _scout_export(self) -> None:
            if not self._scout_records:
                self.message_box.showinfo(tr("导出"), tr("还没有筛选结果可导出。"), parent=self._scout_dialog)
                return
            target = self.file_dialog.asksaveasfilename(
                title=tr("导出筛选结果"), defaultextension=".md",
                initialfile=f"ai-issue-scan-{__import__('datetime').datetime.now().strftime('%Y%m%d-%H%M')}.md",
                filetypes=[("Markdown", "*.md")], parent=self._scout_dialog,
            )
            if not target:
                return
            try:
                export_scan_markdown(self._scout_records, self._scout_scores, Path(target))
            except OSError as exc:
                self.message_box.showerror(tr("导出失败"), str(exc), parent=self._scout_dialog)
                return
            self.message_box.showinfo(tr("导出完成"), tr('已保存：\n{}').format(target), parent=self._scout_dialog)

        # ------------------------------------------------------- 密钥导入
        def _import_api_key_dialog(self, provider: LLMProviderConfig) -> None:
            """图形化导入 API 密钥（零命令行）。存入钥匙串或本地权限文件，永不进入备份/诊断包。"""
            dialog = tk.Toplevel(self)
            dialog.title(tr("导入 API 密钥"))
            dialog.configure(background=self.BACKGROUND)
            dialog.transient(self)
            dialog.grab_set()
            shell = ttk.Frame(dialog, style="App.TFrame", padding=24)
            shell.pack(fill="both", expand=True)
            ttk.Label(shell, text="API KEY", style="Eyebrow.TLabel").pack(anchor="w")
            ttk.Label(shell, text=tr("导入密钥"), style="Title.TLabel", font=self._font("SF Pro Display", 20, "bold")).pack(anchor="w", pady=(4, 6))
            ttk.Label(shell, text=f"{provider.label} · {provider.model}", style="Status.TLabel").pack(anchor="w")
            ttk.Label(
                shell,
                text=tr("密钥将保存在") + storage_hint() + tr("，\n不会写入 providers.json、备份、诊断包或统计上报；也可以随时在这里删除。"),
                style="Muted.TLabel", justify="left",
            ).pack(anchor="w", pady=(10, 12))
            entry = tk.Entry(
                shell, show="•",
                background=self.SURFACE_ALT, foreground=self.TEXT, insertbackground=self.TEXT,
                relief="flat", highlightthickness=1, highlightbackground=self.BORDER,
                highlightcolor=self.ORANGE, font=("SF Mono", 12),
            )
            existing = get_key(provider.id)
            if existing:
                entry.insert(0, existing)
            entry.pack(fill="x", ipady=6)
            entry.focus_set()
            status_var = tk.StringVar(value=tr("已存在导入的密钥，可直接覆盖更新。") if existing else "")
            ttk.Label(shell, textvariable=status_var, style="PageStatus.TLabel", justify="left").pack(anchor="w", pady=(8, 0))

            def save_and_test() -> None:
                value = entry.get().strip()
                if not value:
                    status_var.set(tr("请先粘贴密钥。"))
                    return
                try:
                    mode_used = save_key(provider.id, value)
                except Exception as exc:
                    status_var.set(tr('保存失败：{}').format(exc))
                    return
                status_var.set(tr('已保存（{}），正在测试连接……').format(storage_hint() if mode_used == 'file' else 'macOS 钥匙串'))

                def work() -> None:
                    ok, message = test_connection(provider)
                    try:
                        from datetime import datetime as _dt

                        from .resources import logs_dir

                        log_path = logs_dir() / "llm-test.log"
                        log_path.parent.mkdir(parents=True, exist_ok=True)
                        with log_path.open("a", encoding="utf-8") as fh:
                            fh.write(f"{_dt.now().isoformat(timespec='seconds')}  {provider.id}  {provider.model}  {'OK' if ok else 'FAIL'}  {message}\n")
                    except OSError:
                        pass

                    def apply() -> None:
                        suffix = "" if ok else tr("（详情已记录到 ~/.bugcompass/logs/llm-test.log）")
                        status_var.set((tr("✅ 密钥已保存，连接成功") if ok else "❌ ") + message + suffix)
                        self._refresh_engine_key_button()

                    self._main_queue.put(apply)

                threading.Thread(target=work, daemon=True).start()

            def remove_key() -> None:
                delete_key(provider.id)
                entry.delete(0, "end")
                status_var.set(tr("已删除本地存储的密钥。"))

            row = ttk.Frame(shell, style="App.TFrame")
            row.pack(fill="x", pady=(16, 0))
            ttk.Button(row, text=tr("取消"), command=dialog.destroy, style="Ghost.TButton").pack(side="right")
            ttk.Button(row, text=tr("保存并测试  →"), command=save_and_test, style="Primary.TButton").pack(side="right", padx=(0, 8))
            if existing:
                ttk.Button(row, text=tr("删除已存密钥"), command=remove_key, style="Ghost.TButton").pack(side="left")
            dialog.bind("<Return>", lambda _e: save_and_test())
            self._fit_dialog(dialog, 580, 420)

        def _fit_dialog(self, dialog: Any, min_width: int, min_height: int) -> None:
            """对话框自动适应内容（公共实现在 widgets.fit_dialog）。"""
            fit_dialog(dialog, min_width, min_height)

        # -------------------------------------------------------------- 设置
        def _open_settings(self) -> None:
            dialog = tk.Toplevel(self)
            dialog.title(tr("设置"))
            dialog.configure(background=self.BACKGROUND)
            dialog.transient(self)
            dialog.grab_set()
            shell = ttk.Frame(dialog, style="App.TFrame", padding=24)
            shell.pack(fill="both", expand=True)
            ttk.Label(shell, text="SETTINGS", style="Eyebrow.TLabel").pack(anchor="w")
            ttk.Label(shell, text=tr("设置"), style="Title.TLabel", font=self._font("SF Pro Display", 22, "bold")).pack(anchor="w", pady=(4, 10))
            ttk.Label(shell, text=tr('BugCompass v{} · 设置保存在本地（~/.bugcompass/settings.json）').format(__version__), style="Muted.TLabel").pack(anchor="w", pady=(0, 14))

            # 界面语言
            language_card = ttk.LabelFrame(shell, text=tr("界面语言"), style="Dark.TLabelframe", padding=14)
            language_card.pack(fill="x", pady=(0, 12))
            language_names = {"zh": "中文", "en": "English"}
            current_code = str(self.settings.get("language", "zh"))
            language_var = tk.StringVar(value=language_names.get(current_code, "中文"))

            def change_language(*_args: Any) -> None:
                code = "en" if language_var.get() == "English" else "zh"
                if code == str(self.settings.get("language", "zh")):
                    return
                self.settings["language"] = code
                save_settings(self.settings)
                set_language(code)
                self.message_box.showinfo(
                    tr("设置"),
                    tr("语言已切换。重启 BugCompass 后全部界面生效。"),
                    parent=dialog,
                )

            language_menu = ttk.Combobox(
                language_card, textvariable=language_var, values=list(language_names.values()),
                state="readonly", width=12, style="Dark.TCombobox",
                font=self._font("SF Pro Text", 11),
            )
            language_menu.pack(side="left")
            language_menu.bind("<<ComboboxSelected>>", change_language)
            ttk.Label(language_card, text=tr("切换后重启应用即可完全生效。"), style="Muted.TLabel").pack(side="left", padx=(12, 0))

            # 界面缩放
            zoom_box = ttk.Frame(shell, style="Surface.TFrame", padding=14)
            zoom_box.pack(fill="x")
            ttk.Label(zoom_box, text=tr("界面缩放"), style="Heading.TLabel").pack(anchor="w")
            ttk.Label(zoom_box, text=tr('当前系统 DPI 缩放：约 {}%。使用 Ctrl + 滚轮 也可以随时调整。').format(scale_percent(self)), style="Muted.TLabel").pack(anchor="w", pady=(3, 8))
            zoom_var = tk.IntVar(value=self.ui_scale.percent)
            zoom_label = ttk.Label(zoom_box, text=f"{self.ui_scale.percent}%", style="Status.TLabel")
            zoom_label.pack(anchor="w")
            zoom_slider = tk.Scale(
                zoom_box, variable=zoom_var, from_=UiScale.MIN, to=UiScale.MAX,
                orient="horizontal", resolution=5,
                background=self.SURFACE, foreground=self.TEXT,
                troughcolor=self.SURFACE_ALT, highlightthickness=0, bd=0,
                length=440,
            )
            zoom_slider.pack(fill="x")

            # 模型服务（API）
            llm_box = ttk.Frame(shell, style="Surface.TFrame", padding=14)
            llm_box.pack(fill="x", pady=(10, 0))
            ttk.Label(llm_box, text=tr("模型服务（API 调查引擎）"), style="Heading.TLabel").pack(anchor="w")
            if self.llm_provider_error:
                ttk.Label(llm_box, text=tr('配置读取失败：{}').format(self.llm_provider_error), style="Status.TLabel", justify="left").pack(anchor="w", pady=(3, 6))
            ttk.Label(
                llm_box,
                text=tr("密钥只从环境变量读取，永不写入文件、备份或诊断包。\n"
                     "providers.json 只保存端点、模型名和「密钥环境变量名」。"),
                style="Muted.TLabel", justify="left",
            ).pack(anchor="w", pady=(3, 8))
            if self.llm_providers:
                provider_names = [provider.display_name for provider in self.llm_providers]
                llm_row = ttk.Frame(llm_box, style="Surface.TFrame")
                llm_row.pack(fill="x")
                self._llm_test_var = tk.StringVar(value=provider_names[0])
                ttk.Label(llm_row, text=tr("服务"), style="Muted.TLabel").pack(side="left", padx=(0, 6))
                provider_combo = ttk.Combobox(llm_row, textvariable=self._llm_test_var, values=provider_names, state="readonly", width=28, style="Dark.TCombobox")
                provider_combo.pack(side="left")
                self._llm_status_var = tk.StringVar(value="")
                ttk.Label(llm_box, textvariable=self._llm_status_var, style="Status.TLabel", justify="left").pack(anchor="w", pady=(6, 4))

                def chosen_provider() -> LLMProviderConfig:
                    return next(
                        (item for item in self.llm_providers if item.display_name == self._llm_test_var.get()),
                        self.llm_providers[0],
                    )

                def run_test() -> None:
                    provider = chosen_provider()
                    self._llm_status_var.set(tr('正在测试 {}……').format(provider.display_name))

                    def work() -> None:
                        ok, message = test_connection(provider)
                        def apply() -> None:
                            self._llm_status_var.set(("✅ " if ok else "❌ ") + message)
                        self._main_queue.put(apply)

                    threading.Thread(target=work, daemon=True).start()

                def use_provider() -> None:
                    provider = chosen_provider()
                    self.settings["active_engine"] = provider.id
                    save_settings(self.settings)
                    if hasattr(self, "engine_combo"):
                        self.engine_var.set(provider.display_name)
                    self._llm_status_var.set(tr('已设为当前引擎：{}').format(provider.display_name))

                ttk.Label(llm_row, text=tr("模型"), style="Muted.TLabel").pack(side="left", padx=(14, 6))
                self._llm_model_var = tk.StringVar(value=chosen_provider().model)
                model_combo = ttk.Combobox(llm_row, textvariable=self._llm_model_var, values=chosen_provider().model_options, width=22, style="Dark.TCombobox")
                model_combo.pack(side="left")

                def apply_model_change(*_args: Any) -> None:
                    model = self._llm_model_var.get().strip()
                    provider = chosen_provider()
                    if not model or model == provider.model:
                        return
                    try:
                        self.llm_providers = set_provider_model(provider.id, model)
                    except LLMError as exc:
                        self._llm_status_var.set(str(exc))
                        return
                    updated = provider_by_id(self.llm_providers, provider.id)
                    if updated is not None:
                        self._llm_model_var.set(updated.model)
                        model_combo.configure(values=updated.model_options)
                    if hasattr(self, "engine_combo"):
                        self.engine_combo.configure(values=self._engine_options())
                        if str(self.settings.get("active_engine")) == provider.id and updated is not None:
                            self.engine_var.set(updated.display_name)
                    self._llm_status_var.set(tr('模型已保存：{}').format(model))

                model_combo.bind("<FocusOut>", apply_model_change)
                model_combo.bind("<Return>", apply_model_change)
                model_combo.bind("<<ComboboxSelected>>", apply_model_change)
                fetch_models_btn = ttk.Button(llm_row, text=tr("⟳ 拉取模型"), style="Ghost.TButton")

                def fetch_models() -> None:
                    provider = chosen_provider()
                    fetch_models_btn.configure(state="disabled")
                    self._llm_status_var.set(tr("正在从 {} 获取模型列表……").format(provider.label))

                    def work() -> None:
                        try:
                            models = list_models(provider)
                        except LLMError as exc:
                            message = str(exc)

                            def apply_fail() -> None:
                                self._llm_status_var.set(message)
                                fetch_models_btn.configure(state="normal")

                            self._main_queue.put(apply_fail)
                            return

                        def apply_ok() -> None:
                            fetch_models_btn.configure(state="normal")
                            if not models:
                                self._llm_status_var.set(tr("服务没有返回任何模型。"))
                                return
                            model_combo.configure(values=models)
                            current = self._llm_model_var.get().strip()
                            if current and current not in models:
                                # 当前模型已不在服务列表（如已退役）→ 自动切换并保存
                                new_model = next((m for m in models if "flash" in m or "mini" in m), models[0])
                                self._llm_model_var.set(new_model)
                                try:
                                    self.llm_providers = set_provider_model(provider.id, new_model)
                                except LLMError:
                                    pass
                                self._llm_status_var.set(tr("当前模型已不在服务列表中，已切换为：{}").format(new_model))
                            else:
                                self._llm_status_var.set(tr("已从服务获取 {} 个模型。").format(len(models)))

                        self._main_queue.put(apply_ok)

                    threading.Thread(target=work, daemon=True).start()

                fetch_models_btn.configure(command=fetch_models)
                fetch_models_btn.pack(side="left", padx=(8, 0))
                ttk.Button(llm_row, text=tr("测试连接"), command=run_test, style="Action.TButton").pack(side="left", padx=(10, 0))
                ttk.Button(llm_row, text=tr("设为当前引擎"), command=use_provider, style="Action.TButton").pack(side="left", padx=(8, 0))
                import_btn = ttk.Button(llm_row, text=tr("导入密钥…"), style="Primary.TButton")
                import_btn.pack(side="left", padx=(8, 0))
                key_hint_label = ttk.Label(llm_box, text="", style="Muted.TLabel")
                key_hint_label.pack(anchor="w", pady=(4, 2))

                def refresh_provider_ui(*_args: Any) -> None:
                    chosen = chosen_provider()
                    self._llm_model_var.set(chosen.model)
                    model_combo.configure(values=chosen.model_options)
                    if chosen.needs_key:
                        import_btn.configure(state="normal", command=lambda: self._import_api_key_dialog(chosen))
                        stored = tr("已导入 ✓") if get_key(chosen.id) else tr("未导入")
                        key_hint_label.configure(text=tr('密钥：{} · 环境变量名 {} · 存储：{}').format(stored, chosen.api_key_env, storage_hint()))
                    else:
                        import_btn.configure(state="disabled", command=lambda: None)
                        key_hint_label.configure(text=tr("本地服务，无需密钥"))

                refresh_provider_ui()
                provider_combo.bind("<<ComboboxSelected>>", refresh_provider_ui)
            else:
                ttk.Label(llm_box, text=tr("没有可用服务：运行 bugcompass llm init-config 生成配置模板后重开设置。"), style="Muted.TLabel", justify="left").pack(anchor="w")

            # 统计上报（默认关闭）
            tele_box = ttk.Frame(shell, style="Surface.TFrame", padding=14)
            tele_box.pack(fill="x", pady=(10, 0))
            ttk.Label(tele_box, text=tr("统计上报"), style="Heading.TLabel").pack(anchor="w")
            ttk.Label(
                tele_box,
                text=tr("默认关闭。开启后也只会在本地记录聚合计数（模型、耗时、轮次、token、成本），\n"
                     "不会记录问题描述、证据、源码路径或文件内容；数据只有在“导出待发数据”后才可能离开本机。"),
                style="Muted.TLabel", justify="left",
            ).pack(anchor="w", pady=(3, 8))
            tele_var = tk.BooleanVar(value=telemetry_enabled())
            tk.Checkbutton(
                tele_box, text=tr("我了解并主动开启本地统计记录"), variable=tele_var,
                background=self.SURFACE, foreground=self.TEXT,
                activebackground=self.SURFACE, activeforeground=self.TEXT,
                selectcolor=self.SURFACE_ALT, anchor="w",
            ).pack(anchor="w")
            pending_count = len(telemetry_pending())
            ttk.Label(tele_box, text=tr('本地待发事件：{} 条').format(pending_count), style="Muted.TLabel").pack(anchor="w", pady=(6, 4))
            tele_row = ttk.Frame(tele_box, style="Surface.TFrame")
            tele_row.pack(anchor="w")

            def export_tele() -> None:
                from .resources import user_root

                target = export_telemetry_pending(user_root())
                self.message_box.showinfo(
                    tr("导出待发数据"),
                    tr('已导出到：\n{}\n\n请自行检查内容后再决定是否发送。').format(target) if target else tr("当前没有待发事件。"),
                    parent=dialog,
                )

            def clear_tele() -> None:
                removed = clear_telemetry_pending()
                self.message_box.showinfo(tr("清空待发数据"), tr('已清空 {} 条本地待发事件。').format(removed), parent=dialog)

            ttk.Button(tele_row, text=tr("导出待发数据"), command=export_tele, style="Action.TButton").pack(side="left")
            ttk.Button(tele_row, text=tr("清空待发数据"), command=clear_tele, style="Ghost.TButton").pack(side="left", padx=(8, 0))

            # 数据目录
            data_row = ttk.Frame(shell, style="App.TFrame")
            data_row.pack(fill="x", pady=(14, 0))
            from .resources import logs_dir, user_root

            ttk.Label(data_row, text=tr('数据目录：{}').format(user_root()), style="Muted.TLabel").pack(side="left")
            ttk.Button(data_row, text=tr("打开"), command=lambda: self._open_path_in_explorer(logs_dir()), style="Ghost.TButton").pack(side="left", padx=(8, 0))

            def save() -> None:
                percent = int(zoom_var.get())
                self.settings["ui_scale_percent"] = percent
                self.settings["telemetry_enabled"] = bool(tele_var.get())
                save_settings(self.settings)
                set_telemetry_enabled(bool(tele_var.get()))
                if percent != self.ui_scale.percent:
                    self.ui_scale.set_percent(percent)
                    apply_scaling(self, extra=max(0.5, self.ui_scale.factor))
                    self._configure_styles(self._ttk_module)
                    self._refresh_zoomable_content()
                dialog.destroy()

            row = ttk.Frame(shell, style="App.TFrame")
            row.pack(fill="x", pady=(16, 0))
            ttk.Button(row, text=tr("取消"), command=dialog.destroy, style="Ghost.TButton").pack(side="right")
            ttk.Button(row, text=tr("保存  →"), command=save, style="Primary.TButton").pack(side="right", padx=(0, 8))
            self._fit_dialog(dialog, 640, 560)

        def _refresh_current_case(self) -> None:
            if self.current_case is None:
                return
            try:
                self._show_case(self.controller.load_case(self.current_case.case_id))
                self.copy_status_var.set(tr("结果已刷新。"))
                self._refresh_recent_cases()
            except BugCompassError as exc:
                self.message_box.showerror(tr("刷新失败"), str(exc), parent=self)

        def _copy_instruction(self) -> None:
            if self.current_case is None:
                return
            instruction = self.controller.investigation_instruction(self.current_case.case_id)
            try:
                self.clipboard_clear()
                self.clipboard_append(instruction)
                self.update_idletasks()
            except tk.TclError as exc:
                self.message_box.showerror(tr("复制失败"), tr('无法访问系统剪贴板：{}').format(exc), parent=self)
                return
            self.copy_status_var.set(tr("调查指令已复制，请粘贴到当前 BugCompass 项目的 Codex 新任务中。"))

        def _new_another(self) -> None:
            if self.busy:
                self.message_box.showwarning(tr("调查仍在进行"), tr("请先等待调查完成，或在正在运行的案件中点击“停止调查”。"), parent=self)
                return
            self.bug_text.delete("1.0", "end")
            self._set_char_count()
            self.progress_var.set("")
            self.current_case = None
            self.show_new_page()
            self.bug_text.focus_set()

        def _toggle_details(self) -> None:
            if self.details_visible:
                self._hide_details()
            else:
                self.details_frame.grid(row=3, column=0, sticky="ew")
                self.details_button.configure(text=tr("收起详细环境信息"))
                self.details_visible = True

        def _hide_details(self) -> None:
            self.details_frame.grid_remove()
            self.details_button.configure(text=tr("显示详细环境信息"))
            self.details_visible = False

        def _refresh_recent_cases(self) -> None:
            self.recent_list.delete(0, "end")
            recent = self.controller.recent_cases()
            self.recent_case_ids = [item.case_id for item in recent]
            if not recent:
                self.recent_list.insert("end", tr("暂无案件"))
                return
            status_names = {
                "new": tr("新建"),
                "investigating": tr("调查中"),
                "complete": tr("已完成"),
                "failed": tr("失败"),
                "cancelled": tr("已取消"),
            }
            for item in recent:
                created = item.created_at.replace("T", " ").replace("Z", "")
                if self.codex_active and item.case_id == self.active_case_id:
                    status = tr("调查中")
                elif item.status == "investigating":
                    status = tr("上次中断")
                else:
                    status = status_names.get(item.status, item.status)
                self.recent_list.insert("end", f"{item.case_id} · {created} · {status}")

        def _open_selected_recent(self, _event: Any = None) -> None:
            selection = self.recent_list.curselection()
            if not selection or not self.recent_case_ids:
                return
            index = int(selection[0])
            if index >= len(self.recent_case_ids):
                return
            try:
                self._show_case(self.controller.load_case(self.recent_case_ids[index]))
            except BugCompassError as exc:
                self.message_box.showerror(tr("无法打开案件"), str(exc), parent=self)

    try:
        app = BugCompassApp()
    except tk.TclError as exc:
        detail = str(exc).splitlines()[0] or tr("Tk 初始化失败")
        print(
            tr('错误：无法启动图形界面。Tkinter 模块已安装，但 Tk 运行环境或图形显示不可用。请换用带完整 Tk 支持的 Python，并在本地图形桌面中启动。详情：{}').format(detail)
        )
        return 2
    app.mainloop()
    return 0
