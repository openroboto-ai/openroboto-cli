#!/usr/bin/env bash
#
# Static-check gate. CI runs this very script — writing one command list on each side
# means testing two systems, and that is one of the sources of "green locally, red in
# CI".
#
# Each of the three tools covers one area, and none of them may be dropped:
#   mypy strict  type correctness (src only; reason in pyproject.toml's [tool.mypy])
#   ruff check   lint (including T20, which bans print; the command layer and the
#                templates have their own exemptions)
#   ruff format  formatting
#
# The paths are hardcoded as `src tests`, not `.`:
#   - the old files inherited from openroboto-subnet (rt.py / miner.py / utils/ ...)
#     are already in pyproject's extend-exclude, so `.` would not reach them anyway;
#   - but `.` would also hand the python code blocks inside docs/*.md to ruff format,
#     and docs/MINER.md was measured to go red immediately. Whether that document
#     stays is governed by the migration decision in SCOPE.md, and the formatting gate
#     must not make that call on its behalf.
#   The scope of the gate is the new structure itself: the code moved into src/, and
#   the tests/ guarding it.
#
# Everything is prefixed with `uv run`: `bash scripts/lint.sh` runs bare, without
# activating a venv.

# set -e: exit immediately when any tool fails, passing the exit code through
#         unchanged (CI only looks at the exit code).
# set -x: print the commands as they are actually executed, so when it goes red there
#         is no guessing which one it was.
set -e
set -x

uv run mypy src
uv run ruff check src tests
uv run ruff format --check src tests
