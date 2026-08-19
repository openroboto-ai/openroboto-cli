#!/usr/bin/env bash
#
# 静态检查门禁。CI 跑的就是这个脚本 —— 两边各写一份命令列表，
# 测的就是两个系统，本地绿 CI 红的来源之一。
#
# 三个工具各管一段，都不能删：
#   mypy strict  类型正确性（只查 src，理由见 pyproject.toml 的 [tool.mypy]）
#   ruff check   lint（含 T20 禁 print，命令层与模板另有豁免）
#   ruff format  格式
#
# 路径写死 `src tests`，不写 `.`：
#   - 从 openroboto-subnet 继承来的旧文件（rt.py / miner.py / utils/ …）已经在
#     pyproject 的 extend-exclude 里，写 `.` 也扫不到；
#   - 但 `.` 会把 docs/*.md 里的 python 代码块也交给 ruff format，
#     实测 docs/MINER.md 当场红。那份文档的去留归 SCOPE.md 的迁移决定管，
#     不该由格式门禁替它拍板。
#   门禁的范围就是新结构本身：搬进 src/ 的代码，和守着它的 tests/。
#
# 一律带 `uv run` 前缀：不激活 venv 也能裸跑 `bash scripts/lint.sh`。

# set -e：任一工具失败立刻退出，退出码原样传出去（CI 只看退出码）。
# set -x：把真正执行的命令打出来，红的时候不用猜是哪一条。
set -e
set -x

uv run mypy src
uv run ruff check src tests
uv run ruff format --check src tests
