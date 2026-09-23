from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .cases import create_case, show_case
from .doctor import run_doctor
from .workspace import BugCompassError, init_workspace


STATUS_LABELS = {"ok": "正常", "warning": "警告", "error": "错误"}


class ChineseArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: object, **kwargs: object) -> None:
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
        message = message.replace("invalid choice:", "无效选择：")
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: 错误：{message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = ChineseArgumentParser(prog="bugcompass", description="BugCompass 0.1：本地 Blender Bug 调查工作区")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}", help="显示版本并退出")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="初始化 Blender 调查工作区")
    init.add_argument("--workspace", required=True, help="要创建的工作区目录")
    init.add_argument("--repo", required=True, help="本地 Blender Git 仓库的绝对路径")
    init.set_defaults(handler=_handle_init)

    doctor = commands.add_parser("doctor", help="只读检查 Blender 仓库环境")
    doctor.add_argument("--workspace", required=True, help="BugCompass 工作区目录")
    doctor.set_defaults(handler=_handle_doctor)

    gui = commands.add_parser("gui", help="启动本地图形界面")
    gui.add_argument("--check", action="store_true", help="仅检查 Tkinter 和 GUI 模块，不打开窗口")
    gui.set_defaults(handler=_handle_gui)

    case = commands.add_parser("case", help="创建或查看 Case")
    case_commands = case.add_subparsers(dest="case_command", required=True)
    create = case_commands.add_parser("create", help="从本地 Markdown/TXT 创建 Case")
    create.add_argument("--workspace", required=True)
    create.add_argument("--id", required=True, dest="case_id")
    create.add_argument("--input", required=True, dest="input_path")
    create.set_defaults(handler=_handle_case_create)
    show = case_commands.add_parser("show", help="显示 Case 状态与文件路径")
    show.add_argument("--workspace", required=True)
    show.add_argument("--id", required=True, dest="case_id")
    show.set_defaults(handler=_handle_case_show)
    metrics = case_commands.add_parser("metrics", help="显示 Case 的模型/耗时/轮次/成本指标（本地）")
    metrics.add_argument("--workspace", required=True)
    metrics.add_argument("--id", required=True, dest="case_id")
    metrics.set_defaults(handler=_handle_case_metrics)

    backup = commands.add_parser("backup", help="把整个工作区（含全部 Case）打包成备份 zip")
    backup.add_argument("--workspace", required=True)
    backup.add_argument("--output", default=None, help="备份文件路径（默认保存到 ~/.bugcompass/backups/）")
    backup.set_defaults(handler=_handle_backup)

    restore = commands.add_parser("restore", help="从备份 zip 恢复工作区（自动执行格式迁移）")
    restore.add_argument("--archive", required=True)
    restore.add_argument("--dest", required=True)
    restore.add_argument("--overwrite", action="store_true", help="目标目录已存在且不为空时允许覆盖")
    restore.set_defaults(handler=_handle_restore)

    migrate = commands.add_parser("migrate", help="把旧版本工作区迁移到当前格式（幂等，可重复执行）")
    migrate.add_argument("--workspace", required=True)
    migrate.set_defaults(handler=_handle_migrate)

    diagnostics = commands.add_parser("diagnostics", help="导出脱敏诊断包（不含 API key 与源码）")
    diagnostics.add_argument("--dest", required=True)
    diagnostics.add_argument("--workspace", default=None, help="使用 --case 时需要提供工作区路径")
    diagnostics.add_argument("--case", default=None, help="可选：附带该 Case 的结构计数（不含正文）")
    diagnostics.set_defaults(handler=_handle_diagnostics)

    telemetry = commands.add_parser("telemetry", help="统计上报管理（默认关闭，只写本地）")
    telemetry.add_argument("--status", action="store_true", help="查看当前状态与本地待发事件")
    telemetry.add_argument("--enable", action="store_true", help="主动开启本地统计记录")
    telemetry.add_argument("--disable", action="store_true", help="关闭统计记录")
    telemetry.add_argument("--export", metavar="目录", help="把待发事件导出成 JSON 供检查")
    telemetry.add_argument("--clear", action="store_true", help="清空本地待发事件")
    telemetry.set_defaults(handler=_handle_telemetry)
    return parser


def _handle_init(args: argparse.Namespace) -> int:
    workspace = init_workspace(args.workspace, args.repo)
    print("工作区初始化完成。")
    print(f"工作区：{workspace.path}")
    print(f"Blender 仓库：{workspace.repo_path}")
    return 0


def _handle_doctor(args: argparse.Namespace) -> int:
    result = run_doctor(args.workspace)
    print("BugCompass 环境检查")
    for name, item in result["checks"].items():
        label = STATUS_LABELS[item["status"]]
        print(f"[{label}] {name}: {item['message']}")
    output = Path(args.workspace).expanduser().resolve() / "environment.json"
    print(f"结构化结果：{output}")
    return 1 if result["overall_status"] == "error" else 0


