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
