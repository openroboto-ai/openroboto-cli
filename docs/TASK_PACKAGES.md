# Task packages — checking one before you pay

> **Status**: current · **Updated**: 2026-11-05 · **Audience**: miners entering a
> task-authoring season (`competitions.track = "task"`).

A task-authoring season does **not** take a model checkpoint. The on-chain
commitment names a Hugging Face repository at one commit, exactly as a model
submission does, but that repository holds a **task package**. This document is
about checking that package locally and understanding the digest the subnet
stores for it.

The package format itself (the `task.json` fields, the AXIS manifests, the
RoboDojo reference shape) belongs to the validator; see
`openroboto-validator` → `docs/task-package-format.md`. This document covers the
miner-facing commands and the one number that joins the two sides.

## 1. What the CLI does

```bash
openroboto task check ./my-task                 # local directory, offline
openroboto task check ./my-task --json          # machine-readable evidence
openroboto task check --repo user/pkg --commit <40-hex>
openroboto task check --repo user/pkg --commit <40-hex> --expect-digest <64-hex>

openroboto task status --probe                  # GET /healthz + /readyz only
openroboto task status --json                   # your own tasks (MINER key + signature)
openroboto task status --probe \
  --backend-url http://127.0.0.1:8091           # override backend.url for one call
```

`task status` reads the backend's `/healthz` and `/readyz` (`--probe`, no key
sent), or `GET /api/v1/tasks/mine` for the tasks the configured hotkey
submitted. Its JSON output includes the `meta.request_id` of the task query, so
the line can be quoted in an evidence record and matched against backend
logs.

`--run-id <id>` records an operator run id in that output. It is **not sent on
the wire**: the miner task endpoints have no `run_id` field or header, and the
backend rejects unknown query parameters with `400 UNKNOWN_QUERY_PARAM` rather
than ignoring them. The run id is for joining the CLI evidence with the
A/B budget/shared-run files, not part of the task request. The latter needs **two independent proofs**, and they fail
differently:

- the **MINER-level key** in `X-API-Key`, from `backend.public_key` (or the
  `public_key` published in `control.json`);
- the **hotkey signature** over the exact text from
  `GET /api/v1/tasks/access-message`.

🔴 **Never put the ADMIN_KEY in a miner workspace.** It configures seasons,
fees and settlements; the CLI has no field for it and never reads one. A
`UNAUTHORIZED` answer means the MINER key is missing; `INVALID_SIGNATURE`
means the wallet's hotkey is not the one being queried.

A minimal config for an isolated backend (the key comes from the operator's
`0600` env file, never from this repository):

```yaml
backend:
  url: http://127.0.0.1:8091
  public_key: <MINER-level key>
subnet:
  hotkey_ss58: <the hotkey in the wallet this config names>
```

When the backend is ready, run the probe first; it touches no key, no chain and
no task row. A `readyz: NOT ready` line names whether the database or the
migration head is the failing half.

`task check` prints:

- the **SHA-256 of every file**, sorted by path, so two packages can be diffed
  file by file;
- the **package digest** computed from those files;
- structural findings for the declared source.

`--repo/--commit` downloads exactly that revision and judges what the validator
will read. A branch or tag is refused: the identity is `repo@commit`.