def _handle_gui(args: argparse.Namespace) -> int:
    from .gui import check_gui, run_gui

    if args.check:
        available, message = check_gui()
        stream = sys.stdout if available else sys.stderr
        print(("检查通过：" if available else "检查失败：") + message, file=stream)
        return 0 if available else 2
    return run_gui()


def _handle_case_create(args: argparse.Namespace) -> int:
    metadata = create_case(args.workspace, args.case_id, args.input_path)
    case_dir = Path(args.workspace).expanduser().resolve() / "cases" / metadata["id"]
    print(f"Case 已创建：{metadata['id']}")
    print(f"目录：{case_dir}")
    return 0


def _handle_case_show(args: argparse.Namespace) -> int:
    metadata, paths = show_case(args.workspace, args.case_id)
    print(f"Case：{metadata.get('id', args.case_id)}")
    print(f"状态：{metadata.get('status', 'unknown')}")
    print(f"创建时间：{metadata.get('created_at', 'unknown')}")
    print(f"仓库 commit：{metadata.get('repo_commit') or 'unknown'}")
    print("相关文件：")
    for name, path in paths.items():
        print(f"- {name}: {path}")
    return 0


def _handle_backup(args: argparse.Namespace) -> int:
    from .backup import create_backup

    target = create_backup(args.workspace, args.output)
    print("备份完成。")
    print(f"备份文件：{target}")
    return 0


def _handle_restore(args: argparse.Namespace) -> int:
    from .backup import restore_backup

    destination = restore_backup(args.archive, args.dest, overwrite=args.overwrite)
    print("恢复完成（已自动执行格式迁移）。")
    print(f"工作区：{destination}")
    return 0


def _handle_migrate(args: argparse.Namespace) -> int:
    from .backup import migrate_workspace

    applied = migrate_workspace(args.workspace)
    if not applied:
        print("工作区已是当前格式，无需迁移。")
    else:
        print(f"已应用 {len(applied)} 项迁移：")
        for item in applied:
            print(f"- {item}")
    return 0


def _handle_diagnostics(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .diagnostics import export_bundle

    case_dir = None
    if args.case:
        if not args.workspace:
            print("错误：使用 --case 时必须同时指定 --workspace。", file=sys.stderr)
            return 2
        case_dir = Path(args.workspace).expanduser().resolve() / "cases" / args.case
    target = export_bundle(args.dest, case_dir=case_dir, include_case_structure=case_dir is not None)
    print("诊断包已导出（已自动脱敏并通过自检，不含 API key 与源码）。")
    print(f"诊断包：{target}")
    return 0


def _handle_case_metrics(args: argparse.Namespace) -> int:
    from .metrics import collect_case_metrics, format_cost, format_duration, load_pricing, write_case_metrics
    from .workspace import load_workspace

    workspace = load_workspace(args.workspace)
    case_dir = workspace.path / "cases" / args.case_id
    if not (case_dir / "case.json").is_file():
        print(f"错误：找不到 Case：{args.case_id}", file=sys.stderr)
        return 2
    metrics = collect_case_metrics(case_dir, pricing=load_pricing(workspace.path))
    write_case_metrics(case_dir, metrics)
    totals = metrics.to_dict()["totals"]
    print(f"Case：{args.case_id}")
    print(f"模型：{'、'.join(metrics.models) or '暂无运行记录'}")
    print(f"运行次数：{len(metrics.runs)}")
    print(f"累计耗时：{format_duration(totals['duration_seconds'])}")
    print(f"对话轮次：{totals['turns']}    工具调用：{totals['tool_calls']}")
    print(f"输入 token：{totals['input_tokens']:,}（缓存 {totals['cached_input_tokens']:,}）    输出 token：{totals['output_tokens']:,}")
    print(f"估计成本：{format_cost(totals['estimated_cost_usd'])}（按 pricing.json 单价；未配置则不估算）")
    print("指标仅保存在本地：Case 目录 metrics.json")
    return 0


def _handle_telemetry(args: argparse.Namespace) -> int:
    from . import telemetry

    if args.enable:
        telemetry.set_enabled(True)
        print("统计记录已开启（仍只写本地；数据需手动导出后才会离开本机）。")
        return 0
    if args.disable:
        telemetry.set_enabled(False)
        print("统计记录已关闭。")
        return 0
    if args.export:
        target = telemetry.export_pending(args.export)
        if target is None:
            print("当前没有待发事件。")
        else:
            print(f"待发事件已导出：{target}")
            print("请自行检查内容后再决定是否发送。")
        return 0
    if args.clear:
        print(f"已清空 {telemetry.clear_pending()} 条本地待发事件。")
        return 0
    enabled = telemetry.is_enabled()
    print(f"统计上报：{'已开启（仅本地计数）' if enabled else '未开启（默认）'}")
    print(f"本地待发事件：{len(telemetry.pending())} 条（~/.bugcompass/telemetry-outbox/）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return int(args.handler(args))
    except BugCompassError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("操作已取消。", file=sys.stderr)
        return 130
