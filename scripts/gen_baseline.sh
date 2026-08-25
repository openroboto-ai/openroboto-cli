#!/usr/bin/env bash
#
# Regenerate `tests/fixtures/baseline/` from an older commit.
#
# The baseline is a **contract, not a temporary file**: it is the recorded
# behaviour of the four legacy commands, taken from a tree that predates the
# competition work and — this is the load-bearing half — from an environment
# where `openroboto-protocol` is still **0.6.0**.
#
#   tests/test_backward_compat.py compares today's bytes (0.7.0) with these
#   (0.6.0). Regenerate them on 0.7.0 and that comparison becomes 0.7.0 against
#   itself: green forever, guarding nothing, with no error to tell you.
#
# So this script refuses to write a baseline from a tree that resolves to
# anything other than 0.6.0, and `test_backward_compat.py` asserts the recorded
# version as well. Two locks on the same door because opening it silently is the
# failure mode.
#
# Usage:
#   git worktree add /tmp/or-baseline-0.6.0 <SHA that still pins 0.6.0>
#   (cd /tmp/or-baseline-0.6.0 && uv sync --locked)
#   bash scripts/gen_baseline.sh /tmp/or-baseline-0.6.0
#
# 🔴 Before regenerating, read the roll-back note in
# `.trellis/tasks/08-23-cli-backward-compat-test/design.md`: changing the
# baseline is redefining "what the old behaviour was", and it has to be its own
# commit that says why and which miners it affects.

set -e
set -u

WORKTREE="${1:?usage: gen_baseline.sh <path to a worktree of the older commit>}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXPECTED_PROTOCOL="0.6.0"

installed="$(cd "$WORKTREE" && uv run python -c \
  'import importlib.metadata as m; print(m.version("openroboto-protocol"))')"
if [ "$installed" != "$EXPECTED_PROTOCOL" ]; then
  echo "refusing: $WORKTREE resolves openroboto-protocol to $installed," \
       "expected $EXPECTED_PROTOCOL." >&2
  echo "A baseline recorded on the current pin compares that pin with itself." >&2
  exit 1
fi

# The harness lives in this repo, not in the old worktree — both sides have to
# run the *same* capture code, otherwise a difference in the harness shows up as
# a compatibility break.
cp "$REPO_ROOT/tests/baseline_capture.py" "$WORKTREE/tests/baseline_capture.py"
mkdir -p "$WORKTREE/tests/fixtures"
cp "$REPO_ROOT/tests/fixtures/miner_legacy.yaml" "$WORKTREE/tests/fixtures/"

(cd "$WORKTREE" && uv run python tests/baseline_capture.py \
  "$REPO_ROOT/tests/fixtures/baseline")

(cd "$WORKTREE" && git rev-parse HEAD) > "$REPO_ROOT/tests/fixtures/baseline/COMMIT"

rm -f "$WORKTREE/tests/baseline_capture.py" "$WORKTREE/tests/fixtures/miner_legacy.yaml"

echo "baseline written from $(cat "$REPO_ROOT/tests/fixtures/baseline/COMMIT")" \
     "on protocol $installed"
