#!/usr/bin/env python3
"""Windows 视觉回归检查助手：报告当前 DPI / 缩放状态，并生成人工核对清单。

在目标 Windows 机器上运行（安装版或源码版都可以）：

    python tools/visual_checklist.py            # 打印环境 + 写 checklist markdown
    python tools/visual_checklist.py --json     # 机器可读输出（给 CI 汇总用）

配合 docs/RELEASE-CHECKLIST.md 使用：每台机器一份，逐项人工打勾。
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from datetime import datetime
from pathlib import Path

CHECKLIST = """# 视觉回归人工检查单 · {machine} · {scale}%

生成时间：{timestamp}
机器：{machine}
系统：{system}
Python：{python}
DPI 模式：{dpi_mode}
系统缩放：约 {scale}%

在**本机当前 DPI 设置**下逐项检查；不通过就停下来记录截图与复现步骤。

## 1. 滚动（滚轮 / 拖动滚动条）
- [ ] 结果页长内容用滚轮上下滚动，卡片文字无残影（拖影）
- [ ] 直接拖动右侧滚动条滑块，滚动平滑、无错位断裂
- [ ] 鼠标停在卡片内部任何位置滚轮都能滚动页面
- [ ] 滚到最底部，最后一行完整可见（没有被裁掉半行）
- [ ] 滚回顶部，第一张卡片完整可见

## 2. 缩放
- [ ] Ctrl + 滚轮（页面区域）界面缩放生效，文字与按钮同步变化
- [ ] Ctrl + 滚轮（因果链区域）画布缩放，以鼠标位置为中心
- [ ] 缩放后文字仍清晰，无发虚（模糊 = DPI 声明未生效，报告 bug）
- [ ] 设置里调整界面缩放并保存，重启后仍生效

## 3. 长文本卡片
- [ ] 含超长中文/英文/路径的卡片：窗口拉窄后文字换行，无横向溢出
- [ ] 窗口从最小尺寸拖到最大化再拖回，卡片布局自适应、无重叠
- [ ] 案件概览里的路径显示完整换行

## 4. 因果链（思维导图）
- [ ] 拖动节点跟随指针，无残影、无闪烁
- [ ] 从橙色圆点拖出连线到另一张卡片可建立连线
- [ ] 双击节点就地改名；Delete 删除选中；Ctrl+Z 撤销
- [ ] 空白处拖动平移画布；自动布局/适应内容按钮工作

## 5. 整体
- [ ] 窗口最小尺寸 940x650 下无控件被裁切
- [ ] 关闭再重开应用，界面状态正常
- [ ] （可选）截图存档

结论：☐ 通过 ☐ 不通过（附截图/复现步骤）
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="BugCompass 视觉回归检查助手")
    parser.add_argument("--json", action="store_true", help="输出 JSON（供 CI 汇总）")
    args = parser.parse_args()

    info: dict[str, object] = {
        "machine": platform.node() or platform.machine(),
        "system": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "python": sys.version.split()[0],
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }

    # 尝试真实启动 GUI 层面探测（无显示器时退化为纯文本模式）
    dpi_mode = "unavailable"
    scale = "unknown"
    probe_error = ""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from bugcompass.dpi import apply_scaling, describe, enable_windows_dpi_awareness, scale_percent

        dpi_mode = enable_windows_dpi_awareness()
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        apply_scaling(root)
        scale = str(scale_percent(root))
        info.update(describe(root))
        root.destroy()
    except Exception as exc:  # 无显示环境（CI/远程）也能继续
        probe_error = str(exc)
        info["gui_probe_error"] = probe_error

    info.update({"dpi_mode": dpi_mode, "scale": scale})

    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    if probe_error:
        print("提示：当前环境无法打开 Tk 窗口，仅生成检查单（请在 Windows 图形会话中运行以探测 DPI）。")
    checklist = CHECKLIST.format(
        machine=info["machine"],
        system=info["system"],
        python=info["python"],
        timestamp=info["timestamp"],
        dpi_mode=dpi_mode if dpi_mode != "unavailable" else "未声明（非 Windows 或声明失败）",
        scale=scale if scale != "unknown" else "unknown",
    )
    target = Path.cwd() / f"visual-checklist-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    target.write_text(checklist, encoding="utf-8")
    print(checklist)
    print(f"检查单已写入：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
