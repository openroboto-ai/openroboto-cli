# Changelog

Every entry answers one question before it lists anything: **does a miner have to
change something, and what breaks if they do not?** A diff summary without "who
has to act" is not usable by the people who installed this.

## 1.7.2 (candidate)

Miners using a season with gated submissions must upgrade from 1.7.1. Its access
list check used an incorrect SDK field and stopped real submissions before
upload or payment. This patch reads the SDK's `AccessRequest.username` field;
tests use the actual SDK data type. Unreadable or malformed access lists still
refuse submission with an actionable error, without exposing request emails or
form fields. The upload-before-payment privacy gates remain unchanged.

## 1.7.1

Task authors submitting to a season with `params.hf.grant_to` must upgrade before
uploading a new package. `submit` now reads the live access policy and confirms
manual gating and the official account's approval before uploading any files.
It creates only empty repository metadata before this check. Existing private
repositories stay private; unexpected approvals stop the submission for manual
review rather than being revoked. A failed access check uploads nothing and
pays nothing. Repository names, metadata and model cards remain public on HF.

The live policy and accepted accounts are checked again before payment. A
changed target requires review and a retry. Seasons without a target retain
their existing upload behavior; paid and announced recovery never pays again
or blocks the remaining announcement on a new access check.

## 1.7.0

Task authors can create a restricted Panda tabletop definition, build its
package in a separately installed simulator, record local reference actions,
and run physical self-tests. The CLI remains free of simulator dependencies.
`task catalog` exposes the exact supported objects and ranges. Static checking
and pre-payment submit validation now share reference header and step-budget
rules with the worker; a season-required execution profile cannot be omitted.
Old task packages and the shared package digest are unchanged.

This CLI candidate pins published protocol 0.14.0 with a registry-resolved lock.
The separate simulator runtime remains a review candidate. Normal dependency
resolution is used for both installed environments.

## 1.6.0

### Task-authoring seasons: a package, not a model

A season with `track: task` takes a **task package** (a `task.json`, its
instances and manifests) instead of a model checkpoint. `openroboto check` would
have judged that package by the π0.5/LingBot file rules and rejected a correct
one, and `submit` would have refused it for having no weights. Both are fixed:

```bash
openroboto task check ./my-task            # local, offline
openroboto task check --repo user/pkg --commit <40-hex>
openroboto task status --probe             # GET /healthz + /readyz only
openroboto task status                     # your own tasks, MINER key + signature
openroboto submit
```

`task status` uses the **MINER-level** `backend.public_key` and a hotkey
signature for `/api/v1/tasks/mine`; it never reads the ADMIN_KEY. Both the
probe and the task query print the backend `request_id`, so an evidence record
can be matched against server logs. `--run-id` records the operator's A/B run
id in that output only -- the task endpoints have no run-id field and the
backend rejects unknown query parameters, so it is not sent. `--backend-url`
overrides `backend.url` for one read-only call, so pointing the CLI at an
isolated instance does not require editing `miner.yaml`. The probe sends no key
at all. A `401 UNAUTHORIZED` means the MINER key is missing; a
`401 INVALID_SIGNATURE` means the wallet's hotkey is not the queried one.

`task check` warns (`axis.reference_not_declared`) when an AXIS package has no
reference solution: it is static-only and the validator's runtime admission
rejects it as `reject_unsolvable`. `submit` refuses that shape **before the
fee**, so a miner does not pay for a guaranteed runtime rejection. A declared reference must carry non-empty
per-instance `actions`; a final-state-only file is an error. Adding a reference
changes the digest, so it is a new package identity, not an edit of a frozen
one. `scripts/build_task_package.py --reference` includes one and refuses an
unusable file.

`task check` prints the SHA-256 of **every file** and the package digest. That
digest is the same rule the validator's admission uses and the backend stores in
`tasks.package_digest`; `--expect-digest` lets a miner compare it with a value
the subnet reported. It validates the declared source's structure (AXIS
manifests and payload hashes, RoboDojo official task fields), but it does not
claim the task is solvable or deterministic — that is runtime admission.

`submit` now routes a task season to that package gate instead of the model
layout/fingerprint gates, and refuses a package whose declared source differs
from the season's `params.task.source`. The upload no longer writes
`run_info.json` into a task package: the digest counts every file, so that file
would change the package identity between what the miner checked and what the
validator reads.

`openroboto init --include-task` can create a task workspace; `--refresh`
always asks for task seasons so an existing one can be refreshed. A task
workspace must name `huggingface.repo_id` — `pi05-<hotkey>` is a claim about a
model.

⚠️ **Requires a protocol release that knows `Track.TASK`.** The source protocol
repository's `main` has it, but the 0.12.0 wheel published on PyPI predates the
change — and that `main` line does not contain the 0.12.0 AXIS-layout change
this CLI also imports. A release that merges both is needed. Until then,
`init --include-task`, `task check` and a task season's `submit` refuse with an
explanation: the pinned release cannot parse `track=task` and has no shared
`openroboto_protocol.package_digest`. `task status --probe` / signed
`task status` still work (they do not compute a digest).

