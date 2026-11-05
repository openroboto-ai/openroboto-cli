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
```

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

The `track=task` value is on the protocol repository's `main`, but the 0.12.0
wheel published on PyPI predates it — and that `main` line does not include the
0.12.0 AXIS-layout change this CLI also imports. A release merging both is
required. Until it is published:

- `openroboto task check` works, because it never parses a competition row;
- `openroboto init --include-task` and a task season's `submit` refuse with an
  explanation instead of a schema error.

## Related documents

- [CONTRIBUTOR.md](./CONTRIBUTOR.md) — the identity a task submission is paid to
- [PAYMENT.md](./PAYMENT.md) — what the entry fee costs and when it is wasted
- [README.md](./README.md) — the rest of the command surface
