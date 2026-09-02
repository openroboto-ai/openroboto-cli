# Changelog

Every entry answers one question before it lists anything: **does a miner have to
change something, and what breaks if they do not?** A diff summary without "who
has to act" is not usable by the people who installed this.

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
