# Pinned admission contract fixtures

`admission-contract.tar.gz` is the deterministic export of the validator-owned
synthetic contract suite. Its exact bytes and source identity are recorded in
`contract.json`. This directory does not carry another fixture generator or a
copy of admission/checker rules.

The CLI-side loader verifies the archive SHA before extraction, loads the worker
from the official runtime wheel already bundled with the CLI, and compares the
actual worker/protocol source and fixture hashes with both the lock and the
baseline. This works without a private validator checkout, registry credentials,
or MuJoCo. Any version difference stops before fixture comparisons.

The comparator preserves `accepted`, `code`, each check's name/passed/kind/detail,
package identity and exit status. Differences name the case, check and field;
there is no numerical tolerance, omitted check, or semantic path rewriting.
Only the producer's documented package locator normalization is accepted.

The worker's structural `admit` entry accepts the missing-reference case while
leaving `solvable` and `not_freebie` failed with `kind=runtime`. That is a pending
runtime decision, not a successful full admission. It must remain visible in
regression results rather than being rewritten into a rejection or success.

Run the fixed suite through the real CLI parser and structural handler:

```bash
uv sync --locked
uv run python scripts/capture_cli_admission.py \
  --output-dir artifacts/admission-parity
```

The evidence directory must be new. `--python /absolute/path/to/python` selects a
candidate CLI installation when comparing another wheel or local checkout; that
interpreter also supplies the actual worker wheel and protocol source identity.
The runner verifies the pinned archive, baseline, lock and runtime code before
starting any case. A mismatch returns a version error with no case pass counts.

Each case calls the real `openroboto check --structural-only --json` entry. This
maintainer-only stage projects the raw worker structural verdict and always
returns `scope=structural-only` and `ready_to_submit=false`. The runner checks
these fields, refuses submit receipts, and verifies that the package digest
stays unchanged. This stage is separate from a miner's full `openroboto check`.

Only `TaskRuntime`'s external transport is substituted: it runs the bundled real
`task_source.cli admit` process with the fixed synthetic fixture source/config.
The transport accepts only these read-only cases and structural requests. It
does not mock the CLI handler, admission rules or checker verdicts. This isolates
verdict comparison from Docker, season execution-profile routing, reference
recording and physical replay; those paths are not covered by this regression.

The `Admission verdict parity` GitHub workflow runs this command for main pushes
and pull requests. It uses no private checkout or registry token and retains
identity, expected/actual verdicts, field differences and raw CLI/worker output
for 14 days, including failed runs. A change to the pinned suite or admission
source must update the exported artifact and explicit identity lock together.

Compare two already captured, version-bound evidence bundles:

```bash
uv run python scripts/check_admission_parity.py \
  --baseline worker.json --candidate cli.json --output artifacts/parity.json
```

The JSON report is retained on both success and failure. The version mismatch
report contains no case results or partial pass count.
