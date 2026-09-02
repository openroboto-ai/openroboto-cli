# Miner Guide — π₀.₅ LIBERO Training

> **Status**: current · **Updated**: 2026-09-02 · **Audience**: miners
> **Scope**: Nothing → first submission. Architecture, quick start, chain payload, confirmation outcomes.
> **Note**: Deployment on a real machine is [MINER_DEPLOY.md](./MINER_DEPLOY.md); field-by-field config is [CONFIG.md](./CONFIG.md).

> For miners participating in the OpenRoboto subnet (Bittensor netuid 80).

> **🔴 The π0.5 simulation season was archived on 2026-08-31.** The current
> simulation season runs on LingBot-VLA 2.0 — read
> [MINER_LINGBOT.md](./MINER_LINGBOT.md) for it. Most of this page still applies
> to any season — the fee, the chain announcement, the repository naming, the
> payment→announce window — but the checkpoint layout and where the base model
> comes from do not, and following the π0.5 rules there gets your upload rejected
> after you pay.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    BACKEND API (:8001)                        │
│                                                              │
│  ┌──────────────────┐  ┌──────────────────┐                  │
│  │  Evaluator       │  │   ScannerLoop    │                  │
│  │  (benchmark API) │  │   (scan + seed)  │                  │
│  └──────┬───────────┘  └────────┬─────────┘                  │
│         │                      │                             │
│  ┌──────────────────────────────────────────────────────┐    │
│  │            Public read-only API                      │    │
│  │  /api/v1/competitions   /api/v1/submissions/history  │    │
│  │  /api/v1/scan-rejections   /api/v1/weights           │    │
│  └──────────────────────────────────────────────────────┘    │
└──────────┬───────────────────────────────────────────────────┘
           │  asked once, by `openroboto init`
    ┌──────▼──────────────┐
    │ miner.yaml           │  the season's whole spec, on disk
    │ `competition:`       │
    └──────┬──────────────┘
           │
    ┌──────▼────────────┐
    │  openroboto train │
    │  ① Read the season│  (offline; from miner.yaml)
    │  ② Download data  │
    │  ③ Train          │
    │  → saves state    │
    └────────┬──────────┘
             │
    ┌────────▼───────────────┐
    │ openroboto submit      │
    │  ④ Upload → HF         │
    │  ⑤ Re-check the season │
    │  ⑥ Layout gate         │
    │  ⑦ Pay the entry fee   │
    │  ⑧ Announce on chain   │
    └────────────────────────┘
```

**Two-stage workflow**: `openroboto train` does prep + training. After it
completes, `openroboto submit` does upload → check the layout → pay → announce.

Run `openroboto doctor` before you start and `openroboto check` before
paying — both exist so that "paid the entry fee, then found out the model was
wrong" stops happening.

`openroboto submit` judges the layout itself as well, between the upload and the
payment, so skipping `openroboto check` does not mean skipping the rules. It
reads the file listing of your HuggingFace repository — the same listing the
subnet reads after the fee — and stops without paying if that listing would not
earn a score. There is no flag to switch it off: past that point a rejection is
final and the TAO is not refunded. Running `openroboto check` first is still
worth it, because it is free, it runs *before* the multi-gigabyte upload, and it
checks two rules that need the weight index file on your disk.

**Backend auto-scans chain**: Backend runs `ChainScanner` + `ScannerLoop` (polls every 60s), discovers miner submissions, verifies burns, computes seeds, and queues for evaluation.

## Quick Start

```bash
# 1. Install — no repo clone needed
python3.11 -m venv .venv && source .venv/bin/activate
pip install openroboto

# 2. Pick a competition and create the workspace for it
#    `init` asks the backend which seasons are open and writes the one you
#    pick into miner.yaml. Every later command reads that file — only `init`
#    and `submit` go to the network.
openroboto init my-miner && cd my-miner
#    Then fill in, in miner.yaml:
#      subnet.hotkey_ss58        your hotkey (the HF repo name derives from it)
#      subnet.wallet_password    optional; skips the interactive prompt
#      huggingface.token         a write token
#      huggingface.username

# 3. Check the environment before anything costs money
openroboto doctor

# 4. Build the training image, then train
openroboto build
openroboto train

# 5. Validate the model locally — still free at this point
openroboto check

# 6. Upload → judge the layout → confirm the season → pay → announce
#    One command, in that order. It prints which season, how long it has
#    left, how much and to whom, then asks y/N — and everything that can be
#    refused is refused *before* the money moves.
openroboto submit

