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

Compare two already captured, version-bound evidence bundles:

```bash
uv run python scripts/check_admission_parity.py \
  --baseline worker.json --candidate cli.json --output artifacts/parity.json
```

The JSON report is retained on both success and failure. The version mismatch
report contains no case results or partial pass count.
