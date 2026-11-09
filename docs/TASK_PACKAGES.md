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

## 8. Composed tabletop development candidate

The unpublished CLI 1.8.0 now consumes protocol 0.17.1, which retains the
controlled-v2 contract introduced in 0.15.0. Use the matching reviewed CPU task
runtime for v1/v2 authoring. Consumer index lockfiles cannot be
regenerated against an unpublished protocol version. Do not deploy this checkout
with a stale lockfile or present a source-path development run as a cold install.

```bash
openroboto task catalog --include-development --json
openroboto task create composed.json --profile panda-tabletop-v2
openroboto task build --definition composed.json --output composed-package \
  --task-id local-composed --runtime-python /path/to/cpu-env/bin/python
openroboto task record-reference composed-package --example \
  --runtime-python /path/to/cpu-env/bin/python
openroboto task self-test composed-package \
  --runtime-python /path/to/cpu-env/bin/python
```

`--example` is only a local one-cube recording example. It has unresolved
disturbance failures and is not a generally admitted solver. A failing self-test
must remain visible; do not remove seeds or loosen tolerances to pass it.

The generated definition names actual catalog assets and circular zones. Each
zone declares accepted asset specifications and capacity. A stage combines
`in_zone`, `pose`, `stable`, `released`, `supported_by`, `no_contact` and `matching`
using `all`, `any` or `not`. Held stage goals and earlier-stage dependencies are
explicit; `reject_early` rejects early satisfaction. Episode and active-stage
invariants run at every 2 ms physics step, with violations latched until reset.
Reference action order has no scoring meaning unless the authored stage program
explicitly requires it. Zone capacity constrains the named objects assigned by
`matching`; it is not a global occupancy invariant. Use explicit predicates and
invariants when another object must stay outside a zone or retain its state. Whole-shape containment and real upward supporting
contact replace fixed-cube approximations. Spherical pose targets are rejected
as unobservable under geometric symmetry.

`observation: "state"` exposes physical object truth. `"vision"` hides object
coordinates, truth state, private judge traces and diagnostics; it requires real
camera pixels and uses visual object/zone labels in instructions. The candidate
freezes a 1280 by 720 vision image and a 320 by 180 state-mode image; camera
calibration and dimensions are part of its digest. It does not
claim visual recognition quality until per-object visibility is independently
checked. Resource tiers bound development inputs and steps; they do not grant
API calls or establish a supported object-count limit. Both compared agents must
use the same frozen observation, tools, resource tier and execution contract.

This candidate implements controlled rigid primitives and convex meshes on
Panda. Full physical robustness, all supported combinations and remote dev
submission are pending. Importing arbitrary author-owned scenes/assets, robot
adapters or deformable simulation needs a separate validated capability path;
renaming a primitive or loading a mesh does not establish that capability.

## 9. Author-owned scenes (schema 3)

This is an unpublished development path. The protocol input is commit
`2b3c043ac1cffd146272d7818560f5e65bfa069c`, version `0.17.1`, task schema 3,
execution contract 3 and `panda-open-scene-v1@2.0.0` with profile digest
`052ede59310a10eecdaefb43584f06340bc3ad461dbca8579cdb23b8ec4ad3d5`.
The reviewed final protocol wheel SHA-256 is
`bc99d357c04c28a72ccca6a70bf5a1d5a938d9d797d216c285243a25f8c27761`.
This patch retains the schema, execution contract and profile identity while
validating frame ancestry in linear time.
Install that wheel and this CLI wheel together using normal resolution. The
registry lock is still the released baseline; an index-only install and
`uv sync --locked` are blocked until the authorized protocol release and lock
integration. Do not use a source-path override or `--no-deps` as install evidence.

Prepare a materialized execution directory containing the complete schema-3
`task.json`, root-relative MJCF, meshes, textures and includes. Use the protocol's
exported schema and imported-scene specification. Every transitive dependency
needs its actual size, SHA-256, direct `requires` edges, source, fixed source
revision and license. No dependency is downloaded by the CLI. Source URLs are
provenance, never runtime fetches. Object/part/joint/frame, robot/control,
environment, observation, resource, initial-state and checker bindings must all
be explicit. A primitive renamed as an imported object is not physical support.

Author `public_summary` explicitly in `task.json`. Do not put references, solver
text, raw checker diagnostics or credentials there. The CLI never derives it
from the private root or instruction. Keep local solver scripts and reports
outside both roots. Gated HF repositories still expose filenames, sizes and
hashes; choose neutral filenames. The CLI refuses `README.md` in scene inputs
because model-card content may bypass HF gating.

