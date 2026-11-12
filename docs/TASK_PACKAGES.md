# Task packages — the miner workflow

> **Status**: current · **Updated**: 2026-11-12 · **Audience**: task authors.

## Initialize, check, submit

```bash
openroboto init my-task-workspace
cd my-task-workspace
# Select a task season, edit task.json using catalog.json, and add solve.py.
openroboto check
openroboto submit
```

`init` lists task seasons by default. It selects templates using the chosen
`competition.track`: model seasons keep their training workspace; task seasons
get `miner.yaml` with `authoring`, a valid `task.json` authoring definition,
`catalog.json`, this track's README and `.gitignore`. The catalog contains the
complete protocol catalog for the season's exact execution profile, including
all objects, dimensions, limits and resource profiles. A development profile
stays marked as development even when a season explicitly selects it.

The workspace `task.json` is an editable authoring input. Controlled profiles
produce `task-package/task.json`. Scene profiles produce `task-package/execution/`,
`task-package/private/` and the detached `task-package/submission.json`. Submit
uploads only `authoring.package` (default `task-package/`); it does not upload
`miner.yaml`, solve.py or evidence.

## Free local admission

```bash
openroboto check --static-only           # definition/package format, no Docker
openroboto check                         # missing reference: use solve.py
openroboto check --example               # unchanged v1 one-cube example only
openroboto check --script ./solve.py --json
openroboto check ./existing-package --json
openroboto check --repo user/pkg --commit <40-hex> --static-only --json
```

Full check validates the definition, builds a missing/changed package, records a
missing reference, packages the reference into a new content identity, runs the
worker's structural admission, then its physical admission. The worker's actual
`admit_package` / `_admit_task` and `run_admission` supply the verdict. Checks
retain `name`, `passed`, `kind`, and `detail`; the CLI does not implement another
checker, noise, freebie or determinism policy.

`READY TO SUBMIT` means the local static/physics checks passed. JSON records
`package_digest`, `admission_version`, `runtime_version`,
`admission_policy_sha256`, `runtime_content_sha256` and the immutable local
Docker `image_digest`. A missing reference, unsolved instance, unstable checker,
freebie or invalid structure yields `NOT READY` and a next action. Static-only
success has `ready_to_submit=false` and does not produce a physical passing
receipt. It never builds or runs a container.

Your local `solve.py` defines `solve(runtime, task, instance)`. It must solve every
instance through the runtime; reference replay and admission check the recorded
actions. `--example` is a real pushing example for the v1 cube template, not a
solver for arbitrary objects or composed tasks. For an existing incorrect
reference, use `openroboto record-reference --script solve.py`, then check again.

Recording uses a working copy. Only a complete new reference is published into
the task package. The final `reference.json` is required package content and
changes the digest; solve scripts, raw recording reports and admission reports
stay in `evidence/`. Physical admission mounts the package read-only. Old failed
attempts and previous bundles remain available for inspection outside the upload.

No check uploads, pays, contacts a model or requires a model API key. The local
dedup corpus is explicitly empty: passing it cannot establish originality
against the server's private corpus. Server registration, admission and reward
eligibility remain separate server decisions.

## Official self-test image

As with simulation training, the CLI wheel carries the official Docker build
context. No container registry login or validator checkout is needed:

```bash
openroboto build                         # optional package/image preparation
openroboto doctor                        # package, profile, Docker and image
```

`check` builds the image automatically on first use. The context includes a
pinned Python base digest, a fixed Debian snapshot, a hash-locked dependency
file and the validator's runtime wheel. Every file is SHA-256-bound in
`runner/task/release.json`. The installed CLI verifies those bytes, the local
image label, and the container's runtime/protocol/admission/profile metadata.
Commands then run by immutable image ID, not a mutable tag. The first build needs
network access to download the pinned dependencies; container execution uses
`--network=none`. Subsequent checks reuse the matching local image.

The optional season declaration is `params.task.runtime` with `version`,
`content_sha256` and `admission_version`. Each declared field must match the
bundled release. `init` records the selected release under `authoring.runtime`;
`check` and `doctor` refuse a mismatch instead of substituting another runtime.
The exact execution profile is mandatory for this workflow. Older unprofiled
packages can still be inspected with explicit `check <path> --static-only`.

The CPU admission policy comes from the worker's bundled `axis-cpu` policy:
180 seconds for a complete package's physics; reference recording allows at
most 900 seconds. The local container additionally limits CPU to 2 cores, memory
to 4 GiB, PIDs to 256 and temporary storage to 512 MiB. It runs as the host UID,
with a read-only root, no capabilities, no network and only explicit input files
and a scratch output mount. No workspace credentials are mounted. These local
isolation limits do not assert that an independently deployed worker has the
same host/cgroup configuration.

Maintainers build the runtime context with the validator's
`authoring_runtime/build_image_context.py`, then include that exact context in
the CLI release. The runtime wheel is part of the CLI artifact; it does not need
a separate PyPI or registry publication. Keep the release manifest, build log,
wheel hash and actual image ID with acceptance evidence.


