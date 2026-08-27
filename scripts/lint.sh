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

# `templates/` and `runner/` are excluded from the three gates above on purpose:
# they run inside the training container, import openpi / torch (which this package
# does not install), and reformatting them would make diffing against the container
# side harder. That reasoning covers *style*. It does not cover *correctness*, and
# excluding them from style silently excluded them from correctness too.
#
# 2026-08-25: `_run_default` called `_save_norm_stats(...)`, which was never defined
# anywhere. The default training flow — the one a miner with no custom script runs —
# died on NameError every single time, and had done so unnoticed, because F821 was
# never pointed at that directory.
#
# So: style stays excluded, correctness does not. `--isolated` is what bypasses
# pyproject's extend-exclude; without it these paths are silently skipped and the
# command passes while checking nothing.
uv run ruff check --isolated --select F821,F811,F841 \
    src/openroboto/templates src/openroboto/runner
