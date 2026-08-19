# Migration: `python rt.py` → `openroboto`

> **Status**: current · **Updated**: 2026-08-19 · **Audience**: miners who cloned this
> repository before 2026-08-19
> **Scope**: what changed, what your old commands map to, and what to do if you are
> mid-round right now.
> **Note**: If you installed with `pip install openroboto` you were never on the old
> path and nothing here affects you.

## What changed

The old entry points are **deleted**, not deprecated:

`rt.py` · `miner.py` · `payment.py` · `validator.py` · `miner/` · `utils/` ·
`protocol/` · `download_checkpoint.py` · `download_checkpoint.sh` ·
`requirements.txt` · `miner.example.yaml` · `validator.example.yaml`

If you `git pull`, `python miner.py` and `python rt.py submit` stop working. There is
no `rt` alias — an abbreviation nobody can read is the naming this repository set out
to remove, and shipping an alias would keep it alive indefinitely.

Everything those files did is in the `openroboto` command, installed from PyPI. You no
longer clone anything.

## Install

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install openroboto
openroboto --version          # CLI version + protocol package version
```

## Command map

| Before | Now |
|---|---|
| `bash download_checkpoint.sh` | *(gone)* — the training container fetches the base checkpoint into `cache/pi05_base` itself |
| `cp miner.example.yaml miner.yaml` | `openroboto init my-miner` — a whole workspace: `miner.yaml` (subnet constants pre-filled), `train_strategy.py`, a `README.md`, and a `.gitignore` that keeps your wallet password out of git |
| `cp validator.example.yaml validator.yaml` | `openroboto init --validator` |
| `docker build -t robot-train-openpi:latest openpi-runner/` | `openroboto build` |
| `python miner.py --config miner.yaml` | `openroboto train` |
| *(nothing — you cloned a second repo)* | `openroboto check` — checkpoint format verdict, locally, **before** you pay |
| `python rt.py submit --config miner.yaml --round 1` | `openroboto submit --round 1` |
| `python rt.py upload --config miner.yaml --round 1` | `openroboto upload --round 1` |
| `python rt.py burn --config miner.yaml` | `openroboto burn` |
| `python rt.py announce --config miner.yaml --round 1` | `openroboto announce --round 1` |
| *(nothing — you read the website)* | `openroboto status` — your submissions and the exact rejection reason |
| `python validator.py --config validator.yaml` | `openroboto validator run` |
| `docker compose up --build miner` | `openroboto train` — see below |

`--config miner.yaml` is the default everywhere, so you can drop it.

### About `docker compose up --build miner`

That command did two things at once, and only one of them was ever about you.

It built an image containing the **miner code**, and that code then started a
**second** container to do the actual training — openpi needs `numpy<2.0` while
bittensor needs `numpy>=2.0`, so training has always run in its own container.

Now the CLI runs on the host (`pip install openroboto`) and starts the training
container for you. `openroboto train` is the whole replacement:

```bash
openroboto build     # build the training image, once
openroboto train     # runs it, with your data and strategy mounted in
```

The training image definition ships inside the package, so there is nothing to
clone and nothing to keep in sync. You still need Docker on the host — that has
not changed and cannot.

If what you actually wanted was to keep the CLI itself off your host Python,
that is still possible but it is a repository-level thing, not a miner
workflow — see "Running the CLI in a container" in the repository README.

## `miner.yaml` changes

**Your existing `miner.yaml` still works.** No field was renamed — that was a hard
constraint, because renaming a key silently breaks every miner's file.

Two things to know:

1. **The flat `[DEFAULT]` / `key = value` form is not supported** and never really
   was by this parser. It fails *quietly*: the file loads, every field falls back to a
   default, and the first symptom is an unrelated complaint about a missing `netuid`.
   If your file looks like that, run `openroboto init` into a scratch directory and
   copy your values into the nested layout.

2. **`payment.burn_rate_tao` is optional and normally should be absent.** The fee comes
   from the subnet's `control.json`. If you hard-coded it, delete it — a stale local
   value is how you burn the wrong amount, and a wrong amount is rejected with no
   refund.

Run this after any edit:

```bash
openroboto doctor      # names every field that is missing or unusable
```

## If you are mid-round right now

`state/round_N.json` is unchanged and still read. Finish the round with the new
commands:

- **Trained but not submitted** → `openroboto check`, then `openroboto submit`.
- **Burned but not announced** → `openroboto announce --round N`. It reuses the burn
  recorded in your state file; **do not burn again**. Note the burn→commitment window
  is 50 blocks (~10 minutes) — if more time has passed, `announce` will tell you the
  burn has expired rather than charging you another fee for a submission that would be
  rejected.
- **Announced** → `openroboto status`.

## Behaviour that changed on purpose

These are not renames; the CLI now does something different, and in each case the old
behaviour could cost you TAO:

| | Old | Now |
|---|---|---|
| Fee cannot be fetched | Fell back to a built-in `0.01`, while the network publishes `0.1` — you burned a tenth of the fee and were rejected | **Refuses to burn.** No built-in amount to fall back to |
| Burn is too old to attach | Announced anyway; the backend rejected it and the fee was gone | `announce` **refuses**, and says how many blocks have passed |
| Commitment submitted | Printed a block reference even when nothing had confirmed | Waits for inclusion. Prints a block reference **only** when the chain returned one; otherwise says so and tells you to check `openroboto status` before retrying |

## Getting the old files back

Nothing is lost — they are in this repository's git history:

```bash
git log --diff-filter=D --oneline -- rt.py     # find the deleting commit
git show <commit>^:rt.py > rt.py               # recover one file
```

They are unmaintained from 2026-08-19 and will not receive fixes.
