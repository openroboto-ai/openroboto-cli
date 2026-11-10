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

This checkout declares CLI **1.8.0** and exactly **openroboto-protocol==0.18.0**.
Protocol 0.18.0 is published on official PyPI. The normally generated `uv.lock`
names CLI 1.8.0, protocol 0.18.0 and the same exact metadata requirement, with the
actual published URLs and hashes. The older 1.7.2/0.14.0 lock is historical evidence.

The official protocol wheel SHA-256 is
`bcddd6aa202352d0b83f4f023b395d6cef99128e419c49efa1a52a661229f3bf`;
the sdist SHA-256 is
`b721d1594bb1cd7d1e3935af97f3a865aa7458b1557d35018a126a3deab71c43`.
These belong to release `v0.18.0`, source
`73175b4a20476717a436695853b53b722baea3ed`. They are distinct from the earlier
local candidate wheel in section 9, even though its package files are identical.

Verify locked sync, CI and a clean wheel installation. Do not edit lock entries by
hand, override dependencies, skip dependency resolution or substitute a local wheel
for formal index evidence. CLI merge/tag/publication needs separate authorization;
publishing the protocol does not freeze or admit a runtime environment.

Historical baseline: CLI 1.7.0 used published protocol 0.14.0 and its registry lock;
protocol 0.13.0 introduced task commitments/shared package digests. These statements
describe the earlier release evidence, not this checkout's consumer requirements.

## 6. Normal-install verification

A `PYTHONPATH` splice does not establish that a miner can install this release.
Verify the actual consumer through normal locked resolution, using the repository's
existing CI commands. Maintainers regenerate the lock with `uv lock` only when
intentionally updating dependencies; ordinary verification keeps it unchanged:

```bash
uv sync --locked
bash scripts/lint.sh
uv run coverage run --source=src -m pytest -q --junitxml=pytest-report.xml
uv run coverage report
uv build
# Use a new, unused environment; install the built wheel with all dependencies.
uv venv /tmp/openroboto-consumer-new --python 3.11
uv pip install --python /tmp/openroboto-consumer-new/bin/python dist/openroboto-1.8.0-py3-none-any.whl
uv pip check --python /tmp/openroboto-consumer-new/bin/python
/tmp/openroboto-consumer-new/bin/openroboto --version
/tmp/openroboto-consumer-new/bin/openroboto task scene --help
```

CI also audits skips and wheel contents. Retain raw results and published protocol
artifact hashes; the release build's actual bytes need not equal a local candidate
wheel. The CLI environment does not install a simulator runtime. Installed S0
checks may use registry/describe/pure render/NEW E only; actual record/self-test
requires an independently approved trial plan and the final joint runtime tuple.

Historical explicit-wheel development verification used the following helper:

```bash
python scripts/verify_task_cli_install.py \
  --protocol-wheel /path/to/openroboto_protocol-<version>-py3-none-any.whl \
  --package /path/to/task/package \
  --expect-digest <64-hex> \
  --config <miner.yaml> \
  --status-config <miner.yaml-with-wallet> \
  --run-id <operator-run-id>
```

That historical helper copies the working tree to a temporary directory, rewrites the
protocol pin **only in that copy** to the candidate wheel's version, builds the
CLI wheel, creates a fresh venv, installs both wheels with normal dependency
resolution, and runs the installed `openroboto` binary without `PYTHONPATH`.
It is not the formal index/lock verification above. It checks `Track.TASK`,
`AXIS_LAYOUT`, the package digest, the live `/healthz`
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

CLI 1.8.0 requires protocol 0.18.0, which retains the controlled-v2 contract
introduced in 0.15.0. Use the matching reviewed CPU task runtime for v1/v2
authoring. The index-resolved consumer lock is described in section 5. Do not
deploy a stale lockfile or present a source-path development run as a cold install.

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