The package digest is **only** `openroboto_protocol.package_digest` (OPE-211
candidate 0.13.0). There is no local mirror. The production pin is still the
published 0.12.0, which predates the module, so `task check` refuses with the
release/candidate instruction and `task status`/`check` do not pretend to
compute a digest until 0.13.0 is published and the pin moves. `digest_source`
reports `openroboto-protocol` or
`unavailable(protocol release 0.13.0 needed)`. A local development environment
uses the candidate wheel via `scripts/verify_task_cli_install.py`; that is
**not** a claim that 0.13.0 is pip-installable from PyPI. The verifier is
fail-closed: a missing shared module/function, a non-zero probe,
`digest_source` other than `openroboto-protocol`, or a digest mismatch exits
non-zero.

`openroboto doctor` reports `protocol capabilities` (`Track.TASK`,
`AXIS_LAYOUT`, `package_digest`) and fails a task workspace when any is
missing, so a temporary cherry-pick tree cannot be mistaken for the release a
formal run must pin.

## 1.5.1

### Nothing to do; `openroboto check` stops warning about correct AXIS checkpoints

When `miner.yaml` does not record the season's task set — a workspace from before
1.5.0, or `check` run from another directory with no config — `check` used the
LIBERO layout and flagged an AXIS checkpoint's correctly placed norm stats as
`non_canonical_norm_stats`. It now asks the backend for the live season (one
anonymous request) and judges by that season's task set; the `rules:` line says
`π0.5 (openpi, AXIS)`. If the backend cannot be reached, or more than one π0.5
season could be meant, it keeps the LIBERO layout and says so. `submit` was
already judging by the live season and is unchanged.

## 1.5.0

### If you mine the AXIS simulation season: upgrade before you submit

`pip install -U openroboto`

AXIS checkpoints keep their normalization stats at
`assets/axis-v0.1-task501-runtime-v1/norm_stats.json` — that is where the AXIS
evaluator reads them, and where the season's starting checkpoint ships them.
1.4.0 judged every π0.5 checkpoint by the LIBERO layout, so a correct AXIS
checkpoint drew `non_canonical_norm_stats`, and `openroboto submit` refused to pay
for it. **Do not move your stats to the LIBERO path to silence that warning** —
the AXIS evaluator would then find none.

1.5.0 picks the layout from the season's task set (`benchmark` on the season
record). The gate in `submit` reads it from the live season, so it is right even
for a workspace created with an older CLI. `openroboto check` reads it from
`miner.yaml`; a file written before 1.5.0 does not record it, so `check` may still
print that warning there — the gate in `submit` is the one that decides, and it
reads the live season. Adding `benchmark: axis_v1.0` under `competition:` in
`miner.yaml` makes `check` agree. The `rules:` line says
which one judged you: `π0.5 (openpi, AXIS)` or `π0.5 (openpi)`.

### Nothing else changes

LIBERO and LingBot seasons are judged exactly as before. Requires
`openroboto-protocol==0.12.0` (installed with it).

## 1.4.0

### You do not have to act, but your repository changes

`openroboto submit` now makes your model repository **gated** and puts the
official evaluation account on its access list — before it pays the entry fee.
Nothing to configure: it runs with the token already in your `miner.yaml`, on
your machine, and we never hold a credential of yours.

What changes for other people looking at your repository: they can still see
that it exists, its file names and sizes, and your `README.md`. Downloading the
weights returns 401. Until now those weights were downloadable by anyone for the
whole time a season was being scored.

| Situation | What to do |
|---|---|
| You want your weights withheld while the season is scored | Nothing. `submit` does it. |
| You already set the repository **private** | Make it public again before submitting. Hugging Face checks visibility before the access list, so a private repository refuses the evaluator too — the submission is rejected and **the fee is not refunded**. `submit` gates it for you; private is never the right setting. |
| Your entry starts being paid out | **Remove the gate.** A rewarded entry has to be public for the whole claim period, and a repository left gated stops the payouts. See `docs/REAL_TRACK.md` §5. |
| `submit` says it could not gate or grant | Fix the access problem and run it again. It stops **before** the fee, so nothing has been spent, and re-running resumes the upload rather than repeating it. |

Which account gets access is read from the season, not compiled into this CLI:
a season that names none is left completely alone, which is every simulation
season today. Nothing about mining one has changed.

## 1.3.0

### You have to act if you script this CLI

`round` is not a concept this CLI has. A workspace mines one competition, so the
number every command needs is that competition's id, and it is already in
`miner.yaml` — nothing has to be passed on the command line, and nothing has to
be guessed from a directory listing.