# 7. See what the backend made of it
openroboto status
```

### Testing against something other than mainnet

`environment` picks the subnet and the backend together — they are one decision,
and configuring only half of it is how you burn mainnet TAO at the dev fee rate,
or submit to testnet and then ask production why nothing showed up.

```yaml
environment: mainnet   # default: netuid 80, real TAO
environment: dev       # testnet 313, faucet TAO
environment: local     # your own backend; you must give backend.url
```

`openroboto doctor` reports a mismatched combination, and `submit` refuses to
run on one. Full table and the local-backend example:
[CONFIG.md](./CONFIG.md#environment--one-name-for-four-coupled-settings).

> **⚠️** The backend rejects a payment more than **50 blocks (~10 min)** from
> the chain commitment — an anti-replay rule. `submit` pays and announces
> back-to-back so you stay inside it.

The CLI reads the season's spec out of `miner.yaml`, written there by `init`. It
no longer reads `control.json` at all — external validators still do, for
`public_key` only. `openroboto submit` handles the post-training pipeline,
reading the wallet password from `miner.yaml`.

The evaluation fee comes from the season you are entering
(`competition.params.fee` in `miner.yaml`) and from nowhere else — not from
`control.json`, whose `payment` block is one rate for a subnet that runs several
seasons at once, and not from anything typed into `miner.yaml`. `openroboto
submit` confirms it against the backend in the moment before paying; a workspace
with no `competition` section is refused rather than charged a guess. An amount
that does not match is rejected by the backend, and the TAO is not refunded.

## Two tracks, one flow

The subnet runs several seasons at once. `openroboto init` lists the open ones,
you pick one, and it is written into `miner.yaml`; every later command reads it
off disk. One workspace mines one season — for another, run `init` again in a
new directory.

The tracks differ in three things and nothing else:

| | Simulation | Real robot |
|---|---|---|
| Evaluated on | LIBERO, our GPUs | an xArm 6 in our workshop |
| Fee | burned | transferred to the season's published address |
| Layout judged before paying | yes | no — the real track's base model is not fixed yet |
| Rewards | validator weight to the reigning champion | a per-season prize pool with a claim period — [REAL_TRACK.md](./REAL_TRACK.md) |

`openroboto submit` reads the season's `params.fee` and pays accordingly. You do
not choose, and there is no separate command for it.

### What `submit` does

Upload → judge the layout → confirm the season is open → check the fee against
the backend → check this commit is not already entered → show you the season,
the deadline, the amount and the payee, and ask `y/N` → pay → announce.

Everything that can refuse refuses before the money moves. If a step refuses,
nothing was spent and the upload is still there.

## Chain Submission Format

JSON payload committed on chain (BigRaw):

```json
{"s": "5Hotkey...ss58", "h": "<block hash at submit time>", "c": "<hf commit sha>", "r": 1, "i": "<hf_user>/<repo>", "b": "<payment tx hash>", "bb": 8700000, "cid": 3}
```

| Field | Description |
|-------|-------------|
| `s` | Miner hotkey SS58 |
| `h` | Chain block hash at submission (seed reveal input) |
| `c` | HF commit hash (pins the exact model revision) |
| `r` | Round number. 🔴 Not the seed input — that is `cid`, see [SEED_GENERATION.md](./SEED_GENERATION.md) |
| `i` | HF repo id (`user/repo`) |
| `b` | Payment tx hash (burn or transfer; bound to this submission) |
| `bb` | Payment block number (must be within 50 blocks of the commitment) |
| `cid` | Competition id, resolved from the backend at submit time. Added in protocol 0.7.0 |
| `m` | Model fingerprint. Real track only — its repositories may be private, so the evaluator cannot compute it later |

## Chain Submission Confirmation

`openroboto submit` runs upload → pay → announce in sequence and reports the
outcome of each step:

1. the announcement step builds the payload (including `block_hash` for the seed reveal) and
   publishes it as a chain commitment, **waiting for inclusion in a block**
2. Confirmed → `✅ commitment on chain | ref=<block>-<index> fee=… TAO`
3. State is saved to `state/competition_<id>.json` — re-running skips completed steps

**The CLI distinguishes three outcomes, and they are not the same thing:**

| What you see | What it means | What to do |
|---|---|---|
| `✅ commitment on chain \| ref=<block>-<index>` | In a block. The block reference is real. | Nothing. Check `openroboto status`. |
| `✅ commitment submitted` + `⚠️ The SDK returned no block number` | Submitted, but the SDK returned no block number. Probably fine. | Confirm with `openroboto status`. |
| `❌ The commitment was not confirmed on chain` | We do not know. It may still land. | **Do not pay again.** Run `openroboto status` first; if the backend never received it, re-run `openroboto submit` — it resumes from the checkpoint and sends only the commitment, without re-uploading or paying again. |

A block reference is only ever printed when the chain actually returned one. If
you see `not confirmed`, no block number is being invented to reassure you.

**Resume support**: if a step fails, re-run `openroboto submit` — it resumes from
the last completed step and **reuses the fee already paid rather than paying twice**.

## bittensor 10.x Data Decoding

`get_commitment_metadata` returns `info.fields` as nested tuple of integers:

```python
{'deposit': 0, 'block': 7496925, 'info': {'fields': (({'Raw73': ((123, 34, 104, ...)},)},)}}
```

Or as hex string:

```python
{'info': {'fields': [{'Raw73': '0x7b2268223a...'}]}}
```

Decoding handles all of these shapes:
1. Iterate `fields` → find the `RawXX` key
2. Flatten the nested tuple → `bytes(flat)`, or decode the hex string
3. UTF-8 decode → JSON parse

**The miner and the backend run the same decoder**, from
`openroboto_protocol.commitment` — not two copies. That is deliberate: when these
drifted apart, miners encoded payloads the backend could not read.

`RawN` versus `BigRaw` is only a **byte-length** distinction (`≤128` uses `RawN`),
not a client version. A `Raw119` commitment is not an outdated miner.

## Security Notes

- **HTTP direct links** — datasets are fetched over plain HTTP GET, no R2 SDK. (`control.json` is no longer read by any miner command; external validators still fetch it for `public_key`.)
- **HF token needs write access** — For uploading models to personal repo
- **Chain Commitments API** — Data persists on chain, auto-verified after submission
- **Burn hash strict exact match** — Backend verifies burn tx using strict exact match (no `startswith` prefix matching), preventing false positives from truncated hashes
- **Anti-plagiarism** — Backend computes LFS fingerprint (`repo_hash`) for each submission; same hash from different hotkey → rejected. The hash is stored even for rejected submissions for auditability.