This scene capability remains a development path. The reviewed protocol input is commit
`ea476625c7f84e4ce8fbd9fed4e56ac001c01109`, version `0.18.0`, task schema 3,
execution contract 3 and `panda-open-scene-v1@2.1.0` with profile digest
`9badf61d742ed5787e7af4ad19adc90b616147ee4c62159915bddeff53f9a1a2`.
The reviewed local protocol candidate wheel SHA-256 is
`f2b59b83c3ac561b41bcd6b3ddf359406dd74cae9fb37198b84be75931ce63b8`.
This candidate gives SceneTask.instruction its own 16384 Unicode code point
capacity, bound into the new profile. Other text fields retain their old limits;
v1/v2, physics, tools, budgets and linear frame validation are unchanged.
Previous unpublished scene profile 2.0 packages remain reproducible with their
original 0.17.1 wheel. They are explicitly rejected by this reader, even below
1000 characters. There is no alias or in-place migration: a new profile/runtime
requires deliberately authored NEW E and fresh P/manifest/proof.
That candidate wheel retains its original evidence. Formal consumers use the
actual index release and regenerated lock from sections 5–6; a tag or local wheel
does not close that gate. Do not use source-path overrides or skipped dependencies
as install evidence. Keep the protocol release artifact hash separate from this
local candidate hash.

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

Canonical instruction generation uses an optional capability in the same runtime:

```bash
openroboto task scene instruction --execution ./source/execution \
  --runtime-python /path/to/runtime/bin/python \
  --output ./instruction-public.json --prepared-execution ./prepared/execution
```

The runtime's single pure `task_source.scene_instruction` renderer supplies the
complete text and program/render-input/renderer/policy identities. The CLI does
not render predicates, infer intent from keywords, shorten text or fall back to
a model. It checks the exact installed protocol/profile/environment/agent and
requires the advertised `scene-render-instruction` capability. A runtime lacking
that optional command returns structured unsupported; its existing record and
self-test capabilities remain usable.

The current runtime 0.3.11 and historical 0.3.5/0.3.6/0.3.7 producers share
`instruction=InstructionResult.to_dict()`.
The CLI reads that single nested format, binds `input_task_sha256` to the original
task bytes, and compares installed/input environments with the selected runtime.
The renderer/policy/source identities must match both describe and the exact
registry. The CLI independently measures the entire E digest and checks its
structure; runtime rendering itself reports `package_validated=false`.
The public `rendering.text` contains the full text (`rendering.instruction` remains
its earlier CLI alias). Schema/capacity, source, program/render-input and delivery
audit fields are retained. Generation leaves `delivery_checked=false` and the
actual/delivered text digests null; it does not claim a model received that text.

`--prepared-execution` must name a NEW directory outside retained input bundles.
Only E is copied. Only the top-level `task.json.instruction` string token changes;
other JSON tokens (including numeric precision, escaped keys and public summary)
and every other file stay byte-identical. Source E, existing P, manifest and proof
are untouched. No private root is copied, created or rebound. Omitting this flag
only produces the public report; rerun with a NEW E target to avoid manual text
escaping or numeric formatting. Keep public output outside the retained bundle.

New E requires fresh recording and self-test, within the operator's existing
registered trial plan. No physical experiment or model request is authorized by
rendering or preparation. Continue with `--execution ./prepared/execution`,
`--private ./prepared/private` and `--manifest ./prepared/submission.json` for
recording; never silently reuse an old private proof after changing E.

Generation uses a bounded isolated subprocess (default 30 seconds, maximum 60),
the shared output/JSON byte limits, and the unchanged installed SceneTask schema.
Unsupported length, vision or incomplete coverage returns nonzero and creates
no prepared E. Complete public text, when returned with an unsupported length
result, is retained in full rather than truncated or replaced by a digest.
Protocol 0.18.0/profile 2.1 uses the registered 16384-code-point capacity and
scene-instruction-admission/2; the renderer remains scene-program-text/1.
The CLI never changes schema capacity. Previous 0.17.1/profile 2.0 and its
1000-character policy remain historical candidates, with their original results.
The current normally installed local software candidate is runtime **0.3.11**,
validator source `f11a5833a6a8febffd52b1812aeb6e702432abe5` (the later
`068ac724257d75c8c336917600eed42746c30b37` adds only its delivery README).
Its runtime wheel SHA-256 is
`d739d545f36ee59c124d56b7dd3b380b955e071277e7783fd23eb0d75e6d25f5`,
with environment `ope231-scene-runtime-0.3.11-r1`, digest
`f1b02ec87af9d59bd2a1b49a2d8c86712e5e97f46c7dd776af9eb88d6c075a20`.
It uses published protocol 0.18.0, profile 2.1.0 and harness
`71c038ebabb77411ca306a14e72eb41a0b412710`. Its registered-source factory identity
is `scene-a-gateway@4`, digest
`d78e6940d844999e39bd2bac24586905590861f1651b1199f28490b35a627813`.
These identities describe the reviewed local payload, not active operator trust.