| Before | Now | What to do |
|---|---|---|
| `openroboto submit --round N` | `openroboto submit` | Drop the flag. The workspace knows its competition; passing one was how you submitted against the wrong season. |
| `openroboto check --round N` | `openroboto check` | Drop the flag. `openroboto check <path>` still takes an explicit directory. |
| `openroboto status --round N` | `openroboto status --competition <id>` | Rename it. The value is now a competition **id**, not an ordinal, and it defaults to the competition this workspace mines. |
| `state/round_N.json` | `state/competition_<id>.json` | Nothing, for a workspace that has finished its submissions. An **unfinished** one (uploaded or paid, not yet announced) must be renamed by hand, or the resume will not find it — see below. |
| `tmp/robot_train_vla_miner/round_N/` | `tmp/robot_train_vla_miner/competition_<id>/` | Nothing, unless a script hard-codes the path. |
| `round_info.json` in the uploaded checkpoint | `run_info.json` | Nothing. Nothing reads it, and the model fingerprint excludes it either way. |
| `payment:` in `miner.yaml` | — | Delete it if you like; it is ignored. It never decided what you paid. |
| `subnet.subtensor_endpoint` in `miner.yaml` / `validator.yaml` | — | Delete it. It parsed but was never read, so it has never overridden anything; leaving it in place would keep promising that it does. The chain connection takes `subnet.network`. |
| `urls.control_json` in `miner.yaml` | — | Delete it if you like; it is ignored. It remains a **validator** setting in `validator.yaml`. |

🔴 **Mid-submission when you upgrade?** Rename
`state/round_<seq>.json` to `state/competition_<id>.json`, taking `<id>` from the
`competition:` section of your `miner.yaml`. The fee recorded in that file is
then reused and you do not pay twice. Without the rename, `submit` starts over
and **pays again**.

### Fixed

- **`openroboto status` works again.** The backend has required the competition
  on the submission-history query since 2026-09-01; earlier clients send an
  ordinal it no longer accepts and get an error instead of your history. Rejected
  submissions are now listed for every competition, because rows rejected during
  the chain scan carry no competition at all — filtering them by a number the
  rejected payload may itself have got wrong hid the row you came to find.
- **`training_proof.json` names your season's base model.** It stated
  `"model": "pi05"` for every season, so a LingBot checkpoint shipped to your
  public repository with a proof claiming a π0.5 base. The `config` key is gone
  with it: the training config name lives inside the image and the container
  does not report it, so the host cannot state one without inventing it.
- **Base checkpoints cache per base model** (`cache/<base>`, was always
  `cache/pi05_base`). One directory shared by every season reports a cache hit
  on another base model's weights — the container skips the download and trains
  against the wrong base. An existing `cache/pi05_base` is still a hit, so
  nothing re-downloads.
- **Status words are shown as the backend sends them.** The mapping that
  rewrote a retired worker vocabulary could only ever rewrite words that cannot
  arrive, and would have hidden one that did.

### Changed

- Pins `openroboto-protocol==0.11.0`.
- `OPENROBOTO_E2E_CONFIRM=1` answers the payment confirmation, on the testnet
  netuid only. On any other netuid it **refuses and says so** rather than being
  ignored.
- A workspace with no `competition:` section is refused by `submit` before it
  uploads anything, including one that has already paid. If that is you: your
  checkpoint under `state/` is untouched, so restoring the section with
  `openroboto init --refresh` resumes the same submission without paying twice.
- Drops the `Python :: 3.12` classifier. Supported and tested is 3.11.

### Removed

- The compatibility baseline for pre-1.0 miners, its fixtures and its generator.
- `docs/control_json.md` and its sample, replaced by one section in
  `docs/VALIDATOR.md` — `public_key` is the only field anything reads.
- The `rt.py` → `openroboto` migration guide. Those entry points are gone.

## 1.2.0

### One repository per season, named after that season's base model

`build_repo_id` produced `{user}/pi05-{last 12 of hotkey}` — a fixed string, and
one repository for a miner's whole career. Two failures came out of that pair:

- **The name outlived the base model.** π0.5 was archived and LingBot-VLA 2.0
  took over, while every repository kept saying `pi05`. Eight queued submissions
  read `<user>/pi05-…` while every one of them held a LingBot model; three people
  in a row read that table and concluded miners had submitted the wrong base, and
  one proposed rejecting all eight at admission — eight paid submissions, 0.8 TAO
  burned, models entirely correct.
- **Seasons piled up in one directory.** `upload_folder` never deletes, so a
  career-long repository is season 7 laid on seasons 1–6, and a `.cache/` left by
  an earlier push is `LEFTOVER_UPLOAD_STATE` at admission — terminal, fee gone.

The name is now `{username}/{base_model_family}-{last 12 of hotkey}`. It is a
default, not a protocol rule: the backend fetches whatever the commitment's `i`
field points at.

### 🔴 Upgrading does not move your repository

**You do not have to do anything.** A workspace whose `competition:` section
names no base model keeps its `pi05-…` name — which is every workspace a
released CLI created, because `base_model_family` landed after 1.1.1 shipped.

An earlier build of this change refused instead, which would have broken every
miner on upgrade: `upload` dies until you run `init --refresh`, and then
re-pushes ~25 GB to a repository nobody asked for. Refusing is right where
`base_model_family` selects the rule book a paid submission is judged by; as a
word in a repository name it decides nothing.

Adopting the season-scoped name is opt-in: `openroboto init --refresh`, and the
next upload re-pushes the model once. `huggingface.repo_id` still pins any
repository explicitly, and a missing username or hotkey is still refused.
