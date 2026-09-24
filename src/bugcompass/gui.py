from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .backup import BackupError, create_backup, inspect_backup, restore_backup
from .codex_runner import CodexRunBusyError, CodexRunResult, CodexRunner
from .diagnostics import export_bundle, install_crash_handler, install_tk_handler
from .dpi import apply_scaling, enable_windows_dpi_awareness, scale_percent
from .gui_controller import CaseView, GuiController
from .llm import LLMError, LLMProviderConfig, load_providers, test_connection
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
from .settings import load_settings, save_settings
from .telemetry import (
    clear_pending as clear_telemetry_pending,
    export_pending as export_telemetry_pending,
    is_enabled as telemetry_enabled,
    pending as telemetry_pending,
    record_run_metrics,
    set_enabled as set_telemetry_enabled,
)
from .widgets import MouseWheelRouter, ScrollableFrame, UiScale
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
        "idle": f"● {engine_label} 空闲",
        "working": f"● {engine_label} 工作中",
        "stopping": f"● {engine_label} 正在停止",
        "complete": f"● {engine_label} 已完成",
        "failed": f"● {engine_label} 调查失败",
        "stopped": f"● {engine_label} 已停止",
    }
    if active_case_id and run_state in {"working", "stopping"}:
        if viewed_case_id == active_case_id:
            return CodexStatusPresentation(
                run_state,
                labels[run_state],
                f"案件 {active_case_id}\n{run_detail}",
                run_state == "working",
            )
        return CodexStatusPresentation(
            "other",
            "● 其他案件调查中",
            f"Codex 正在处理 {active_case_id}。\n当前案件没有在运行。",
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
        "complete": ("complete", "上一次 Codex 调查已经完成，可以继续补充调查。"),
        "failed": ("failed", "上一次调查失败，案件内容仍然保留。"),
        "cancelled": ("stopped", "上一次调查已停止，可以从当前 Case 继续。"),
        "investigating": ("stopped", "没有检测到这个案件的 Codex 进程；上一次运行可能已中断。"),
        "new": ("idle", "案件尚未开始或尚未产生调查结果。"),
    }
    state, detail = persisted.get(viewed_status, (run_state, run_detail))
    return CodexStatusPresentation(state, labels.get(state, f"● Codex {state}"), detail, False)


def check_gui() -> tuple[bool, str]:
    try:
        import tkinter  # noqa: F401
        from tkinter import ttk  # noqa: F401
    except (ImportError, ModuleNotFoundError) as exc:
        return False, f"Tkinter 不可用：{exc}"
    if not CodexRunner.find_executable():
        return False, (
            "Tkinter 可以加载，但找不到 Codex CLI。可以安装并登录 Codex，"
            "或者在设置中配置大模型 API（bugcompass llm init-config）作为调查引擎。"
        )
    return True, "Tkinter、BugCompass GUI 和 Codex CLI 均可用。"


