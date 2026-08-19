# Miner Guide — π₀.₅ LIBERO Training

> **Status**: current · **Updated**: 2026-08-19 · **Audience**: miners
> **Scope**: Nothing → first submission. Architecture, quick start, chain payload, confirmation outcomes.
> **Note**: Deployment on a real machine is [MINER_DEPLOY.md](./MINER_DEPLOY.md); field-by-field config is [CONFIG.md](./CONFIG.md).

> For miners participating in the RobotTrain subnet.

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
│  │                  Public API                          │    │
│  │  /api/rank   /api/scores   /api/weights              │    │
│  │  /api/miner  /api/export  /api/v1/*                  │    │
│  └──────────────────────────────────────────────────────┘    │
└──────────┬───────────────────────────────────────────────────┘
           │
    ┌──────▼────────────┐
    │  openroboto train │
    │  ① Fetch control  │
    │  ② Download data  │
    │  ③ LoRA train     │
    │  → saves state    │
    └────────┬──────────┘
             │
    ┌────────▼──────────┐
    │ openroboto submit │
    │  ④ Upload → HF    │
    │  ⑤ Burn (payment) │
    │  ⑥ Announce chain │
    └───────────────────┘
```

**Two-stage workflow**: `openroboto train` does prep + training. After it
completes, `openroboto submit` does upload → burn → announce.

Run `openroboto doctor` before the first round and `openroboto check` before
paying — both exist so that "burned TAO, then found out the model was wrong"
stops happening.

**Backend auto-scans chain**: Backend runs `ChainScanner` + `ScannerLoop` (polls every 60s), discovers miner submissions, verifies burns, computes seeds, and queues for evaluation.

## Quick Start

```bash
# 1. Install — no repo clone needed
python3.11 -m venv .venv && source .venv/bin/activate
pip install openroboto

# 2. Configure: writes miner.yaml + train_strategy.py
openroboto init my-miner && cd my-miner
#    Required in miner.yaml: subnet.hotkey_ss58, huggingface.token,
#    huggingface.username, urls.control_json, subnet.wallet_password

# 3. Check the environment before anything costs money
openroboto doctor

# 4. Build the training image, then train one round
openroboto build
openroboto train

# 5. Validate the model locally — still free at this point
openroboto check

# 6. Upload → Burn → Announce
openroboto submit

# 7. See what the backend made of it
openroboto status
```

> **⚠️ Burn→announce window.** The backend rejects any submission whose burn tx is more than **50 blocks (~10 minutes)** away from the chain commitment. This is an anti-replay rule: a fee cannot be paid once and attached to a later submission. `openroboto submit` runs upload → burn → announce back-to-back precisely so you stay inside this window. If you run `openroboto burn` and `openroboto announce` separately and the gap exceeds 50 blocks, the submission is rejected and the burned TAO is **not refunded** — `announce` will refuse to submit rather than let you pay a commitment fee for a submission that is already doomed.

The CLI pulls `control.json` via **HTTP direct link** (ETag cached), no R2 SDK
dependency. `openroboto submit` handles the post-training pipeline, reading the
wallet password from `miner.yaml`.

The evaluation fee comes from `control.json` and from nowhere else. If it cannot
be fetched, `burn` **refuses to run** instead of falling back to a guess — an
amount that does not match is rejected by the backend, and the TAO is not
refunded.

## Chain Submission Format

JSON payload committed on chain (BigRaw):

```json
{"s": "5Hotkey...ss58", "h": "<block hash at submit time>", "c": "<hf commit sha>", "r": 1, "i": "<hf_user>/<repo>", "b": "<burn tx hash>", "bb": 8700000}
```

| Field | Description |
|-------|-------------|
| `s` | Miner hotkey SS58 |
| `h` | Chain block hash at submission (seed reveal input) |
| `c` | HF commit hash (pins the exact model revision) |
| `r` | Round number |
| `i` | HF repo id (`user/repo`) |
| `b` | Burn tx hash (payment proof, bound to this submission) |
| `bb` | Burn block number (must be within 50 blocks of the commitment) |

## Chain Submission Confirmation

`openroboto submit` runs upload → burn → announce in sequence and reports the
outcome of each step:

1. `announce` builds the payload (including `block_hash` for the seed reveal) and
   publishes it as a chain commitment, **waiting for inclusion in a block**
2. Confirmed → `✅ commitment 已上链 | ref=<block>-<index> fee=… TAO`
3. State is saved to `state/round_N.json` — re-running skips completed steps

**The CLI distinguishes three outcomes, and they are not the same thing:**

| What you see | What it means | What to do |
|---|---|---|
| `✅ commitment 已上链 \| ref=<block>-<index>` | In a block. The block reference is real. | Nothing. Check `openroboto status`. |
| `✅ commitment 已提交` + `⚠️ SDK 没给回区块号` | Submitted, but the SDK returned no block number. Probably fine. | Confirm with `openroboto status`. |
| `❌ commitment 没有确认上链` | We do not know. It may still land. | **Do not burn again.** Run `openroboto status` first; only re-run `announce` if the backend never received it. |

A block reference is only ever printed when the chain actually returned one. If
you see `未确认`, no block number is being invented to reassure you.

**Resume support**: if a step fails, re-run `openroboto submit` — it resumes from
the last completed step and **reuses the existing burn rather than paying twice**.

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

- **HTTP direct links** — Miner/Backend pulls control.json and datasets via HTTP GET, no R2 SDK
- **HF token needs write access** — For uploading models to personal repo
- **Chain Commitments API** — Data persists on chain, auto-verified after submission
- **Burn hash strict exact match** — Backend verifies burn tx using strict exact match (no `startswith` prefix matching), preventing false positives from truncated hashes
- **Anti-plagiarism** — Backend computes LFS fingerprint (`repo_hash`) for each submission; same hash from different hotkey → rejected. The hash is stored even for rejected submissions for auditability.