Select that candidate's actual installed Python with `--runtime-python` and use E
authored for those exact refs. The CLI reads installed distribution/registry values;
it does not force a runtime dependency into the lightweight CLI or rewrite older
E/P for compatibility. With this new runtime selected, an older environment or
agent returns `runtime_identity_mismatch`. Keep its original runtime and proof, or
deliberately author NEW E and obtain new reference evidence under an approved plan.

This candidate has normal macOS arm64 installation and pure instruction/NEW_E
software evidence. Its 38-wheel dependency lock is not a Linux native lock.
Active deployment/registration, complete physical record/self-test, server admission
and reward eligibility remain separate unmet gates. No physical run or model call
is authorized by selecting it, by scope=true, or by a successful pure render.

Historical installed S0 rendering evidence uses runtime **0.3.6**, validator commit
`32de3755bfb0db7b6e431fd016cb54ad87cacfcd`. Its wheel SHA-256 is
`39b53a3d4b6e01d5725aa71565bd1e6427af66c0c80e504224252c6526e76016`.
Its exact environment is `ope231-scene-runtime-0.3.6-r1`, digest
`a2932709e57e4532026778388dd8e433f0b46ac18ade0e67fa8ca839355bd2a9`,
with harness `26e7a2e5d4b67e86c197f36cc79723b71f87bebc`. That tuple is retained
for its own S0 evidence; it is not the final jointly frozen authoring runtime.
Select a matching runtime interpreter explicitly and author NEW E using its
describe refs.
Neither instruction preparation nor self-test upgrades old environment/agent
refs or rebinds earlier P/proof.

Its authenticated `scene-observation-admission/2` scope matches profile 2.1;
describe reports `observation_policy_scope_matches_profile=true`. That only
allows the remaining checks to proceed. It is not a physical reference,
complete scene-family validation, server admission or reward result. Actual
record/self-test still requires the separately registered local trial plan.
The later 0.3.11 candidate integrates the reviewed P02/P03/P07/P08 source payloads.
Final operational runtime/environment/authority registration and deployed isolation
gateway/dev acceptance remain pending. Pure rendering does not close those gates.

Historical runtime **0.3.7** / validator
`f808a9d1aa8b4ec504386acc08dd10f2a060f4c0` is a source/API checkpoint only. Its
environment is `ope231-scene-runtime-0.3.7-r1`, digest
`baa9477eb47f6261f885e8da2d23ba9e0117eaf2e1e33c55b92967c86a203fd3`.
It adds admission policy discovery and a typed `run_scene_admission` requiring
registered context and verified private reference. It still advertises
`server_admission=unregistered`; execution remains unavailable without the
reviewed A gateway/authority. No new physical CLI result or final joint freeze is
claimed for it. The subsequent 0.3.9 records also remain source-only checkpoints.
Do not relabel those E/P/proofs or present them as the installed 0.3.11 candidate.

Historical runtime 0.3.5 supports rendering but reports scope=false. Its
`scene_observation_policy_scope_unregistered/resource_unavailable` results remain
valid under that older identity. The CLI continues to stop before dispatch for
an explicitly false scope, without downgrading or bypassing the gate.

The runtime bridge uses the reviewed `task_source.authoring` scene API, not the
legacy v1 recorder. Start with materialized E at `./authored/execution` and choose
an explicit **new** private root and detached manifest. They need not exist for
recording. The CLI never derives private materials from a public summary, copies
solver code into E, overwrites an existing P, or rewrites E for another runtime.