def run_gui() -> int:
    try:
        import tkinter as tk  # noqa: F401
        from tkinter import filedialog, messagebox, simpledialog, ttk
    except (ImportError, ModuleNotFoundError) as exc:
        print(f"错误：Tkinter 不可用：{exc}")
        return 2

    # 必须在创建任何 Tk 窗口之前声明 DPI aware，否则 Windows 会用位图拉伸
    # 整个窗口，在 150%/200% 缩放下出现发虚、拖影和断层（见 dpi.py）。
    enable_windows_dpi_awareness()
    install_crash_handler()

    # Codex 缺失不再阻止启动：历史案件仍可离线浏览（安装包交付的关键路径）。
    codex_warning = None
    if not CodexRunner.find_executable():
        codex_warning = (
            "未找到 Codex CLI：仍可以离线浏览、编辑历史案件和导出备份，"
            "但自动调查不可用。安装并登录 Codex 后重启 BugCompass 即可。"
        )

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

        def __init__(self) -> None:
            super().__init__()
            self.title(f"BugCompass — Blender Bug 调查助手 · v{__version__}")
            self.geometry("1120x760")
            self.minsize(940, 650)
            self.configure(background=self.BACKGROUND)

            self.controller = GuiController()
            # 资源根目录在源码树 / PyInstaller / 便携版下各不相同（见 resources.py），
            # 打包成安装包后 parents[2] 不再存在，必须走统一的资源定位。
            from .resources import data_root

            self._data_root_path = data_root()
            self.codex_runner = CodexRunner(self._data_root_path)
            self.practice_manager = PracticeManager(self._data_root_path, self.controller.workspace_path)
            # 大模型 API 引擎：默认不启用（active_engine=codex），配置见 llm.py。
            try:
                self.llm_providers: list[LLMProviderConfig] = load_providers()
                self.llm_provider_error = ""
            except LLMError as exc:
                self.llm_providers = []
                self.llm_provider_error = str(exc)
            self._llm_investigators: dict[str, LLMInvestigator] = {}
            self._active_runner: Any = None
            # 工作线程 → 主线程的安全回调通道（测试连接等异步操作用）。
            self._main_queue: queue.SimpleQueue = queue.SimpleQueue()
            self.repo_var = tk.StringVar()
            self.repo_status_var = tk.StringVar(value="请选择本地 Blender 源码文件夹。")
            self.char_count_var = tk.StringVar(value="0 个字符")
            self.progress_var = tk.StringVar()
            self.result_summary_var = tk.StringVar()
            self.result_hint_var = tk.StringVar()
            self.copy_status_var = tk.StringVar()
            self.codex_status_var = tk.StringVar(value="● Codex 空闲")
            self.codex_status_detail_var = tk.StringVar(value="当前没有正在运行的调查。")
            self.details_visible = False
            self.busy = False
            self.codex_active = False
            self.active_case_id: str | None = None
            self.codex_state = "idle"
            self.codex_state_detail = "当前没有正在运行的调查。"
            self.current_case: CaseView | None = None
            self.current_reveal: PracticeReveal | None = None
            self.practice_cases: list[PracticeCase] = []
            self.recent_case_ids: list[str] = []
            self.events: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
            self.path_cards: list[Any] = []
            self.mindmap: MindMapCanvas | None = None
            self.causal_graph: dict[str, Any] = {"nodes": [], "edges": []}

            # 设置 / 缩放 / 滚轮路由（Windows 滚动与拖影修复的一部分）。
            self.settings = load_settings()
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
            self.show_new_page()
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
            style.configure("Dark.TEntry", fieldbackground=self.SURFACE_ALT, foreground=self.TEXT, insertcolor=self.TEXT, padding=10, borderwidth=1)
            style.configure("Dark.TLabelframe", background=self.SURFACE, bordercolor=self.BORDER, relief="solid", borderwidth=1)
            style.configure("Dark.TLabelframe.Label", background=self.SURFACE, foreground=self.MUTED, font=self._font("SF Pro Text", 9, "bold"))

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
            ttk_module.Label(brand_text, text="Blender 调查台", style="SidebarTitle.TLabel").pack(anchor="w")
            ttk_module.Button(sidebar, text="＋  新建调查", command=self.show_new_page, style="Primary.TButton").pack(fill="x", pady=(0, 8))
            ttk_module.Button(sidebar, text="◫  历史 PR 练习", command=self.show_practice_page, style="Action.TButton").pack(fill="x", pady=(0, 28))
            ttk_module.Label(sidebar, text="最近案件", style="SidebarTitle.TLabel", font=self._font("SF Pro Text", 11, "bold")).pack(anchor="w")
            ttk_module.Label(sidebar, text="保存在本地 · 最多显示 10 个", style="SidebarHint.TLabel").pack(anchor="w", pady=(3, 12))
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

            self.page_host = ttk_module.Frame(shell, style="App.TFrame", padding=(34, 28, 34, 26))
            self.page_host.grid(row=0, column=1, sticky="nsew")
            self.page_host.rowconfigure(0, weight=1)
            self.page_host.columnconfigure(0, weight=1)

            self.new_page = ttk_module.Frame(self.page_host, style="App.TFrame")
            self.result_page = ttk_module.Frame(self.page_host, style="App.TFrame")
            self.practice_page = ttk_module.Frame(self.page_host, style="App.TFrame")
            for page in (self.new_page, self.result_page, self.practice_page):
                page.grid(row=0, column=0, sticky="nsew")

            self._build_new_page(tk_module, ttk_module)
            self._build_practice_page(tk_module, ttk_module)
            self._build_result_page(tk_module, ttk_module)

        def _build_practice_page(self, tk_module: Any, ttk_module: Any) -> None:
            page = self.practice_page
            page.columnconfigure(0, weight=2)
            page.columnconfigure(1, weight=3)
            page.rowconfigure(2, weight=1)
            header = ttk_module.Frame(page, style="App.TFrame")
            header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 16))
            ttk_module.Label(header, text="HISTORICAL PRACTICE", style="Eyebrow.TLabel").pack(anchor="w")
            ttk_module.Label(header, text="历史 PR 练习场", style="Title.TLabel").pack(anchor="w", pady=(4, 3))
            ttk_module.Label(header, text="在不知道答案的前提下调查真实 Blender Bug，然后与真实修复对照。", style="PageSubtitle.TLabel").pack(anchor="w")

            steps = ttk_module.Frame(page, style="App.TFrame")
            steps.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 16))
            for index, label in enumerate(("选择案例", "调查", "提交判断", "揭晓", "评分")):
                ttk_module.Label(steps, text=f"{index + 1}  {label}", style="StepActive.TLabel" if index == 0 else "Step.TLabel").pack(side="left", padx=(0, 7))

            list_card = ttk_module.LabelFrame(page, text="案例库 · 10", style="Dark.TLabelframe", padding=12)
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

            detail = ttk_module.LabelFrame(page, text="练习题面", style="Dark.TLabelframe", padding=20)
            detail.grid(row=2, column=1, sticky="nsew")
            detail.columnconfigure(0, weight=1)
            detail.rowconfigure(4, weight=1)
            self.practice_meta_var = tk_module.StringVar()
            self.practice_title_var = tk_module.StringVar(value="选择左侧案例")
            self.practice_symptom_var = tk_module.StringVar(value="这里会显示练习时允许看到的问题信息。")
            self.practice_steps_var = tk_module.StringVar()
            self.practice_status_var = tk_module.StringVar()
            ttk_module.Label(detail, textvariable=self.practice_meta_var, style="Status.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Label(detail, textvariable=self.practice_title_var, style="Heading.TLabel", font=self._font("SF Pro Display", 18, "bold"), wraplength=520, justify="left").grid(row=1, column=0, sticky="w", pady=(7, 8))
            ttk_module.Label(detail, textvariable=self.practice_symptom_var, style="Body.TLabel", wraplength=520, justify="left").grid(row=2, column=0, sticky="w")
            ttk_module.Label(detail, text="复现轮廓", style="Muted.TLabel", font=self._font("SF Pro Text", 9, "bold")).grid(row=3, column=0, sticky="w", pady=(18, 5))
            ttk_module.Label(detail, textvariable=self.practice_steps_var, style="Body.TLabel", wraplength=520, justify="left").grid(row=4, column=0, sticky="nw")
            ttk_module.Label(detail, textvariable=self.practice_status_var, style="Status.TLabel").grid(row=5, column=0, sticky="w", pady=(12, 8))
            self.practice_start_button = ttk_module.Button(detail, text="进入修复前版本并开始调查  →", command=self._start_practice, style="Primary.TButton", state="disabled")
            self.practice_start_button.grid(row=6, column=0, sticky="ew")

        def _build_new_page(self, tk_module: Any, ttk_module: Any) -> None:
            page = self.new_page
            page.columnconfigure(0, weight=1)
            page.rowconfigure(3, weight=1)
            header = ttk_module.Frame(page, style="App.TFrame")
            header.grid(row=0, column=0, sticky="ew", pady=(0, 18))
            ttk_module.Label(header, text="NEW INVESTIGATION", style="Eyebrow.TLabel").pack(anchor="w")
            ttk_module.Label(header, text="开始一次新的调查", style="Title.TLabel").pack(anchor="w", pady=(4, 3))
            ttk_module.Label(header, text="选择源码，粘贴问题。剩下的交给 BugCompass。", style="PageSubtitle.TLabel").pack(anchor="w")

            steps = ttk_module.Frame(page, style="App.TFrame")
            steps.grid(row=1, column=0, sticky="w", pady=(0, 18))
            for index, label in enumerate(("1  选择源码", "2  描述问题", "3  开始调查")):
                ttk_module.Label(steps, text=label, style="StepActive.TLabel" if index == 0 else "Step.TLabel").pack(side="left", padx=(0, 8))

            repo_card = ttk_module.LabelFrame(page, text="01  /  BLENDER 源码", style="Dark.TLabelframe", padding=20)
            repo_card.grid(row=2, column=0, sticky="ew", pady=(0, 14))
            repo_card.columnconfigure(0, weight=1)
            ttk_module.Label(repo_card, text="本地源码仓库", style="Heading.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Label(repo_card, text="只会读取源码和 Git 信息，创建案件时不会修改 Blender。", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 0))
            path_row = ttk_module.Frame(repo_card, style="Card.TFrame")
            path_row.grid(row=2, column=0, sticky="ew", pady=(13, 8))
            path_row.columnconfigure(0, weight=1)
            ttk_module.Entry(path_row, textvariable=self.repo_var, state="readonly", style="Dark.TEntry").grid(row=0, column=0, sticky="ew", padx=(0, 10), ipady=3)
            ttk_module.Button(path_row, text="选择源码文件夹", command=self._choose_repo, style="Action.TButton").grid(row=0, column=1)
            self.repo_status_label = ttk_module.Label(repo_card, textvariable=self.repo_status_var, style="Muted.TLabel")
            self.repo_status_label.grid(row=3, column=0, sticky="w")

            bug_card = ttk_module.LabelFrame(page, text="02  /  BUG 描述", style="Dark.TLabelframe", padding=20)
            bug_card.grid(row=3, column=0, sticky="nsew", pady=(0, 14))
            bug_card.columnconfigure(0, weight=1)
            bug_card.rowconfigure(1, weight=1)
            title_row = ttk_module.Frame(bug_card, style="Card.TFrame")
            title_row.grid(row=0, column=0, sticky="ew", pady=(0, 10))
            title_row.columnconfigure(0, weight=1)
            title_text = ttk_module.Frame(title_row, style="Card.TFrame")
            title_text.grid(row=0, column=0, sticky="w")
            ttk_module.Label(title_text, text="把问题原样贴进来", style="Heading.TLabel").pack(anchor="w")
            ttk_module.Label(title_text, text="标题、复现步骤、日志都可以。这里的内容不会被当成命令执行。", style="Muted.TLabel").pack(anchor="w", pady=(3, 0))
            ttk_module.Button(title_row, text="↑  导入 .md / .txt", command=self._import_issue, style="Ghost.TButton").grid(row=0, column=1)
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
            ttk_module.Label(engine_row, text="调查引擎", style="Muted.TLabel", font=self._font("SF Pro Text", 9, "bold")).pack(side="left", padx=(0, 8))
            self.engine_combo = ttk_module.Combobox(
                engine_row,
                textvariable=self.engine_var,
                values=self._engine_options(),
                state="readonly",
                width=32,
            )
            self.engine_combo.pack(side="left")
            self.engine_combo.bind("<<ComboboxSelected>>", self._on_engine_changed)
            ttk_module.Label(engine_row, text="默认 Codex CLI；大模型 API 可在设置里配置与测试", style="Muted.TLabel").pack(side="left", padx=(10, 0))

            action_row = ttk_module.Frame(page, style="App.TFrame")
            action_row.grid(row=5, column=0, sticky="ew")
            action_row.columnconfigure(0, weight=1)
            self.progress_label = ttk_module.Label(action_row, textvariable=self.progress_var, style="PageStatus.TLabel")
            self.progress_label.grid(row=0, column=0, sticky="w")
            self.create_button = ttk_module.Button(action_row, text="开始调查  →", command=self._start_create, style="Primary.TButton")
            self.create_button.grid(row=0, column=1, sticky="e")

        def _build_result_page(self, tk_module: Any, ttk_module: Any) -> None:
            page = self.result_page
            page.columnconfigure(0, weight=1)
            page.rowconfigure(4, weight=1)
            header = ttk_module.Frame(page, style="App.TFrame")
            header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
            ttk_module.Label(header, text="INVESTIGATION WORKSPACE", style="Eyebrow.TLabel").pack(anchor="w")
            ttk_module.Label(header, text="调查工作台", style="Title.TLabel").pack(anchor="w", pady=(4, 8))
            flow = ttk_module.Frame(header, style="App.TFrame")
            flow.pack(anchor="w")
            for index, label in enumerate(("Bug 描述", "调查中", "三条路径", "实验", "结论")):
                ttk_module.Label(flow, text=f"{index + 1}  {label}", style="StepActive.TLabel" if index == 2 else "Step.TLabel").pack(side="left", padx=(0, 7))

            summary_card = ttk_module.LabelFrame(page, text="案件概览", style="Dark.TLabelframe", padding=16)
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
            self.details_button = ttk_module.Button(summary_card, text="查看环境详情", command=self._toggle_details, style="Ghost.TButton")
            self.details_button.grid(row=2, column=0, sticky="w", pady=(8, 0))
            self.details_frame = ttk_module.Frame(summary_card, style="Card.TFrame")
            self.details_text = tk_module.Text(self.details_frame, height=7, wrap="none", font=self._font("SF Mono", 9), relief="flat", borderwidth=0, background=self.SURFACE_ALT, foreground=self.MUTED, padx=10, pady=10)
            self.details_text.pack(fill="both", expand=True, pady=(6, 0))

            actions = ttk_module.Frame(page, style="App.TFrame")
            actions.grid(row=2, column=0, sticky="ew", pady=(0, 10))
            self.copy_button = ttk_module.Button(actions, text="复制调查指令（备用）", command=self._copy_instruction, style="Action.TButton")
            self.copy_button.pack(side="left")
            ttk_module.Button(actions, text="刷新结果", command=self._refresh_current_case, style="Action.TButton").pack(side="left", padx=10)
            ttk_module.Button(actions, text="新建另一个调查", command=self._new_another, style="Action.TButton").pack(side="left")
            self.continue_button = ttk_module.Button(actions, text="继续调查  ▶", command=self._continue_investigation, style="Primary.TButton", state="disabled")
            self.continue_button.pack(side="left", padx=(10, 0))
            self.cancel_button = ttk_module.Button(actions, text="停止 Codex  ■", command=self._cancel_investigation, style="Action.TButton", state="disabled")
            self.cancel_button.pack(side="left", padx=(8, 0))
            ttk_module.Button(actions, text="备份", command=self._backup_workspace, style="Action.TButton").pack(side="left", padx=(8, 0))
            ttk_module.Button(actions, text="恢复备份…", command=self._restore_backup_dialog, style="Action.TButton").pack(side="left", padx=(8, 0))
            ttk_module.Button(actions, text="导出诊断包", command=self._export_diagnostics, style="Action.TButton").pack(side="left", padx=(8, 0))
            ttk_module.Button(actions, text="⚙ 设置", command=self._open_settings, style="Ghost.TButton").pack(side="left", padx=(8, 0))
            self.submit_judgment_button = ttk_module.Button(actions, text="提交根因判断", command=self._open_judgment_dialog, style="Primary.TButton")
            self.reveal_button = ttk_module.Button(actions, text="揭晓真实修复", command=self._reveal_practice, style="Action.TButton")
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

        def show_new_page(self) -> None:
            self.new_page.tkraise()
            self.copy_status_var.set("")

        def show_practice_page(self) -> None:
            if self.busy:
                self.message_box.showwarning("任务仍在进行", "请先等待当前任务完成，或在正在运行的案件中点击“停止 Codex”。", parent=self)
                return
            try:
                self.practice_cases = self.practice_manager.list_cases()
            except BugCompassError as exc:
                self.message_box.showerror("无法读取练习案例", str(exc), parent=self)
                return
            self.practice_list.delete(0, "end")
            difficulty = {"easy": "入门", "medium": "进阶", "hard": "挑战"}
            for index, case in enumerate(self.practice_cases, 1):
                self.practice_list.insert("end", f"{index:02d}   #{case.issue_id}  {difficulty.get(case.difficulty, case.difficulty)}\n      {case.title}")
            self.practice_title_var.set("选择左侧案例")
            self.practice_meta_var.set("10 个真实修复 · 答案已隐藏")
            self.practice_symptom_var.set("选择一个案例，BugCompass 会创建隔离源码副本并切换到修复前版本。")
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
            difficulty = {"easy": "入门", "medium": "进阶", "hard": "挑战"}
            self.practice_meta_var.set(
                f"#{case.issue_id}  ·  {difficulty.get(case.difficulty, case.difficulty)}  ·  建议 {case.suggested_minutes} 分钟  ·  {case.suggested_ai_runs} 次 AI"
            )
            self.practice_title_var.set(case.title)
            self.practice_symptom_var.set(case.symptom)
            self.practice_steps_var.set("\n".join(f"{i}.  {step}" for i, step in enumerate(case.reproduction, 1)))
            self.practice_status_var.set(f"修复前 commit  {case.pre_fix_commit[:12]}  ·  真实修复保持隐藏")
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
                selected = self.file_dialog.askdirectory(title="选择 Blender 源码文件夹", mustexist=True)
                if not selected:
                    return
                validation = self.controller.validate_repository(selected)
                if not validation.valid:
                    self.message_box.showerror("Blender 源码无效", validation.message, parent=self)
                    return
                repo_to_initialize = str(validation.repo_path)
                self.repo_var.set(repo_to_initialize)
            self.busy = True
            self.practice_start_button.configure(state="disabled")
            self.practice_status_var.set("正在准备修复前源码副本……")
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
                    message = str(exc) if isinstance(exc, (BugCompassError, OSError)) else "创建历史练习时发生意外错误。"
                    self.events.put(("error", (view.case_id if view else None, message)))

            threading.Thread(target=worker, daemon=False).start()
            self.after(100, self._poll_events)

        def _choose_repo(self) -> None:
            selected = self.file_dialog.askdirectory(title="选择 Blender 源码文件夹", mustexist=True)
            if not selected:
                return
            validation = self.controller.validate_repository(selected)
            self.repo_var.set(str(validation.repo_path))
            self.repo_status_var.set(validation.message)
            self.repo_status_label.configure(style="Status.TLabel" if validation.valid else "Muted.TLabel")

        def _import_issue(self) -> None:
            selected = self.file_dialog.askopenfilename(
                title="导入 Bug 描述",
                filetypes=(("Markdown 或文本文件", "*.md *.txt"), ("Markdown 文件", "*.md"), ("文本文件", "*.txt")),
            )
            if not selected:
                return
            try:
                content = Path(selected).read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                self.message_box.showerror("无法导入文件", f"无法读取所选文件：\n{exc}", parent=self)
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
            self.char_count_var.set(f"{count} 个字符")

        def _start_create(self) -> None:
            if self.busy:
                return
            repo = self.repo_var.get().strip()
            bug_text = self.bug_text.get("1.0", "end-1c")
            if not repo:
                self.message_box.showwarning("请选择源码", "请先选择本地 Blender 源码文件夹。", parent=self)
                return
            if not bug_text.strip():
                self.message_box.showwarning("请填写 Bug 描述", "Bug 描述不能为空。", parent=self)
                return
            validation = self.controller.validate_repository(repo)
            if not validation.valid:
                self.message_box.showerror("Blender 源码无效", validation.message, parent=self)
                return

            self.busy = True
            runner = self._engine_runner()
            self._active_runner = runner
            runner.reset_cancellation()
            self.create_button.configure(state="disabled")
            self.progress_var.set("正在创建案件并准备调查引擎……")

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
                    message = str(exc) if isinstance(exc, (BugCompassError, OSError)) else "创建案件时发生意外错误。"
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
                    self._set_codex_state("working", "正在读取案件和 Blender 源码。")
                    self.result_hint_var.set("案件已创建，Codex 正在自动调查……")
                    self._refresh_recent_cases()
                elif kind == "practice_created":
                    self.codex_active = True
                    self.active_case_id = payload.case_id
                    self.current_case = payload
                    self.current_reveal = None
                    self._show_case(payload)
                    self._set_codex_state("working", "正在调查修复前版本，答案仍隐藏。")
                    self.result_hint_var.set("历史练习已开始：当前是修复前源码，真实答案保持隐藏。")
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
                            f"实验 {experiment_id} 执行完成（返回码 {record['return_code']}），正在请 Codex 更新假设。"
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
                        self.result_hint_var.set("Codex 调查完成，结果已自动刷新。")
                    elif self.current_case is not None:
                        self._show_case(self.controller.load_case(self.current_case.case_id))
                        self.copy_status_var.set(f"案件 {finished_case_id} 的调查已完成。")
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
                        self.result_hint_var.set("调查已取消；案件内容已保留，可以稍后重试。")
                    elif self.current_case is not None:
                        self._show_case(self.controller.load_case(self.current_case.case_id))
                        self.copy_status_var.set(f"案件 {finished_case_id} 的调查已停止。")
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
                                self.result_hint_var.set("Codex 调查未完成；案件已经保存在本地。")
                            elif failed_case_id:
                                self.copy_status_var.set(f"案件 {failed_case_id} 的操作失败。")
                        except BugCompassError:
                            pass
                    self._refresh_recent_cases()
                    self.message_box.showerror("操作未完成", str(message), parent=self)
            if self.busy:
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
                self.events.put(("error", (view.case_id, "Codex 已结束，但没有写入调查结果。案件已保留，请检查 Codex 登录状态后重试。")))
                return
            self.controller.set_case_status(view.case_id, "complete")
            self.events.put(("success", self.controller.load_case(view.case_id)))

        def _friendly_codex_error(self, result: CodexRunResult) -> str:
            if getattr(result, "engine", "") == "llm":
                # 大模型引擎的错误信息在生成时已是用户友好的中文指引。
                return result.error_detail or "大模型调查未完成。"
            detail = result.error_detail.lower()
            error_message = self.codex_runner.extract_error_message(result.error_detail)
            if result.timed_out:
                return (
                    f"Codex 在 {int(self.codex_runner.timeout_seconds)} 秒内没有完成，BugCompass 已自动停止它，避免继续消耗额度。"
                    "案件和已收集内容都已保留，可以稍后继续。"
                )
            if "invalid_json_schema" in detail:
                return "BugCompass 的结构化输出格式不兼容，Codex 尚未开始实际调查。案件已经保存在本地，修复格式后可点击“继续调查”。"
            if any(word in detail for word in ("login", "auth", "unauthorized", "credential")):
                return "Codex 尚未登录或登录已失效。请先在终端完成 Codex 登录，然后重试；案件已经保存在本地。"
            if any(word in detail for word in ("network", "connect", "dns", "timed out")):
                return "Codex 暂时无法连接服务。请检查网络后重试；案件已经保存在本地。"
            if error_message:
                return f"Codex 返回错误：{error_message[:500]}\n详细记录已保存到 Case 的 codex-last-run.json。"
            return f"Codex 调查未完成（退出码 {result.returncode}）。案件已经保存在本地，可以使用备用复制按钮继续。"

        def _cancel_investigation(self) -> None:
            if (
                not self.busy
                or not self.codex_active
                or self.current_case is None
                or self.current_case.case_id != self.active_case_id
            ):
                return
            self.result_hint_var.set("正在取消当前调查……")
            self._set_codex_state("stopping", "正在安全终止当前调查进程。")
            runner = self._active_runner or self._engine_runner()
            runner.cancel()

        def _continue_investigation(self) -> None:
            if self.busy or self.current_case is None:
                return
            try:
                view = self.controller.load_case(self.current_case.case_id)
                self.controller.set_case_status(view.case_id, "investigating")
            except BugCompassError as exc:
                self.message_box.showerror("无法继续调查", str(exc), parent=self)
                return
            self.current_case = view
            self.busy = True
            self.codex_active = True
            self.active_case_id = view.case_id
            runner = self._engine_runner()
            self._active_runner = runner
            runner.reset_cancellation()
            self._refresh_recent_cases()
            self._set_codex_state("working", "正在继续同一个案件，不会重新创建 Case。")
            self.result_hint_var.set("正在继续调查当前案件……")
            self._add_timeline("已继续当前案件，保留原有证据、预测和用户编辑。")

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
                    message = str(exc) if isinstance(exc, (BugCompassError, OSError)) else "继续调查时发生意外错误。"
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

        def _on_close(self) -> None:
            if self.codex_active:
                should_close = self.message_box.askyesno(
                    "调查仍在进行",
                    "关闭窗口会取消当前 Codex 调查。确定要关闭吗？",
                    parent=self,
                )
                if not should_close:
                    return
                self.codex_runner.cancel()
            elif self.busy:
                should_close = self.message_box.askyesno(
                    "实验仍在进行",
                    "本地实验仍在运行，当前不能用“停止 Codex”中止它。确定要关闭窗口吗？",
                    parent=self,
                )
                if not should_close:
                    return
            self.destroy()

        def _show_case(self, view: CaseView) -> None:
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
            status_names = {"ok": "正常", "warning": "有提醒", "error": "存在错误", "unknown": "未知"}
            capability_value = view.environment.get("checks", {}).get("capabilities", {}).get("value", {})
            capability_names = {"ok": "可以", "warning": "条件不足", "error": "不可用"}
            capabilities = (
                f"可调查：{capability_names.get(capability_value.get('investigate'), '未知')} · "
                f"可构建：{capability_names.get(capability_value.get('build'), '未知')} · "
                f"可运行测试：{capability_names.get(capability_value.get('test'), '未知')}"
            )
            self.result_summary_var.set(
                f"案件：{view.case_id}\n"
                f"Blender 仓库：{view.repo_path}\n"
                f"当前 commit：{view.short_commit}\n"
                f"环境状态：{status_names.get(view.environment_status, view.environment_status)}\n"
                f"{capabilities}\n"
                f"案件文件夹：{view.case_dir}"
            )
            self.result_hint_var.set("案件已经创建，下一步请让 Codex 调查它。" if view.awaiting_investigation else "已读取 Codex 调查结果。")
            if view.practice_session_id and self.current_reveal is None:
                self.result_hint_var.set("历史练习进行中 · 真实修复和答案仍然隐藏。")
            self._render_codex_state()
            self._render_investigation(view)
            self.details_text.configure(state="normal")
            self.details_text.delete("1.0", "end")
            self.details_text.insert("1.0", json.dumps(view.environment, ensure_ascii=False, indent=2))
            self.details_text.configure(state="disabled")
            if self.details_visible:
                self._hide_details()
            self.result_page.tkraise()

        def _render_investigation(self, view: CaseView) -> None:
            # 统一走滚动容器的 clear()：销毁旧控件 + 复位滚动，避免残影。
            self.cards_scroll.clear()
            data = view.investigation
            summary = data.get("summary", {})
            intro = ttk.LabelFrame(self.cards_host, text="问题整理", style="Dark.TLabelframe", padding=18)
            intro.pack(fill="x", pady=(0, 10))
            self._wrap_label(intro, text=summary.get("problem") or "案件已创建，Codex 正在整理问题。", style="Body.TLabel", justify="left", font=self._font("SF Pro Display", 14, "bold")).pack(anchor="w")
            self._render_metrics_card(view)
            self._render_causal_graph(data.get("causal_graph", {"nodes": [], "edges": []}))
            self._render_semantic_diff(data.get("semantic_diff", {}))
            if not data.get("hypotheses"):
                empty = ttk.Frame(self.cards_host, style="Surface.TFrame", padding=24)
                empty.pack(fill="x", pady=12)
                ttk.Label(empty, text="◌  正在等待调查路径", style="Heading.TLabel").pack(anchor="w")
                ttk.Label(empty, text="调查完成后，这里会出现三条有证据、可否定、可继续深入的路径。", style="Muted.TLabel").pack(anchor="w", pady=(5, 0))
            priority_names = {"high": "高", "medium": "中", "low": "低"}
            for index, item in enumerate(data.get("hypotheses", []), 1):
                card = ttk.LabelFrame(self.cards_host, text=f"路径 0{index}", style="Dark.TLabelframe", padding=18)
                card.pack(fill="x", pady=(0, 12))
                status = "（已否定）" if item.get("status") == "rejected" else ""
                title_row = ttk.Frame(card, style="Card.TFrame")
                title_row.pack(fill="x")
                ttk.Label(title_row, text=item.get("title", "未命名路径"), style="Heading.TLabel").pack(side="left")
                ttk.Label(title_row, text=f"  {priority_names.get(item.get('priority'), '未知')}优先级 {status}", style="Status.TLabel").pack(side="right")
                self._wrap_label(card, text=item.get("claim", ""), style="Body.TLabel", justify="left").pack(anchor="w", pady=(10, 12))
                ttk.Label(card, text="当前依据", style="Muted.TLabel", font=self._font("SF Pro Text", 9, "bold")).pack(anchor="w")
                for basis in item.get("basis", []):
                    self._wrap_label(card, text=f"•  {basis}", style="Body.TLabel", justify="left").pack(anchor="w", pady=1)
                for reference in item.get("source_references", []):
                    line = reference.get("line")
                    label = f"↗ {reference.get('path', '')}{':' + str(line) if line else ''}"
                    ttk.Button(card, text=label, command=lambda ref=reference: self._open_reference(ref), style="Ghost.TButton").pack(anchor="w", pady=2)
                next_box = ttk.Frame(card, style="Elevated.TFrame", padding=10)
                next_box.pack(fill="x", pady=(10, 8))
                self._wrap_label(next_box, text=f"下一步  →  {item.get('next_step', '')}", background=self.SURFACE_ALT, foreground=self.TEXT, justify="left", font=self._font("SF Pro Text", 10, "bold")).pack(anchor="w")
                buttons = ttk.Frame(card, style="Card.TFrame")
                buttons.pack(anchor="w", pady=(4, 0))
                ttk.Button(buttons, text="查看证据", command=lambda h=item: self._show_evidence(h), style="Action.TButton").pack(side="left")
                ttk.Button(buttons, text="深入调查  →", command=lambda h=item: self._run_followup("deepen", h.get("id")), style="Primary.TButton").pack(side="left", padx=8)
                ttk.Button(buttons, text="否定路径", command=lambda h=item: self._reject_path(h.get("id")), style="Ghost.TButton").pack(side="left")
                experiments = [e for e in data.get("suggested_experiments", []) if e.get("hypothesis_id") == item.get("id")]
                for experiment in experiments:
                    self._render_experiment_card(card, experiment)
            facts = [e.get("statement", "") for e in data.get("evidence", []) if e.get("kind") == "fact"]
            inferences = [e.get("statement", "") for e in data.get("evidence", []) if e.get("kind") == "inference"]
            unknowns = [u.get("question", "") for u in data.get("unknowns", [])]
            for title, values in (("事实", facts), ("推测", inferences), ("未知", unknowns)):
                box = ttk.LabelFrame(self.cards_host, text=title, style="Dark.TLabelframe", padding=14)
                box.pack(fill="x", pady=(0, 8))
                self._wrap_label(box, text="\n".join(f"•  {value}" for value in values) or "暂无", style="Body.TLabel", justify="left").pack(anchor="w")
            if self.current_reveal is not None:
                self._render_practice_reveal(self.current_reveal)
            # 内容与换行宽度最终同步（修复窄窗口/DPI 变化下长文本被裁切）。
            self.cards_scroll.refresh()

        def _render_causal_graph(self, graph: dict[str, Any]) -> None:
            panel = ttk.LabelFrame(self.cards_host, text="因果链 · AI 初稿，可编辑", style="Dark.TLabelframe", padding=14)
            panel.pack(fill="x", pady=(0, 10))
            title_row = ttk.Frame(panel, style="Card.TFrame")
            title_row.pack(fill="x", pady=(0, 8))
            ttk.Label(title_row, text="拖动卡片整理逻辑；从橙色圆点拖到另一张卡片连线；空白处拖动可平移；Ctrl+滚轮缩放。", style="Muted.TLabel").pack(side="left")
            buttons = ttk.Frame(title_row, style="Card.TFrame")
            buttons.pack(side="right")
            ttk.Button(buttons, text="＋ 添加节点", command=self._add_causal_node, style="Ghost.TButton").pack(side="left", padx=(6, 0))
            ttk.Button(buttons, text="⛶ 自动布局", command=self._auto_layout_causal, style="Ghost.TButton").pack(side="left", padx=(6, 0))
            ttk.Button(buttons, text="⊞ 适应内容", command=self._fit_causal_view, style="Ghost.TButton").pack(side="left", padx=(6, 0))
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
                ttk.Label(panel, text="Codex 完成调查后会在这里生成“触发条件 → 状态变化 → 可见故障”的因果链。", style="Body.TLabel", justify="left").pack(anchor="w", pady=8)
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
            legend = ttk.Frame(panel, style="Card.TFrame")
            legend.pack(fill="x", pady=(7, 0))
            ttk.Label(legend, text="实线＝事实   虚线＝推测/未知   双击节点就地改名   空白处框选多节点   Delete 删除选中   Ctrl+Z/Ctrl+Y 撤销重做   按住 Alt 拖动临时关闭网格吸附", style="Muted.TLabel").pack(side="left")

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
                self.message_box.showerror("无法保存因果链", str(exc), parent=self)

        def _on_mindmap_status(self, text: str) -> None:
            self.result_hint_var.set(text)

        def _add_causal_node(self) -> None:
            if self.current_case is None or self.mindmap is None:
                return
            label = self.simple_dialog.askstring("添加因果节点", "用一句话描述新的因果环节：", parent=self)
            if not label or not label.strip():
                return
            self.mindmap.add_node(label)

        def _render_semantic_diff(self, semantic: dict[str, Any]) -> None:
            status = semantic.get("status", "not_available")
            names = {"not_available": "尚无相关代码改动", "proposed": "预期语义变化", "observed": "已观察到的语义变化"}
            panel = ttk.LabelFrame(self.cards_host, text="语义 Diff · 行为规则变化", style="Dark.TLabelframe", padding=16)
            panel.pack(fill="x", pady=(0, 10))
            header = ttk.Frame(panel, style="Card.TFrame")
            header.pack(fill="x")
            ttk.Label(header, text=names.get(status, status), style="Status.TLabel").pack(side="left")
            ttk.Button(header, text="分析当前代码改动", command=lambda: self._run_followup("semantic_diff", "working-tree"), style="Action.TButton").pack(side="right")
            if status == "not_available":
                self._wrap_label(panel, text="当前还没有可解释的相关改动。代码发生变化后点击右侧按钮，Codex 会只读分析 git diff。", style="Muted.TLabel", justify="left").pack(anchor="w", pady=(8, 0))
                return
            self._wrap_label(panel, text=semantic.get("summary", ""), style="Body.TLabel", justify="left", font=self._font("SF Pro Display", 12, "bold")).pack(anchor="w", pady=(10, 8))
            rules = ttk.Frame(panel, style="Card.TFrame")
            rules.pack(fill="x")
            for title, value, column in (("旧规则", semantic.get("old_rule", ""), 0), ("新规则", semantic.get("new_rule", ""), 1)):
                box = ttk.Frame(rules, style="Elevated.TFrame", padding=11)
                box.grid(row=0, column=column, sticky="nsew", padx=(0, 5) if column == 0 else (5, 0))
                rules.columnconfigure(column, weight=1)
                ttk.Label(box, text=title, background=self.SURFACE_ALT, foreground=self.ORANGE, font=self._font("SF Pro Text", 9, "bold")).pack(anchor="w")
                self._wrap_label(box, text=value or "尚未明确", background=self.SURFACE_ALT, foreground=self.TEXT, justify="left").pack(anchor="w", pady=(4, 0))
            for title, key in (("改变的不变量", "changed_invariants"), ("受影响路径", "affected_paths"), ("剩余风险", "remaining_risks")):
                values = semantic.get(key, [])
                if values:
                    ttk.Label(panel, text=title, style="Muted.TLabel", font=self._font("SF Pro Text", 9, "bold")).pack(anchor="w", pady=(10, 2))
                    self._wrap_label(panel, text="\n".join(f"•  {value}" for value in values), style="Body.TLabel", justify="left").pack(anchor="w")
            references = semantic.get("source_references", [])
            if references:
                ttk.Label(panel, text="源码依据", style="Muted.TLabel", font=self._font("SF Pro Text", 9, "bold")).pack(anchor="w", pady=(10, 2))
                for reference in references:
                    line = reference.get("line")
                    label = f"↗ {reference.get('path', '')}{':' + str(line) if line else ''}"
                    ttk.Button(panel, text=label, command=lambda ref=reference: self._open_reference(ref), style="Ghost.TButton").pack(anchor="w")

        def _ask_experiment_prediction(self, experiment: dict[str, Any]) -> tuple[str, str] | None:
            dialog = tk.Toplevel(self)
            dialog.title("运行前预测")
            dialog.geometry("650x500")
            dialog.minsize(560, 440)
            dialog.configure(background=self.BACKGROUND)
            dialog.transient(self)
            dialog.grab_set()
            shell = ttk.Frame(dialog, style="App.TFrame", padding=24)
            shell.pack(fill="both", expand=True)
            ttk.Label(shell, text="PREDICT BEFORE RUN", style="Eyebrow.TLabel").pack(anchor="w")
            ttk.Label(shell, text="先预测，再看结果", style="Title.TLabel", font=self._font("SF Pro Display", 22, "bold")).pack(anchor="w", pady=(4, 4))
            ttk.Label(shell, text="预测会被锁定，实验完成后 BugCompass 会把它和实际输出对照，避免事后解释。", style="PageSubtitle.TLabel", wraplength=590, justify="left").pack(anchor="w", pady=(0, 14))
            choice_var = tk.StringVar()
            options = [
                experiment.get("expected_support") or "结果支持当前假设",
                experiment.get("expected_weakening") or "结果削弱当前假设",
                "会得到其他或无法判断的结果",
            ]
            for option in options:
                tk.Radiobutton(shell, text=option, variable=choice_var, value=option, background=self.BACKGROUND, foreground=self.TEXT, activebackground=self.BACKGROUND, activeforeground=self.ORANGE, selectcolor=self.SURFACE_ALT, anchor="w", justify="left", wraplength=570, font=self._font("SF Pro Text", 10)).pack(fill="x", pady=4)
            ttk.Label(shell, text="为什么这样预测？", style="PageSubtitle.TLabel", font=self._font("SF Pro Text", 10, "bold")).pack(anchor="w", pady=(14, 6))
            rationale = tk.Text(shell, height=5, wrap="word", background=self.SURFACE_ALT, foreground=self.TEXT, insertbackground=self.TEXT, relief="flat", highlightthickness=1, highlightbackground=self.BORDER, highlightcolor=self.ORANGE, padx=12, pady=10, font=self._font("SF Pro Text", 11))
            rationale.pack(fill="both", expand=True)
            result: list[tuple[str, str]] = []
            row = ttk.Frame(shell, style="App.TFrame")
            row.pack(fill="x", pady=(14, 0))

            def save() -> None:
                choice, reason = choice_var.get().strip(), rationale.get("1.0", "end-1c").strip()
                if not choice or not reason:
                    self.message_box.showwarning("预测还不完整", "请选择一个结果，并用一句话说明理由。", parent=dialog)
                    return
                result.append((choice, reason))
                dialog.destroy()

            ttk.Button(row, text="取消", command=dialog.destroy, style="Ghost.TButton").pack(side="right")
            ttk.Button(row, text="锁定预测并继续  →", command=save, style="Primary.TButton").pack(side="right", padx=(0, 8))
            dialog.wait_window()
            return result[0] if result else None

        def _open_judgment_dialog(self) -> None:
            if self.current_case is None or not self.current_case.practice_session_id:
                return
            dialog = tk.Toplevel(self)
            dialog.title("提交根因判断")
            dialog.geometry("640x430")
            dialog.minsize(540, 360)
            dialog.configure(background=self.BACKGROUND)
            dialog.transient(self)
            dialog.grab_set()
            shell = ttk.Frame(dialog, style="App.TFrame", padding=24)
            shell.pack(fill="both", expand=True)
            ttk.Label(shell, text="YOUR DIAGNOSIS", style="Eyebrow.TLabel").pack(anchor="w")
            ttk.Label(shell, text="提交你的根因判断", style="Title.TLabel", font=self._font("SF Pro Display", 22, "bold")).pack(anchor="w", pady=(4, 4))
            ttk.Label(shell, text="写清楚你认为哪里错了、为什么，以及最重要的证据。提交后才能揭晓真实修复。", style="PageSubtitle.TLabel", wraplength=570, justify="left").pack(anchor="w", pady=(0, 14))
            editor = tk.Text(shell, wrap="word", background=self.SURFACE_ALT, foreground=self.TEXT, insertbackground=self.TEXT, selectbackground="#654127", relief="flat", highlightthickness=1, highlightbackground=self.BORDER, highlightcolor=self.ORANGE, padx=14, pady=12, font=self._font("SF Pro Text", 11))
            editor.pack(fill="both", expand=True)
            row = ttk.Frame(shell, style="App.TFrame")
            row.pack(fill="x", pady=(14, 0))
            ttk.Button(row, text="取消", command=dialog.destroy, style="Ghost.TButton").pack(side="right")

            def submit() -> None:
                try:
                    self.practice_manager.submit_judgment(self.current_case.case_id, editor.get("1.0", "end-1c"))
                except BugCompassError as exc:
                    self.message_box.showerror("无法提交", str(exc), parent=dialog)
                    return
                dialog.destroy()
                self.reveal_button.configure(state="normal")
                self.result_hint_var.set("根因判断已锁定。现在可以揭晓真实修复。")

            ttk.Button(row, text="锁定判断  →", command=submit, style="Primary.TButton").pack(side="right", padx=(0, 8))
            editor.focus_set()

        def _reveal_practice(self) -> None:
            if self.current_case is None or not self.current_case.practice_session_id:
                return
            if not self.message_box.askyesno("揭晓真实修复", "揭晓后会显示真实根因、修复 commit 和评分，当前判断不能再伪装成未揭晓状态。继续吗？", parent=self):
                return
            try:
                self.current_reveal = self.practice_manager.reveal(self.current_case.case_id)
                self._show_case(self.controller.load_case(self.current_case.case_id))
                self.result_hint_var.set("真实修复已揭晓，评分已保存到本地练习会话。")
            except BugCompassError as exc:
                self.message_box.showerror("无法揭晓", str(exc), parent=self)

        def _render_practice_reveal(self, reveal: PracticeReveal) -> None:
            panel = ttk.LabelFrame(self.cards_host, text="真实修复 · 练习评分", style="Dark.TLabelframe", padding=20)
            panel.pack(fill="x", pady=(12, 8))
            score_row = ttk.Frame(panel, style="Card.TFrame")
            score_row.pack(fill="x")
            ttk.Label(score_row, text=f"{reveal.quality_total} / {reveal.quality_max}", style="Heading.TLabel", font=self._font("SF Pro Display", 28, "bold"), foreground=self.ORANGE).pack(side="left")
            ttk.Label(score_row, text=f"用时 {reveal.elapsed_seconds // 60} 分 {reveal.elapsed_seconds % 60} 秒  ·  AI 运行 {reveal.ai_runs} 次", style="Muted.TLabel").pack(side="right")
            labels = {
                "true_subsystem_in_top3": "前三路径命中真实子系统",
                "relevant_files_found": "找到真实相关文件",
                "valid_evidence": "引用有效证据",
                "revealing_experiment": "提出有效实验",
                "premature_lock_in": "避免过早锁定",
            }
            for key, label in labels.items():
                row = ttk.Frame(panel, style="Elevated.TFrame", padding=(10, 7))
                row.pack(fill="x", pady=3)
                ttk.Label(row, text=label, background=self.SURFACE_ALT, foreground=self.TEXT).pack(side="left")
                ttk.Label(row, text=f"{reveal.scores.get(key, 0)} / 2", background=self.SURFACE_ALT, foreground=self.ORANGE, font=self._font("SF Pro Text", 10, "bold")).pack(side="right")
            answer = reveal.answer
            ttk.Label(panel, text="你的判断", style="Muted.TLabel", font=self._font("SF Pro Text", 9, "bold")).pack(anchor="w", pady=(16, 4))
            self._wrap_label(panel, text=reveal.submission, style="Body.TLabel", justify="left").pack(anchor="w")
            ttk.Label(panel, text="真实根因", style="Muted.TLabel", font=self._font("SF Pro Text", 9, "bold")).pack(anchor="w", pady=(16, 4))
            self._wrap_label(panel, text=answer.get("root_cause", ""), style="Body.TLabel", justify="left").pack(anchor="w")
            ttk.Label(panel, text=f"修复 commit  {answer.get('fix_commit', '')[:12]}  ·  PR #{answer.get('pull_request', '')}", style="Status.TLabel").pack(anchor="w", pady=(12, 4))
            self._wrap_label(panel, text="真实相关文件\n" + "\n".join(f"•  {path}" for path in answer.get("relevant_files", [])), style="Body.TLabel", justify="left").pack(anchor="w", pady=(5, 0))

        def _add_timeline(self, message: str) -> None:
            self.timeline.insert("end", message)
            self.timeline.yview_moveto(1.0)

        def _open_reference(self, reference: dict[str, Any]) -> None:
            try:
                self.controller.open_source_reference(reference, self.current_case.repo_path if self.current_case else None)
            except BugCompassError as exc:
                self.message_box.showerror("无法打开源码", str(exc), parent=self)

        def _show_evidence(self, hypothesis: dict[str, Any]) -> None:
            if self.current_case is None:
                return
            wanted = set(hypothesis.get("evidence_ids", []))
            entries = [e for e in self.current_case.investigation.get("evidence", []) if e.get("id") in wanted]
            text = "\n\n".join(f"[{('事实' if e.get('kind') == 'fact' else '推测')}] {e.get('statement', '')}" for e in entries) or "这条路径还没有关联证据。"
            self.message_box.showinfo("路径证据", text, parent=self)

        def _reject_path(self, hypothesis_id: str | None) -> None:
            if not self.current_case or not hypothesis_id:
                return
            if not self.message_box.askyesno("否定调查路径", "确认将这条路径标记为已否定？后续 Codex 更新会保留这个决定。", parent=self):
                return
            try:
                self._show_case(self.controller.reject_path(self.current_case.case_id, hypothesis_id))
            except BugCompassError as exc:
                self.message_box.showerror("无法更新路径", str(exc), parent=self)

        def _render_experiment_card(self, parent: Any, experiment: dict[str, Any]) -> None:
            permission_names = {"green": "绿色 · 只读", "yellow": "黄色 · 构建/测试/运行", "red": "红色 · 会修改数据"}
            colors = {"green": "#25813B", "yellow": "#A46000", "red": "#B42318"}
            permission = experiment.get("permission", "red")
            frame = ttk.LabelFrame(parent, text=f"实验 {experiment.get('id', '')}", style="Dark.TLabelframe", padding=14)
            frame.pack(fill="x", pady=(12, 0))
            ttk.Label(frame, text=experiment.get("title", "未命名实验"), style="Heading.TLabel", font=self._font("SF Pro Display", 12, "bold")).pack(anchor="w")
            self._wrap_label(frame, text=f"目的  {experiment.get('purpose') or experiment.get('description', '')}", style="Body.TLabel", justify="left").pack(anchor="w", pady=(5, 8))
            command = experiment.get("command", [])
            command_box = ttk.Frame(frame, style="Elevated.TFrame", padding=10)
            command_box.pack(fill="x")
            self._wrap_label(command_box, text=f"$ {' '.join(command)}", background=self.SURFACE_ALT, foreground="#D6DAE2", font=self._font("SF Mono", 9), justify="left").pack(anchor="w")
            self._wrap_label(frame, text=f"{experiment.get('description', '')}\n工作目录  {experiment.get('cwd', '.')}  ·  预计 {experiment.get('estimated_seconds', '?')} 秒", style="Muted.TLabel", justify="left").pack(anchor="w", pady=8)
            ttk.Label(frame, text=f"●  {permission_names.get(permission, permission)}", background=self.SURFACE, foreground=colors.get(permission, colors["red"]), font=self._font("SF Pro Text", 10, "bold")).pack(anchor="w")
            if experiment.get("result"):
                self._wrap_label(frame, text=f"上次结果  {experiment.get('result')}\n影响  {experiment.get('effect', 'pending')}", style="Body.TLabel", justify="left").pack(anchor="w", pady=(8, 0))
            prediction = experiment.get("prediction", {})
            if prediction.get("predicted_at"):
                prediction_box = ttk.Frame(frame, style="Elevated.TFrame", padding=10)
                prediction_box.pack(fill="x", pady=(8, 0))
                self._wrap_label(prediction_box, text=f"你的运行前预测  {prediction.get('choice', '')}", background=self.SURFACE_ALT, foreground=self.ORANGE, font=self._font("SF Pro Text", 10, "bold"), justify="left").pack(anchor="w")
                self._wrap_label(prediction_box, text=prediction.get("rationale", ""), background=self.SURFACE_ALT, foreground=self.MUTED, justify="left").pack(anchor="w", pady=(3, 0))
                if experiment.get("prediction_comparison"):
                    self._wrap_label(prediction_box, text=f"结果对照  {experiment.get('prediction_comparison')}", background=self.SURFACE_ALT, foreground=self.TEXT, justify="left").pack(anchor="w", pady=(5, 0))
            ttk.Button(frame, text="运行实验  ▶", command=lambda e=experiment: self._approve_and_run_experiment(e), style="Action.TButton").pack(anchor="w", pady=(10, 0))

        def _approve_and_run_experiment(self, experiment: dict[str, Any]) -> None:
            if self.busy or self.current_case is None:
                return
            try:
                plan = self.controller.prepare_experiment(experiment, self.current_case.repo_path)
            except BugCompassError as exc:
                self.message_box.showerror("实验不可执行", str(exc), parent=self)
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
                    self.message_box.showerror("无法保存预测", str(exc), parent=self)
                    return
                self.result_hint_var.set("预测已锁定。实验结束后会自动对照实际结果。")
            detail = f"将执行：\n{plan.command_text}\n\n工作目录：\n{plan.cwd}\n\n预计耗时：{plan.estimated_seconds} 秒"
            if plan.permission == "yellow":
                approved = self.message_box.askyesno("确认黄色实验", detail + "\n\n该实验会构建、测试或启动 Blender。确认执行吗？", parent=self)
                if not approved:
                    return
            elif plan.permission == "red":
                answer = self.simple_dialog.askstring("明确授权红色实验", detail + "\n\n该操作可能修改源码、删除文件或影响 Git。请输入“明确授权”继续：", parent=self)
                if answer != "明确授权":
                    self.result_hint_var.set("红色实验未获得明确授权，未执行。")
                    return
            view = self.current_case
            self.busy = True
            self.codex_active = False
            self.active_case_id = None
            self._set_codex_state("idle", "正在运行本地实验，Codex 当前没有工作。")
            self.result_hint_var.set(f"正在运行实验 {plan.experiment_id}……")
            self._add_timeline(f"实验 {plan.experiment_id} 已获用户批准，正在执行。")
            def worker() -> None:
                try:
                    record = self.controller.run_experiment(view.case_id, plan)
                    self.events.put(("experiment_complete", (view, plan.experiment_id, record)))
                except Exception as exc:
                    message = str(exc) if isinstance(exc, (BugCompassError, OSError)) else "实验执行时发生意外错误。"
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
                self.message_box.showerror("无法开始调查", str(exc), parent=self)
                return
            self.busy = True
            self.codex_active = True
            self.active_case_id = view.case_id
            runner = self._engine_runner()
            self._active_runner = runner
            runner.reset_cancellation()
            self._refresh_recent_cases()
            self._set_codex_state("working", "正在读取新增证据并更新同一个案件。")
            if self.current_case is not None and self.current_case.case_id == view.case_id:
                self.result_hint_var.set("正在更新同一个案件……")
                self._add_timeline("深入调查已有案件，不会创建新答案。")
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
                    message = str(exc) if isinstance(exc, (BugCompassError, OSError)) else "继续调查时发生意外错误。"
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
            options = ["Codex CLI（默认）"]
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
            return "Codex CLI（默认）" if provider is None else provider.display_name

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

        def _on_engine_changed(self, _event: Any = None) -> None:
            selection = self.engine_var.get()
            provider = next(
                (item for item in self.llm_providers if item.display_name == selection),
                None,
            )
            self.settings["active_engine"] = provider.id if provider else "codex"
            save_settings(self.settings)
            name = provider.display_name if provider else "Codex CLI"
            self.progress_var.set(f"调查引擎已切换为 {name}（已保存）。")

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
            self.result_hint_var.set(f"界面缩放 {percent}%（已保存，重启后仍生效）")

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
            panel = ttk.LabelFrame(self.cards_host, text="运行指标 · 本地保存", style="Dark.TLabelframe", padding=14)
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
                ttk.Label(header, text="还没有 Codex 运行记录；运行后这里会显示模型、耗时、工具轮次与成本。", style="Muted.TLabel").pack(anchor="w")
                return
            totals = metrics.to_dict()["totals"]
            models = "、".join(metrics.models) or "未知"
            runs = len(metrics.runs)
            self._wrap_label(header, text=f"模型  {models}   ·   运行 {runs} 次", style="Heading.TLabel").pack(anchor="w")
            grid = ttk.Frame(panel, style="Card.TFrame")
            grid.pack(fill="x", pady=(8, 0))
            columns = (
                ("累计耗时", format_duration(totals["duration_seconds"])),
                ("对话轮次", str(totals["turns"])),
                ("工具调用", str(totals["tool_calls"])),
                ("输入 token", f"{totals['input_tokens']:,}（缓存 {totals['cached_input_tokens']:,}）"),
                ("输出 token", f"{totals['output_tokens']:,}"),
                ("估计成本", format_cost(totals["estimated_cost_usd"])),
            )
            for index, (name, value) in enumerate(columns):
                box = ttk.Frame(grid, style="Elevated.TFrame", padding=(10, 7))
                box.grid(row=index // 3, column=index % 3, sticky="ew", padx=4, pady=3)
                grid.columnconfigure(index % 3, weight=1)
                ttk.Label(box, text=name, background=self.SURFACE_ALT, foreground=self.MUTED, font=self._font("SF Pro Text", 9, "bold")).pack(anchor="w")
                ttk.Label(box, text=value, background=self.SURFACE_ALT, foreground=self.TEXT, font=self._font("SF Pro Text", 11, "bold")).pack(anchor="w", pady=(2, 0))
            telemetry_state = "已开启（仅计数，且需再手动导出才会离开本机）" if telemetry_enabled() else "未开启"
            self._wrap_label(
                panel,
                text=f"成本按 ~/.bugcompass/pricing.json 里你填写的单价估算；未配置的模型显示“未配置单价”，不会凭空估算。统计上报：{telemetry_state}。",
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

        # -------------------------------------------------------------- 备份
        def _backup_workspace(self) -> None:
            if self.current_case is None:
                self.message_box.showinfo("备份", "请先打开一个案件（备份会打包整个工作区）。", parent=self)
                return
            try:
                target = create_backup(self.current_case.workspace_path, note=f"GUI 导出 · v{__version__}")
            except (BackupError, OSError) as exc:
                self.message_box.showerror("备份失败", str(exc), parent=self)
                return
            if self.message_box.askyesno("备份完成", f"备份已保存：\n{target}\n\n是否打开所在文件夹？", parent=self):
                self._open_path_in_explorer(target)
            self.result_hint_var.set("工作区备份完成。")

        def _restore_backup_dialog(self) -> None:
            archive = self.file_dialog.askopenfilename(
                title="选择 BugCompass 备份文件",
                filetypes=[("BugCompass 备份", "*.zip"), ("所有文件", "*.*")],
                parent=self,
            )
            if not archive:
                return
            try:
                manifest = inspect_backup(archive)
            except (BackupError, OSError) as exc:
                self.message_box.showerror("备份无效", str(exc), parent=self)
                return
            count = manifest.get("file_count", "?")
            created = str(manifest.get("created_at", "未知"))[:19].replace("T", " ")
            version = manifest.get("app_version", "未知")
            target_dir = self.file_dialog.askdirectory(title=f"恢复到哪个文件夹？（备份：{count} 个文件 · v{version} · {created}）", parent=self)
            if not target_dir:
                return
            try:
                destination = restore_backup(archive, target_dir)
            except (BackupError, OSError) as exc:
                self.message_box.showerror("恢复失败", str(exc), parent=self)
                return
            migrations = ""
            self.message_box.showinfo(
                "恢复完成",
                f"工作区已恢复到：\n{destination}\n\n迁移记录见工作区内的 backup-manifest.json。{migrations}",
                parent=self,
            )

        def _export_diagnostics(self) -> None:
            include_case = self.message_box.askyesno(
                "诊断包内容",
                "诊断包包含：崩溃日志、环境摘要、设置（已脱敏）。\n\n是否额外包含当前案件的结构信息（只有计数与状态，不含问题描述、证据正文或源码）？",
                parent=self,
            )
            dest = self.file_dialog.askdirectory(title="诊断包保存到哪个文件夹？", parent=self)
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
                self.message_box.showerror("导出失败", f"无法生成诊断包：{exc}", parent=self)
                return
            if self.message_box.askyesno("导出完成", f"诊断包已保存（已自动脱敏并通过自检）：\n{target}\n\n是否打开所在文件夹？", parent=self):
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

        # -------------------------------------------------------------- 设置
        def _open_settings(self) -> None:
            dialog = tk.Toplevel(self)
            dialog.title("设置")
            dialog.geometry("620x560")
            dialog.minsize(560, 520)
            dialog.configure(background=self.BACKGROUND)
            dialog.transient(self)
            dialog.grab_set()
            shell = ttk.Frame(dialog, style="App.TFrame", padding=24)
            shell.pack(fill="both", expand=True)
            ttk.Label(shell, text="SETTINGS", style="Eyebrow.TLabel").pack(anchor="w")
            ttk.Label(shell, text="设置", style="Title.TLabel", font=self._font("SF Pro Display", 22, "bold")).pack(anchor="w", pady=(4, 10))
            ttk.Label(shell, text=f"BugCompass v{__version__} · 设置保存在本地（~/.bugcompass/settings.json）", style="Muted.TLabel").pack(anchor="w", pady=(0, 14))

            # 界面缩放
            zoom_box = ttk.Frame(shell, style="Surface.TFrame", padding=14)
            zoom_box.pack(fill="x")
            ttk.Label(zoom_box, text="界面缩放", style="Heading.TLabel").pack(anchor="w")
            ttk.Label(zoom_box, text=f"当前系统 DPI 缩放：约 {scale_percent(self)}%。使用 Ctrl + 滚轮 也可以随时调整。", style="Muted.TLabel").pack(anchor="w", pady=(3, 8))
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
            ttk.Label(llm_box, text="模型服务（API 调查引擎）", style="Heading.TLabel").pack(anchor="w")
            if self.llm_provider_error:
                ttk.Label(llm_box, text=f"配置读取失败：{self.llm_provider_error}", style="Status.TLabel", justify="left").pack(anchor="w", pady=(3, 6))
            ttk.Label(
                llm_box,
                text="密钥只从环境变量读取，永不写入文件、备份或诊断包。\n"
                     "providers.json 只保存端点、模型名和「密钥环境变量名」。",
                style="Muted.TLabel", justify="left",
            ).pack(anchor="w", pady=(3, 8))
            if self.llm_providers:
                provider_names = [provider.display_name for provider in self.llm_providers]
                llm_row = ttk.Frame(llm_box, style="Surface.TFrame")
                llm_row.pack(fill="x")
                self._llm_test_var = tk.StringVar(value=provider_names[0])
                ttk.Label(llm_row, text="服务", style="Muted.TLabel").pack(side="left", padx=(0, 6))
                provider_combo = tk.OptionMenu(llm_row, self._llm_test_var, *provider_names)
                provider_combo.configure(
                    background=self.SURFACE_ALT, foreground=self.TEXT,
                    activebackground=self.BORDER, activeforeground=self.TEXT,
                    highlightthickness=0, bd=0,
                )
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
                    self._llm_status_var.set(f"正在测试 {provider.display_name}……")

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
                    self._llm_status_var.set(f"已设为当前引擎：{provider.display_name}")

                ttk.Button(llm_row, text="测试连接", command=run_test, style="Action.TButton").pack(side="left", padx=(10, 0))
                ttk.Button(llm_row, text="设为当前引擎", command=use_provider, style="Action.TButton").pack(side="left", padx=(8, 0))
                chosen = chosen_provider()
                key_hint = f"密钥环境变量：{chosen.api_key_env}" if chosen.needs_key else "本地服务，无需密钥"
                ttk.Label(llm_box, text=key_hint, style="Muted.TLabel").pack(anchor="w", pady=(4, 2))
            else:
                ttk.Label(llm_box, text="没有可用服务：运行 bugcompass llm init-config 生成配置模板后重开设置。", style="Muted.TLabel", justify="left").pack(anchor="w")

            # 统计上报（默认关闭）
            tele_box = ttk.Frame(shell, style="Surface.TFrame", padding=14)
            tele_box.pack(fill="x", pady=(10, 0))
            ttk.Label(tele_box, text="统计上报", style="Heading.TLabel").pack(anchor="w")
            ttk.Label(
                tele_box,
                text="默认关闭。开启后也只会在本地记录聚合计数（模型、耗时、轮次、token、成本），\n"
                     "不会记录问题描述、证据、源码路径或文件内容；数据只有在“导出待发数据”后才可能离开本机。",
                style="Muted.TLabel", justify="left",
            ).pack(anchor="w", pady=(3, 8))
            tele_var = tk.BooleanVar(value=telemetry_enabled())
            tk.Checkbutton(
                tele_box, text="我了解并主动开启本地统计记录", variable=tele_var,
                background=self.SURFACE, foreground=self.TEXT,
                activebackground=self.SURFACE, activeforeground=self.TEXT,
                selectcolor=self.SURFACE_ALT, anchor="w",
            ).pack(anchor="w")
            pending_count = len(telemetry_pending())
            ttk.Label(tele_box, text=f"本地待发事件：{pending_count} 条", style="Muted.TLabel").pack(anchor="w", pady=(6, 4))
            tele_row = ttk.Frame(tele_box, style="Surface.TFrame")
            tele_row.pack(anchor="w")

            def export_tele() -> None:
                from .resources import user_root

                target = export_telemetry_pending(user_root())
                self.message_box.showinfo(
                    "导出待发数据",
                    f"已导出到：\n{target}\n\n请自行检查内容后再决定是否发送。" if target else "当前没有待发事件。",
                    parent=dialog,
                )

            def clear_tele() -> None:
                removed = clear_telemetry_pending()
                self.message_box.showinfo("清空待发数据", f"已清空 {removed} 条本地待发事件。", parent=dialog)

            ttk.Button(tele_row, text="导出待发数据", command=export_tele, style="Action.TButton").pack(side="left")
            ttk.Button(tele_row, text="清空待发数据", command=clear_tele, style="Ghost.TButton").pack(side="left", padx=(8, 0))

            # 数据目录
            data_row = ttk.Frame(shell, style="App.TFrame")
            data_row.pack(fill="x", pady=(14, 0))
            from .resources import logs_dir, user_root

            ttk.Label(data_row, text=f"数据目录：{user_root()}", style="Muted.TLabel").pack(side="left")
            ttk.Button(data_row, text="打开", command=lambda: self._open_path_in_explorer(logs_dir()), style="Ghost.TButton").pack(side="left", padx=(8, 0))

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
            ttk.Button(row, text="取消", command=dialog.destroy, style="Ghost.TButton").pack(side="right")
            ttk.Button(row, text="保存  →", command=save, style="Primary.TButton").pack(side="right", padx=(0, 8))

        def _refresh_current_case(self) -> None:
            if self.current_case is None:
                return
            try:
                self._show_case(self.controller.load_case(self.current_case.case_id))
                self.copy_status_var.set("结果已刷新。")
                self._refresh_recent_cases()
            except BugCompassError as exc:
                self.message_box.showerror("刷新失败", str(exc), parent=self)

        def _copy_instruction(self) -> None:
            if self.current_case is None:
                return
            instruction = self.controller.investigation_instruction(self.current_case.case_id)
            try:
                self.clipboard_clear()
                self.clipboard_append(instruction)
                self.update_idletasks()
            except tk.TclError as exc:
                self.message_box.showerror("复制失败", f"无法访问系统剪贴板：{exc}", parent=self)
                return
            self.copy_status_var.set("调查指令已复制，请粘贴到当前 BugCompass 项目的 Codex 新任务中。")

        def _new_another(self) -> None:
            if self.busy:
                self.message_box.showwarning("调查仍在进行", "请先等待调查完成，或在正在运行的案件中点击“停止 Codex”。", parent=self)
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
                self.details_button.configure(text="收起详细环境信息")
                self.details_visible = True

        def _hide_details(self) -> None:
            self.details_frame.grid_remove()
            self.details_button.configure(text="显示详细环境信息")
            self.details_visible = False

        def _refresh_recent_cases(self) -> None:
            self.recent_list.delete(0, "end")
            recent = self.controller.recent_cases()
            self.recent_case_ids = [item.case_id for item in recent]
            if not recent:
                self.recent_list.insert("end", "暂无案件")
                return
            status_names = {
                "new": "新建",
                "investigating": "调查中",
                "complete": "已完成",
                "failed": "失败",
                "cancelled": "已取消",
            }
            for item in recent:
                created = item.created_at.replace("T", " ").replace("Z", "")
                if self.codex_active and item.case_id == self.active_case_id:
                    status = "调查中"
                elif item.status == "investigating":
                    status = "上次中断"
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
                self.message_box.showerror("无法打开案件", str(exc), parent=self)

    try:
        app = BugCompassApp()
    except tk.TclError as exc:
        detail = str(exc).splitlines()[0] or "Tk 初始化失败"
        print(
            "错误：无法启动图形界面。Tkinter 模块已安装，但 Tk 运行环境或图形显示不可用。"
            f"请换用带完整 Tk 支持的 Python，并在本地图形桌面中启动。详情：{detail}"
        )
        return 2
    if codex_warning is not None:
        messagebox.showwarning("Codex CLI 未安装", codex_warning, parent=app)
    app.mainloop()
    return 0