```bash
# These commands require only the lightweight CLI, not MuJoCo.
openroboto task catalog --include-development --json
openroboto task scene build --execution ./my-execution \
  --reference ./my-private-reference.json --output ./my-submission
openroboto task scene check ./my-submission
```

Alternatively pass `--private ./my-private-tree` instead of `--reference` to
retain multiple private materials. That input needs a protocol
`reference-binding.json` naming its schema-1 reference JSON files. Build copies
all files and rebinds the private reference to the measured execution bytes and
normalized execution-contract digest. It writes `submission.json` outside the
two roots, refuses existing output and retains failed builds. Build is
materialization and structural validation; native compilation belongs to the
same separately installed worker runtime used for execution.

Every regular file within each root, including stray files, counts in its
unchanged protocol `package_digest`. The detached `submission_digest` binds
execution digest E and private digest P. It never replaces the historical
meaning of `package_digest`. Edit either root only by rebuilding a new submission.
The check rejects missing/changed dependencies, undeclared XML resources, unsafe
paths, symlinks, resource overflow, invalid public metadata and stale bindings.
Its JSON says `scope=structure_only`, `local_physics=not_run`,
`server_admission=not_run`, `reward_status=not_evaluated`.

```bash
openroboto task scene record-reference ./my-submission \
  --script ./solve.py --runtime-python /path/to/runtime/bin/python \
  --output ./record-diagnostic.json
openroboto task scene self-test ./my-submission \
  --runtime-python /path/to/runtime/bin/python --output ./self-test-diagnostic.json
```

**Current limitation:** the fixed P01 importer `ed9dcc37` (following `05670d4c`)
provides native scene
import, not a schema-3 authoring/replay API with private reference output. These
two commands perform a bounded runtime metadata probe and then return nonzero
with `scene_authoring_unavailable`, `resource_unavailable` and all execution
gates `not_run`. They do not execute `solve.py`, run physics, produce references,
or call a model. Retain the report and install the future reviewed P02/P03 runtime
and its matching CLI bridge before treating recording/self-test as available.
Running the old v1 recorder on `execution/` would put private references in the
wrong root and is not a workaround.

Once the operator has authorized a scene season and verified the exact server
runtime route, the submission transport remains the existing chain/payment
pipeline:

```bash
openroboto submit --config miner.yaml --scene-package ./my-submission
openroboto task status --config miner.yaml
# Use the backend task id above, not task.json's author-defined task id.
openroboto task scene register BACKEND_TASK_ID --package ./my-submission \
  --config miner.yaml
openroboto task scene status BACKEND_TASK_ID --config miner.yaml
```

Before upload the CLI checks both local roots, the exact scene season, the
server's verified environment/robot/control/observation/resource route, and the
season's `params.hf.grant_to`. The existing manual gated access checks run before
any content is uploaded. Before payment it downloads the full immutable HF
commit, verifies both roots against the original identity, checks the live route
again and uses the existing contributor binding, deduplication, access, fee
confirmation and burn-window rules. Failed infrastructure checks do not become
author/physical rejection. Retry `submit` in the same workspace; uploaded content
and a paid checkpoint are reused. `--force` still pays again: it is not a retry.

Chain ingestion and scene registration are separate steps. Registration finds
the task through the wallet-signed author endpoint, downloads its exact
`repo@commit` and signs the backend's canonical registration message binding that
transport and all three digests. A MINER key is still required. Pending ingestion,
storage errors or registration retries never require another payment. Keep the
original local submission for this step.

Lookup scans authenticated author pages (100 rows per request, at most 100 pages
per invocation), matching only the explicit backend task id. An empty or final
page ends lookup. If the scan reaches its page cap, `task_lookup_limit` reports a
continuation `--lookup-offset N`; retry the same registration with that offset
without paying again. A changing listing or a genuinely missing id can be
retried from offset 0. The located row's exact repo/commit and both root digests
are still checked before registration; no id is inferred from the submission name.

The backend distributes roles: author,
assigned admission worker and reviewer can access both roots; assigned evaluation
workers receive only execution files; model/tool processes and public visitors
receive neither root. The CLI does not grant worker roles or claim process
isolation was verified locally.

Status reports the server's `current_capability`, physical `result.admission` and
`result.reward_status` separately. A local static check, successful upload,
registration, `supported` route, or `admitted` result is not itself reward
eligibility. Review and server evidence remain required. This candidate has no
new real scene physical success, server admission or eligible task evidence.

## Related documents

- [CONTRIBUTOR.md](./CONTRIBUTOR.md) — the identity a task submission is paid to
- [PAYMENT.md](./PAYMENT.md) — what the entry fee costs and when it is wasted
- [README.md](./README.md) — the rest of the command surface