## Paying a Gamma submission fee

The season publishes its payment kind and exact fee in
`competition.params.fee` in `miner.yaml`. For a Gamma season, `kind` is `gamma`
and `amount_gamma` is the fixed Gamma price; the CLI does not convert it from
TAO each time you submit. Run `openroboto init --refresh` if the live fee changed.
`init` and `check` remain free.

Before your first paid submission, fund the Gamma deposit at your miner hotkey
on the selected subnet. This CLI pays locally: the deposit must belong to the
coldkey that owns your miner hotkey and name that same coldkey as its spender.
The CLI never uploads your wallet keys or asks a backend to spend for you.

```bash
# Replace every placeholder with the values of your own wallet and season.
btcli gamma mint --netuid <NETUID> --hotkey <MINER_HOTKEY> \
  --amount-tao <TAO_TO_TOP_UP> --spender <YOUR_COLDKEY> -w <WALLET>
# For an existing deposit with another spender:
btcli gamma set-spender --netuid <NETUID> --hotkey <MINER_HOTKEY> \
  --spender <YOUR_COLDKEY> -w <WALLET>
openroboto doctor
openroboto submit
```

Minting buys and burns subnet Alpha to create service credits. Gamma spending
destroys those credits; it does not transfer tokens to a collection wallet.
Keep TAO available for chain transaction fees. A deposit's daily spending cap
must allow the complete submission fee; doctor and submit explain how to fix
insufficient balance, spender authorization or allowance.

Submit runs the existing package, reference, receipt, identity and duplicate
checks before asking to pay. It records the signed transaction hash durably
before broadcast and requires the exact `GammaSpent` event for the subnet,
owner, miner hotkey, amount and immutable `repo@commit`. Transaction success
alone is not payment: a spend can be skipped.

If a chain call times out, rerun `openroboto submit` in this same workspace.
It reconciles the original transaction and never sends a replacement charge
while the outcome is unknown. Preserve both `state/competition_<id>.json` and
`state/competition_<id>.gamma.json`. The Gamma journal survives a damaged main
checkpoint. Same-host workspaces share an authoritative journal under
`~/.cache/openroboto/gamma-payments/<genesis>/<submission-slot>.json` and a payment
lock keyed by the backend's `(netuid, competition_id, hotkey, hf_commit)`
submission slot. A second directory reuses the original payment. If it names
another repository for the same slot, submit points to the original repository
and stops before charging. Preserve that cache
as well as the workspace copies. Do not submit the same payment wallet and
repository revision concurrently from different machines. `--force` reuses a confirmed
Gamma payment for the same submission instead of charging again; it cannot
clear an unresolved payment. Gamma waits for chain finalization. If the complete signed transaction era
expires and every block in that era is finalized with no such transaction,
submit can return to its full free checks and ask to authorize a new payment.
Missing or unfinalized blocks keep the payment unresolved. An already paid
submission whose announcement window expired needs operator review and is
never charged again automatically.

## Other commands and task status

`catalog` prints the same profile-filtered object as `catalog.json`.
`create [task.json]` creates a fresh definition without overwriting a file.
`record-reference` and `self-test` use the bundled container and default to the
workspace package. These task-specific verbs refuse model workspaces. There is
one top-level set of `build`, `check`, `status` and `doctor`, routed by the
workspace track. The former task command group has been removed without aliases.

```bash
openroboto status --json
openroboto status --probe
openroboto status --probe --backend-url http://127.0.0.1:8091
```

Task status uses the configured hotkey's signature over the backend access
message plus the MINER-level API key. A readiness probe sends no key and queries
only health/readiness. The CLI never reads an ADMIN_KEY. `--run-id` is recorded
in output only, not sent as an unknown backend query parameter.

## The digest and payment gate

`openroboto_protocol.package_digest` is the shared implementation. It hashes all
regular package files, sorts paths as paths, and hashes the UTF-8 sequence of
`path + NUL + file_sha256` lines. Directory names do not affect identity; adding
any package file does. Do not put reports, solver code or temporary files there.

`submit` preserves the existing pre-payment gate: fetch the exact `repo@commit`,
select and validate the package, require reference trajectories, compare the
season's task source and execution profile, and print the resulting digest.
Failed fetch or validation stops payment. It then verifies the detached local
`evidence/check-receipt.json` against that measured digest. The receipt records
the final package identity, admission/runtime/content versions, actual image ID,
UTC check time, workspace and season identity, scope, and each check's
`name`/`passed`/`kind`. It contains no check details, reference actions or private
task text. It stays outside the uploaded package and never changes its digest.
Every new check invalidates the previous receipt before running; a failed check
leaves no successful receipt.

Payment requires a successful `full` receipt from this workspace and season,
matching the current admission release and the season's declared validity period.
Missing, stale, static-only or mismatched receipts stop before confirmation and
tell you to run `openroboto check` again. Changed uploaded bytes require checking
and uploading those same bytes. Submit reads the receipt without building a
container or rerunning physics, and does not upload the receipt to the backend.
The fee and live season are rechecked before confirmation. Check does not
implicitly authorize a payment.

