# Miner Guide — π₀.₅ LIBERO Training

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
    │    MINER          │
    │  miner.py Steps 1-2│
    │  ① Fetch control  │
    │  ② Download data  │
    │  ③ LoRA train     │
    │  → saves state    │
    └────────┬──────────┘
             │
    ┌────────▼──────────┐
    │    rt.py          │
    │  Steps 3-5        │
    │  ④ Upload → HF    │
    │  ⑤ Burn (payment) │
    │  ⑥ Announce chain │
    └───────────────────┘
```

**Two-stage workflow**: `miner.py` handles Steps 1-2 (prep + training). After training completes, run `rt.py submit` for Steps 3-5 (upload → burn → announce).

**Backend auto-scans chain**: Backend runs `ChainScanner` + `ScannerLoop` (polls every 60s), discovers miner submissions, verifies burns, computes seeds, and queues for evaluation.

## Quick Start

```bash
# 1. Install
uv venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt

# 2. Configure (copy miner.example.yaml → miner.yaml)
#    Required: hotkey_ss58, hf_token, hf_username, control_json_url, wallet_password

# 3. Train (Steps 1-2: prep + training, then exits)
python miner.py --config miner.yaml

# 4. Upload → Burn → Announce (Steps 3-5)
python rt.py submit --config miner.yaml
```

> **⚠️ Burn→announce window.** The backend rejects any submission whose burn tx is more than **50 blocks (~10 minutes)** away from the chain commitment. This is an anti-replay rule: a fee cannot be paid once and attached to a later submission. `openroboto submit` runs upload → burn → announce back-to-back precisely so you stay inside this window. If you run `openroboto burn` and `openroboto announce` separately and the gap exceeds 50 blocks, the submission is rejected and the burned TAO is **not refunded** — `announce` will refuse to submit rather than let you pay a commitment fee for a submission that is already doomed.

Miner pulls `control.json` via **HTTP direct link** (ETag cached), no R2 SDK dependency.
`rt.py` handles the post-training pipeline with wallet password from miner.yaml.

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

`rt.py` handles chain submission and logs confirmation immediately:

1. `rt.py submit` runs upload → burn → announce in sequence
2. Step 5 (announce) calls `submit_hf_model_announcement` with block_hash for reveal
3. On success → logs `✅ Commitment submitted | block=N ext=0x...`
4. State is saved to `state/round_N.json` — re-running skips completed steps

**Resume support**: If a step fails, re-run `rt.py submit` and it resumes from where it left off.

## bittensor 10.x Data Decoding

`get_commitment_metadata` returns `info.fields` as nested tuple of integers:

```python
{'deposit': 0, 'block': 7496925, 'info': {'fields': (({'Raw73': ((123, 34, 104, ...)},)},)}}
```

Or as hex string:

```python
{'info': {'fields': [{'Raw73': '0x7b2268223a...'}]}}
```

`_decode_raw()` in `utils/chain.py` handles all formats:
1. Iterate `fields` → find `RawXX` key
2. Flatten nested tuple → `bytes(flat)` OR decode hex string
3. UTF-8 decode → JSON parse

ChainScanner (`backend/chain_scanner.py`) uses the same `_decode_raw()`.

## Security Notes

- **HTTP direct links** — Miner/Backend pulls control.json and datasets via HTTP GET, no R2 SDK
- **HF token needs write access** — For uploading models to personal repo
- **Chain Commitments API** — Data persists on chain, auto-verified after submission
- **Burn hash strict exact match** — Backend verifies burn tx using strict exact match (no `startswith` prefix matching), preventing false positives from truncated hashes
- **Anti-plagiarism** — Backend computes LFS fingerprint (`repo_hash`) for each submission; same hash from different hotkey → rejected. The hash is stored even for rejected submissions for auditability.