`--expect-digest` compares the computed value with a digest the subnet reported
(for example a validator's `package_digest`). A mismatch is exit code 1.

The command validates the **format**: required manifests, instance references,
and the canonical hashes the manifests declare. It does not claim the task is
solvable, deterministic or not a freebie — those are the validator's runtime
admission results, not something a static check can answer.

## 2. The digest rule

> The implementation is `openroboto_protocol.package_digest`, introduced in
> protocol 0.13.0. The CLI has no local mirror. Protocol 0.14.0 adds the
> restricted authoring contract without changing this digest or the on-chain
> format. See the release requirement below for the separately installed runtime.

The miner, the admission worker and the backend must compute the same value
byte for byte:

1. walk **every regular file** in the package directory, recursively, with no
   whitelist and no exclusions;
2. sort paths **as paths** (not as strings);
3. per file, `sha256(read_bytes())`;
4. digest the UTF-8 bytes of `"\n".join(f"{path}\0{file_sha256}")`.

Three consequences worth knowing:

- Adding any file — a `run_info.json`, a `.DS_Store`, a stray backup — changes
  the digest. `openroboto submit` therefore uploads a task package exactly as
  written and does not inject a run-info file.
- Renaming the directory does not change the digest. The identity is the
  content, not the directory name.
- The backend does not recompute this value. The validator reports
  `package_digest` on `POST /api/v1/task/admit`, and the backend stores the
  reported value in `tasks.package_digest` (a second report with a different
  digest is a `PACKAGE_DIGEST_MISMATCH`).

### Reference solutions are required for runtime admission

A package may omit `reference_solution` and still pass the **static** format
check. It is not a complete submission: the validator's runtime admission has
no trajectory to replay and rejects it as `reject_unsolvable` (OPE-145 measured
this on an official-payload package that passed every static check).

`task check` emits `axis.reference_not_declared` as a warning in that case. If
a reference is declared, it must carry a non-empty `actions` list for every
instance; a `final_state`-only file is an error
(`axis.reference_actions_missing`).

Adding a reference changes the package digest. Build a **new** package
identity/digest for it; never edit an old frozen digest to make it match. The
builder accepts a real reference with `--reference <reference.json>` and
refuses one whose instances lack actions.

## 3. Submitting

```bash
openroboto init my-task-workspace --include-task
cd my-task-workspace
# put the package in the output directory (or pass --output-dir to submit)
openroboto task check <package>
openroboto submit
```

For a task season `submit`:

- skips the model layout and weights-fingerprint gates;
- downloads the repository at the commit that will go on chain, validates it,
  and refuses if the package's declared `source` differs from the season's
  `params.task.source` (the validator would use the wrong checker vocabulary);
- prints the package digest it computed, which is the value admission will
  report and the backend will store.

It then pays the entry fee and announces the commitment as usual. Runtime
admission happens after the commitment is scanned; a package that passes this
check can still be rejected as unsolvable, non-deterministic or a freebie.

One runtime-fatal shape is refused **before** the fee: an AXIS package with no
reference solution actions. OPE-145 measured that such a package passes every
static check and is rejected by the runtime as `reject_unsolvable`; paying for
it would buy a rejection. Supply `reference.json` with non-empty per-instance
`actions`, rebuild (the digest changes), and submit again.

A task workspace must set `huggingface.repo_id`. There is no honest default:
`pi05-<hotkey>` is a model name, and the repository holds a task package.

## 4. Official source material

The validator repository ships the frozen official AXIS release bundle
(`configs/benchmarks/axis_v2.0.tar.zst`) with its release receipt and payloads.
A per-task package derived from that bundle keeps the official `mjcf_xml`,
checker configuration, initial states and payload hashes; only the package
loader key `instance` is added to each randomization variant. Do not substitute
a self-made scene for the official one: it may pass this command while testing
something nobody evaluates.

The CLI repository ships a reproducible builder for that conversion:

```bash
python scripts/build_task_package.py \
  --release <validator>/configs/benchmarks/axis_v2.0.tar.zst \
  --receipt <validator>/configs/benchmarks/axis_v2.0-release.json \
  --task-id 22 --out /tmp/axis-22
openroboto task check /tmp/axis-22
```

It verifies the archive against the receipt, copies payloads verbatim, and adds
only the loader's `instance` ordinal. The resulting package is accepted by the
validator's static admission (`checker_registered`, `axis.payload_hashes`,
`axis.manifest_hashes`, instance count); solvable / not-a-freebie remain runtime
checks.

RoboDojo packages reference the official task at the validator's pinned commit
(`task.json.robodojo_ref`); they contain no geometry of their own. Runtime
requires Isaac Sim 5.1 and an A100.

## 5. Requirement on the protocol release

Protocol 0.13.0 is the published baseline for task commitments and shared
package digests. The new restricted authoring commands require protocol 0.14.0.
Protocol 0.14.0 is published on PyPI and this checkout has a real registry lock.
CLI 1.7.0 and the separate simulator runtime remain review candidates. Installing
the CLI candidate wheel resolves the exact published protocol normally. No local
protocol copy, invented registry entry or editable dependency override is used.

## 6. Normal-install verification

A `PYTHONPATH` splice is component verification, not proof that a miner can
`pip install` and use the task commands. Once a candidate protocol wheel exists,
verify the real install shape in a clean venv:

```bash
python scripts/verify_task_cli_install.py \
  --protocol-wheel /path/to/openroboto_protocol-<version>-py3-none-any.whl \
  --package /path/to/task/package \
  --expect-digest <64-hex> \
  --config <miner.yaml> \
  --status-config <miner.yaml-with-wallet> \
  --run-id <operator-run-id>
```

The script copies the working tree to a temporary directory, rewrites the
protocol pin **only in that copy** to the candidate wheel's version, builds the
CLI wheel, creates a fresh venv, installs both wheels with normal dependency
resolution, and runs the installed `openroboto` binary without `PYTHONPATH`.
It checks `Track.TASK`, `AXIS_LAYOUT`, the package digest, the live `/healthz`
+ `/readyz` probe, and (with `--status-config`) a signed `tasks/mine` read.

`--shared-module` / `--shared-function` (default
`openroboto_protocol.package_digest.package_digest`) compare the CLI digest
with the shared implementation. The verifier is **fail-closed**: a missing
module or function, a non-zero probe, malformed output, a non-64-hex value,
`digest_source` other than `openroboto-protocol`, or a digest mismatch all exit
non-zero. There is no fail-open diagnostic mode; a probe pointed at a missing
function must be red.

The simulator runtime candidate is not a published release. Review it as an
explicit local wheel, with normal dependency resolution; installing the CLI
alone does not install the simulator.

## 7. Create a task using the restricted tabletop scheme

These commands are a local authoring flow, not a task submission. They do not
upload, pay, call a model or grant an admission result themselves:

```bash
openroboto task catalog --json
openroboto task create task-definition.json
# Edit positions and goals within the catalog's ranges.
openroboto task build --definition task-definition.json --output ./my-task \
  --runtime-python /path/to/task-runtime/bin/python
openroboto task record-reference ./my-task --example \
  --runtime-python /path/to/task-runtime/bin/python
openroboto task self-test ./my-task --output ./self-test.json \
  --runtime-python /path/to/task-runtime/bin/python
openroboto task check ./my-task --json
```

The lightweight CLI does not install a simulator. Install the separately
versioned `openroboto-task-runtime` wheel in a dedicated environment and select
its Python with `--runtime-python`. A missing or failed runtime is an error;
there is no mock or fallback runtime. The runtime wheel must be published before
this becomes an index-only public runtime installation path.

The initial scheme uses one Franka Panda arm, one or two fixed rigid cubes and
round target areas on a fixed tabletop. `task catalog` publishes the exact
object sizes, mass, position ranges, success conditions and fixed runtime
reference. Authors may change only the listed fields. An arbitrary mesh,
robot, gravity, object mass or checker is not accepted by this scheme.

`--example` is a verified local one-cube push demonstration, not a general
solver. For other tasks, record the author's own local reference:

```bash
openroboto task record-reference ./my-task --script ./solve.py \
  --runtime-python /path/to/task-runtime/bin/python
```

`solve.py` defines `solve(runtime, task, instance)`. It runs explicitly on the
author's machine. Keep it outside the task package; remote workers only replay
the resulting action JSON. Keep self-test reports outside too: every package
file affects the digest. Build outputs and reports never overwrite existing
files. `task check` checks format and shared budget rules; only a successful
physical self-test demonstrates the reference can run. The official admission
worker must independently repeat its own checks after submission.

A new definition freezes `execution_profile` (id, version and digest) and a
restricted `task_definition`. The CLI, runtime and backend use the same shared
validation. If a season requires that profile, omitting it cannot fall back to
a legacy task. Existing task bytes and digests are never rewritten.

Public display is opt-in: pass `--public-summary public.json` to `task create`
or `task build`. The synopsis must explicitly declare title, instruction,
robot, execution_profile and origin (`original`, `derived` or `imported`).
It is intended to be public. Omitting it leaves no public synopsis; old private
instructions are not copied into it. Start from the exact reference returned
by `task catalog`, rather than typing a digest by hand.

When local validation is complete, reuse the existing task workspace,
Hugging Face upload and `openroboto submit` flow. The live season's fee and
required profile are rechecked before payment. Static validity or an example
reference is not a promise of discrimination, author emissions or launch
readiness.

## Related documents

- [CONTRIBUTOR.md](./CONTRIBUTOR.md) — the identity a task submission is paid to
- [PAYMENT.md](./PAYMENT.md) — what the entry fee costs and when it is wasted
- [README.md](./README.md) — the rest of the command surface