Operators must publish `params.task.check_receipt.max_age_seconds` as a positive
integer and `params.task.runtime.admission_version` as the active worker's
admission version. There is no default validity period or guessed live worker
version. The CLI reads these fields from the live season response during submit;
editing the workspace snapshot cannot grant an exception. A season may explicitly
set `params.task.check_receipt.allow_static_only=true`; submit then labels that
exception in its output. Otherwise `--static-only` never qualifies for payment.
The runtime version/content/policy and image digest declarations, when provided,
must also match the checked release.

This workflow supports controlled Panda v1/v2 and schema-3 scenes. Scene receipts
also bind the private-root digest, joint `submission_digest`, environment,
execution-contract digest and exact runtime identity. A changed E creates a new
P; neither an old scene proof nor a structural check can replace the full receipt.
Ordinary `submit` recognizes the dual-root workspace automatically and registers
the measured identity with the backend after announcement. If chain ingestion
is pending, rerun `submit` to finish registration without paying again. An
unregistered or mismatched server runtime still stops upload/payment.
The backend must also publish a matching active reference-panel execution
contract. Its exact digest is part of the verified submission route; runtime
availability alone cannot authorize payment. `init` writes the published contract
into the scene template and `catalog.json`, and snapshots it in `authoring`.
Importing into that workspace uses the published contract and requires fresh
reference actions. Check refuses a changed contract; submit verifies it again
against the current server declaration.

The CLI pins `openroboto-protocol==0.19.0` for Gamma payment identity. Runtime
0.3.20 retains its frozen protocol 0.18.0 scene contracts and MuJoCo 3.17.0;
payment does not execute in the self-test image. Verify normal locked installs and wheel contents with:

```bash
uv sync --locked
bash scripts/lint.sh
uv run coverage run --source=src -m pytest -q
uv run coverage report
uv build
```

Legacy official AXIS and RoboDojo material remains subject to the season's source
and runtime. The bundled CPU authoring image supports the exact controlled
Panda v1/v2 and exact scene profiles; RoboDojo's Isaac Sim runtime is not a CPU
substitute.

## 9. Author-owned scenes (schema 3)

Use the same `init` → `check` → `submit` workflow above. A scene workspace includes
a complete public CC0 import starter, MJCF, declared mesh and catalog. Replace its
probe goal with your task and write `solve.py`. The starter is structural input,
not a general solver or admission example. Check copies only `task.json` and
declared dependencies into a new E, renders the complete canonical instruction
in the official image, records every initial state and verifies the exact E/P
pair through the existing scene authoring API. No host runtime interpreter is
needed. `doctor`, `build`, `record-reference` and `self-test` use this profile too.
Solvers that read local data files declare them in `authoring.solve_inputs` as
workspace-relative paths. Recording mounts only those regular files and the
script read-only, preserving their paths; it never mounts workspace credentials
or uploads the solver's auxiliary inputs.

To import an existing execution tree without changing its old evidence:

```bash
openroboto create authored/task.json --from-execution /path/to/original/E
# Set authoring.definition: authored/task.json in miner.yaml.
openroboto check
```

Create retains the original E and writes new inputs with the bundled runtime's
environment and agent binding. Scene assets, all initial states and the original
checker remain author inputs; source solvers and any private supporting data
stay outside the uploaded roots. An imported task still needs fresh successful
reference actions. Missing inputs, physical penetration, solver failures or
freebies stop readiness and cannot authorize payment.

The explicit `scene` commands below remain available for advanced inspection
and independently installed runtimes. They do not replace the workspace workflow.

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
openroboto catalog --include-development --json
openroboto scene build --execution ./my-execution \
  --reference ./my-private-reference.json --output ./my-submission
openroboto scene check ./my-submission
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
openroboto scene instruction --execution ./source/execution \
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

The historical 0.3.5/0.3.6/0.3.7/0.3.11 producers share
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
openroboto scene record-reference --execution ./authored/execution \
  --private ./authored/private --manifest ./authored/submission.json \
  --script ./solve.py --runtime-python /path/to/runtime/bin/python \
  --private-report ./private-diagnostics/record.json --output ./record-summary.json
openroboto scene check ./authored
openroboto scene self-test --execution ./authored/execution \
  --private ./authored/private --manifest ./authored/submission.json \
  --runtime-python /path/to/runtime/bin/python \
  --private-report ./private-diagnostics/test.json --output ./self-test-summary.json
# Equivalent self-test shorthand (existing complete bundle):
# openroboto scene self-test ./authored --runtime-python ... --output ...
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
openroboto status --config miner.yaml
# Use the backend task id above, not task.json's author-defined task id.
openroboto scene register BACKEND_TASK_ID --package ./my-submission \
  --config miner.yaml
openroboto scene status BACKEND_TASK_ID --config miner.yaml
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
and a paid checkpoint are reused. On legacy TAO payments, `--force` still pays
again. Gamma reuses the confirmed payment for the same immutable submission
and refuses to replace a charge with an unknown result.

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
