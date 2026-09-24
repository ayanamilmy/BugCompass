#!/bin/sh
# BugCompass 任务同步协议（规则见 docs/COLLABORATION.md）
#
#   tools/task.sh start <分支名>        从最新 main 开新分支
#   tools/task.sh sync                  当前分支变基到最新 main
#   tools/task.sh finish "提交说明"     测试全绿 → commit → push
#   tools/task.sh pr "PR 标题"          创建 PR（或给出网页链接）
#
# 铁律：main 不直接提交；测试不绿不推送。

set -e

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

say() { printf '\033[1;32m[task]\033[0m %s\n' "$1"; }
die() { printf '\033[1;31m[task] 错误：\033[0m %s\n' "$1" >&2; exit 1; }

require_clean() {
    [ -z "$(git status --porcelain)" ] || die "工作区有未提交改动，请先 commit 或 stash。"
}

run_tests() {
    say "运行固定测试集……"
    if python3 -m pytest -q >/tmp/task-tests.log 2>&1; then
        tail -1 /tmp/task-tests.log
        return 0
    fi
    # pytest 不可用时退回 unittest
    if PYTHONPATH=src python3 -m unittest discover -s tests >/tmp/task-tests.log 2>&1; then
        tail -2 /tmp/task-tests.log
        return 0
    fi
    echo "------ 测试输出（最后 25 行） ------"
    tail -25 /tmp/task-tests.log
    die "测试未通过，拒绝推送。请修复后再执行 finish。"
}

case "${1:-}" in
    start)
        [ -n "${2:-}" ] || die "用法：tools/task.sh start <分支名>（如 codex/修滚动）"
        require_clean
        say "拉取最新 main……"
        git fetch origin
        git checkout -b "$2" origin/main
        say "已在新分支 $2 上（基于最新 main）。开工吧。"
        ;;
    sync)
        say "同步：变基到最新 main……"
        git fetch origin
        git rebase origin/main
        say "已同步。"
        ;;
    finish)
        [ -n "${2:-}" ] || die "用法：tools/task.sh finish \"提交说明\""
        require_clean
        [ -n "$(git log origin/main..HEAD --oneline 2>/dev/null)" ] \
            || die "当前分支没有领先 main 的提交，没有可推送的内容。"
        run_tests
        BRANCH=$(git rev-parse --abbrev-ref HEAD)
        [ "$BRANCH" = "main" ] && die "在 main 上，拒绝直接推送。请用 start 开分支。"
        git push -u origin "$BRANCH"
        say "已推送 $BRANCH。运行 tools/task.sh pr \"标题\" 创建 PR。"
        ;;
    pr)
        [ -n "${2:-}" ] || die "用法：tools/task.sh pr \"PR 标题\""
        BRANCH=$(git rev-parse --abbrev-ref HEAD)
        [ "$BRANCH" = "main" ] && die "在 main 上没有 PR 可建。"
        git fetch origin
        git push -u origin "$BRANCH" >/dev/null 2>&1 || true
        if command -v gh >/dev/null 2>&1; then
            gh pr create --base main --head "$BRANCH" --title "$2" \
                && say "PR 已创建。"
        else
            say "没装 gh，请打开下面的链接手动开 PR："
            printf '    https://github.com/ayanamilmy/BugCompass/compare/main...%s\n' "$BRANCH"
        fi
        ;;
    *)
        sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
        die "未知命令：${1:-}（可用：start / sync / finish / pr）"
        ;;
esac