```bash
openroboto task scene record-reference --execution ./authored/execution \
  --private ./authored/private --manifest ./authored/submission.json \
  --script ./solve.py --runtime-python /path/to/runtime/bin/python \
  --private-report ./private-diagnostics/record.json --output ./record-summary.json
openroboto task scene check ./authored
openroboto task scene self-test --execution ./authored/execution \
  --private ./authored/private --manifest ./authored/submission.json \
  --runtime-python /path/to/runtime/bin/python \
  --private-report ./private-diagnostics/test.json --output ./self-test-summary.json
# Equivalent self-test shorthand (existing complete bundle):
# openroboto task scene self-test ./authored --runtime-python ... --output ...
```

`--output` receives only projected identities, fixed errors and check booleans.
`--private-report` receives the runtime's full diagnostics; its adjacent
`.evidence` directory may contain private trajectories, states and proof. Keep
both reports/evidence outside E/P and the transport bundle. Omitting
`--private-report` retains diagnostics in a new mode-0700 temporary directory;
the projected report gives its path. Never publish that diagnostic file/directory.
Raw child stdout/stderr and arbitrary error strings are not relayed to the CLI.
Reports and outputs must be new. Failed artifacts remain available for diagnosis.

The selected `--runtime-python` runs in isolated Python mode. Before dispatch the
CLI verifies installed protocol and runtime distributions, schema 3/execution 3,
exact profile, structured scene command capabilities, canonical registry identity,
robot/control and the frozen agent binding. It verifies runtime identity again
after execution. The 0.3.x scene API v1 runtime is an unpublished candidate; the initial d3821a5
wheel pins protocol 0.17.0 and is incompatible with this CLI's 0.18.0 baseline.
Patch 0.3.1 retains the same authoring interface and changes environment identity.
Runtime 0.3.4 retains the old protocol 0.17.1/profile 2.0 identity and its existing
physical evidence; it cannot be relabeled as the new profile. Runtime 0.3.5 uses
0.18.0/profile 2.1, exposes the optional renderer, and explicitly reports its
historical unregistered observation scope. Current runtime 0.3.6 registers that
scope and changes scene gripper command continuity with a new harness/environment;
old P/agent bindings cannot be reused. This bridge cannot make a different
environment supported or authorize an experiment.

Recording invokes `scene-record-reference` with immutable E and NEW P/manifest;
all initial states must be recorded. The old candidate positional recording form
returns `scene_argument_migration` with explicit replacement arguments before
loading P or running a script. Self-test first validates E/P/joint and the
normalized execution contract, then invokes `scene-self-test` with the exact
`--expect-submission-digest`. An optional public flag of the same name also
checks an independently retained digest. Self-test always uses the runtime's
registered all-state reference/repeat/noise/nop/observation/tool-proof pipeline;
it offers no seed subset, policy override or skipped-physics option.

`--timeout-seconds` bounds the entire local command (default recording 900,
self-test 180; maximum 900 seconds), including metadata probes. It never increases
the task's execution budgets. Recording alone accepts `--max-reference-steps`,
which may only lower the contract step cap. Child output is limited to 1 MiB and
private report parsing to 8 MiB; malformed, nonfinite or duplicate-key JSON,
timeouts and incomplete/mismatched receipts return nonzero safe diagnostics.
E and existing P/manifest are checked again after execution. A completed rejected
self-test remains `accepted=false`, nonzero and `local_physics=failed`; a static
check or incomplete receipt cannot claim physical completion. Local recording
or self-test never establishes server admission or reward eligibility.

CLI subprocess fixtures are synthetic interface evidence. Real cold-installed
CLI/runtime physics verification belongs to OPE-231's frozen trial plan and its
remaining allowance; this client implementation alone adds no physical result.

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

If the contributor identity uses a nondefault key, add `--key-file /path/to/author.key`
to `submit`, using the same path as `identity show`. Repeat it on each retry;
`--config` alone does not select that key. The default path remains
`~/.openroboto/contributor.key`. Scene registration uses the wallet hotkey and
does not need the contributor key.

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
