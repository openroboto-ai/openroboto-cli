# RobotTrain Backend API Reference (English)

## Overview

- **Base URL**: `http://<host>:<port>` (default `http://localhost:8001`)
- **Authentication**: Three-tier system — see below
- **Response Format**: `application/json`, `snake_case` fields
- **Timestamps**: ISO 8601 UTC
- **CORS**: Enabled (`Access-Control-Allow-Origin: *`)
- **Caching**: `Cache-Control: max-age=15`
- **Rate Limiting**: 30 requests per minute per IP

### Authentication Tiers

| Tier | Header | Source | Endpoints |
|------|--------|--------|-----------|
| **No auth** | — | — | `GET /health` |
| **public_key** (read-only) | `X-API-Key: ***` | `control.json` → `public_key` | All read-only endpoints listed below |
| **admin_key** (write/management) | `X-API-Key: ***` | `backend.yaml` → `admin_key` | `POST /api/v1/benchmark/task/{id}/score`, `PATCH /api/v1/benchmark/task/{id}/status`, `GET /api/v1/payments/*` |

---

## Complete Endpoint List

### 1. Health (no auth)

#### GET /health

Liveness probe.

```bash
curl http://localhost:8001/health
```

**200**:
```json
{"status": "ok", "timestamp": "2026-07-27T16:00:00.000Z"}
```

---

### 2. Rounds & Leaderboard (public_key)

#### GET /api/v1/rounds/current

Current active round.

```bash
curl -H "X-API-Key: ***" http://localhost:8001/api/v1/rounds/current
```

**200**:
```json
{
  "round": {
    "id": 1, "label": "Round 01", "status": "live",
    "network": "finney",
    "base_model": {"name": "pi0.5", "hf_repo": "openroboto-ai/pi05-libero-pytorch", "revision": ""},
    "started_at": null, "ends_at": null, "submission_count": 3,
    "champion": {
      "miner_hotkey": "5MinerAexamp...", "model_name": "pi05-AAAAAAAAAAAA",
      "score": 0.847, "delta_vs_prev_champion": 0.029, "settled_at": "2026-07-06T14:40:00Z"
    }
  }
}
```

---

#### GET /api/v1/rounds?limit=8

Round history.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 8 | Number of recent rounds |

```bash
curl -H "X-API-Key: ***" "http://localhost:8001/api/v1/rounds?limit=8"
```

**200**:
```json
{
  "summary": {"rounds_settled": 0, "cumulative_improvement": 0},
  "rounds": [{"id": 1, "label": "Round 01", "status": "live", "champion": null}]
}
```

---

#### GET /api/v1/leaderboard?round_id=0&limit=50&offset=0

Rankings. Challenge-based: challenger avg_score > king avg_score + CHAMPION_MARGIN (0.01).

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `round_id` | int | current | 0 = current round |
| `limit` | int | 50 | Page size |
| `offset` | int | 0 | Offset |

```bash
curl -H "X-API-Key: ***" "http://localhost:8001/api/v1/leaderboard?round_id=0&limit=50"
```

**200**:
```json
{
  "round_id": 1, "generated_at": "2026-07-17T08:00:00Z",
  "baseline": {"model_name": "pi0.5", "hf_repo": "openroboto-ai/pi05-libero-pytorch", "revision": "", "score": {"mean": 0.502917, "std": 0.001, "trials": 3}},
  "total": 1,
  "rows": [{
    "rank": 1, "submission_id": "task_5MinerAexamp..._1",
    "miner": {"hotkey": "5MinerAexamp...", "display_name": "5MinerAexamp..."},
    "model": {"name": "pi05-AAAAAAAAAAAA", "hf_repo": "miner-a/pi05-AAAAAAAAAAAA", "revision": "aaaaaaaa"},
    "score": {"mean": 0.847, "std": null, "trials": 1},
    "delta_vs_base": 0.242, "tasks_passed": {"passed": 0, "total": 40},
    "status": "champion", "submitted_at": "2026-07-17T08:06:00Z",
    "audit": {"score_json_url": "", "logs_url": "", "env_hash": ""}
  }]
}
```

---

#### GET /api/rank

Top-3 challenge ranking (compact).

```bash
curl -H "X-API-Key: ***" http://localhost:8001/api/rank
```

**200**: `[{hotkey, avg_score, rank, status}, ...]`

---

### 3. Submissions & Scores (public_key)

#### GET /api/v1/submissions/{submission_id}

Submission detail (audit endpoint, v1 API).

```bash
curl -H "X-API-Key: ***" http://localhost:8001/api/v1/submissions/task_5MinerAexamp..._1
```

**200**: Full submission record with `stage`, `detail`, `worker_id` fields.

---

#### GET /api/submission/{task_id}

Submission detail (legacy endpoint).

```bash
curl -H "X-API-Key: 54321" http://localhost:8001/api/submission/task_5MinerBexampleexampleexampleexampleeBBBBBBBBBBBB_1
```

**200**: Single submission record with `result` field containing the full `env_scores` array.

```json
{
  "id": 3, "task_id": "task_xxx_1", "hotkey": "5MinerBe...",
  "round_num": 1, "status": "done",
  "hf_repo_id": "miner-b/pi05-BBBBBBBBBBBB", "hf_commit": "bbbbbbbb",
  "result": {
    "task_id": "task_xxx_1", "success": true,
    "total_score": 0.5179,
    "env_scores": [
      {"env_name": "libero_spatial", "score": 0.66, "samples": 400, "duration_sec": 2924.5},
      {"env_name": "libero_object", "score": 0.6575, "samples": 400, "duration_sec": 3455.9},
      {"env_name": "libero_goal", "score": 0.51, "samples": 400, "duration_sec": 3752.7},
      {"env_name": "libero_10", "score": 0.32, "samples": 400, "duration_sec": 7324.4},
      {"env_name": "libero_object_swap", "score": 0.53, "samples": 100, "duration_sec": 971.4},
      {"env_name": "libero_spatial_swap", "score": 0.43, "samples": 100, "duration_sec": 858.2}
    ],
    "error": "", "duration_sec": 947.8
  },
  "submitted_at": "2026-07-27T14:00:00Z"
}
```

---

#### GET /api/submission/history/{hotkey}/{round_num}

Full submission history (all statuses). Returns `stage`, `detail` (parsed from `eval_detail` JSON), and `worker_id` fields via `LEFT JOIN submissions`.

```bash
curl -H "X-API-Key: ***" http://localhost:8001/api/submission/history/5MinerAexamp.../1
```

**200**:
```json
[{
  "id": 3, "task_id": "task_5MinerAexamp..._1", "hotkey": "5MinerAexamp...",
  "round_num": 1, "status": "pending",
  "hf_repo_id": "miner-a/pi05-AAAAAAAAAAAA", "hf_commit": "aaaaaaaa",
  "commit_block": 7577046, "seed": 1759347728,
  "block_hash": "f874d5ed4e14113d...",
  "drand_random": "85eeb23277d6ba2b...", "drand_round": 6294819,
  "model_hash": "", "env_list": "[\"libero_spatial\",\"libero_object\",\"libero_goal\",\"libero_10\"]",
  "stage": null, "detail": null, "worker_id": null,
  "submitted_at": "2026-07-17T08:06:51Z"
}]
```

---

#### GET /api/v1/submissions/history

All submissions history (paginated, public_key). Returns `stage`, `detail`, `worker_id`.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `hotkey` | string | — | Filter by hotkey |
| `round_num` | int | — | Filter by round |
| `limit` | int | 50 | Page size |
| `offset` | int | 0 | Offset |

---

#### GET /api/scores

All miner scores (hotkey → avg_score map).

```bash
curl -H "X-API-Key: ***" http://localhost:8001/api/scores
```

**200**: `{"<hotkey>": <avg_score>, ...}`

---

#### GET /api/miner/{hotkey}

Single miner status.

```bash
curl -H "X-API-Key: ***" http://localhost:8001/api/miner/5MinerAexamp...
```

**200**: Submission record for the hotkey.

---

### 4. Eval Queue (public_key)

#### GET /api/tasks

Eval queue (same data as `/api/pending-tasks`).

```bash
curl -H "X-API-Key: ***" http://localhost:8001/api/tasks
```

**200**:
```json
[{
  "id": 1, "task_id": "task_5MinerAexamp..._1", "uid": 10,
  "hotkey": "5MinerAexampleexampleexampleexampleeAAAAAAAAAAAA",
  "hf_repo_id": "miner-a/pi05-AAAAAAAAAAAA",
  "hf_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "round_num": 1, "commit_block": 7577046,
  "env_list": "[\"libero_spatial\",\"libero_object\",\"libero_goal\",\"libero_10\"]",
  "status": "pending", "seed": 1759347728,
  "block_hash": "f874d5ed4e14113df54fd46ba1c9ab3863ff4bec3d13ff67ef13430477ce4014",
  "drand_random": "85eeb23277d6ba2bb50fffebff719c6194371d0a4f05893da7149e060c708009",
  "drand_round": 6294819, "model_hash": "",
  "submitted_at": "2026-07-17T08:06:51Z",
  "created_at": "2026-07-17T08:06:51Z", "updated_at": "2026-07-17T08:06:53Z", "miner_uid": 0
}]
```

---

#### GET /api/pending-tasks

Pending tasks (Benchmark Worker polling). Same data as `/api/tasks`.

```bash
curl -H "X-API-Key: ***" http://localhost:8001/api/pending-tasks
```

---

#### GET /api/v1/benchmark/queue

Benchmark queue (public version). Uses `public_key` auth.

```bash
curl -H "X-API-Key: ***" http://localhost:8001/api/v1/benchmark/queue
```

**200**: `{"queue_size": 1, "tasks": [...]}`

---

#### GET /api/v1/queue/status

Eval queue status (internal counters). Uses unified status values (`evaluated`, `eval_failed`, `rejected`).

```bash
curl -H "X-API-Key: ***" http://localhost:8001/api/v1/queue/status
```

**200**: Queue status counters with legacy status mapping (`done`→`evaluated`, `failed`→`eval_failed`).

---

#### GET /api/v1/scan-rejections

Submissions rejected at the scanner stage (payment verification failed) — no API key required. **If your commitment landed on chain but your task never appears in the queue, check here first**: it returns the exact `reject_reason` (e.g. `burn_tx_too_old: block_diff=45 exceeds window=10`, `amount mismatch`, `burn tx replay`).

Query params: `hotkey`, `uid`, `round_num`, `limit` (default 200), `offset`.

```bash
curl "https://api.openroboto.ai/api/v1/scan-rejections?hotkey=<your_hotkey_ss58>"
```

**200**:

```json
{
  "success": true,
  "rejections": [
    {
      "uid": 61,
      "hotkey": "5Miner...ss58",
      "round_num": 1,
      "hf_repo_id": "user/pi05-XXXXXXXXXXXX",
      "commit_block": 8736712,
      "burn_block": 8736667,
      "burn_tx_hash": "4449a27c...",
      "reject_reason": "burn_tx_too_old: block_diff=45 exceeds window=10",
      "created_at": "2026-07-31T04:01:00+00:00"
    }
  ],
  "total": 1,
  "limit": 200,
  "offset": 0
}
```

Note: a rejected burn is **not refunded**. Fix the cause and re-submit with a fresh burn via one-shot `openroboto submit`.

---

### 5. Weights & Export (public_key)

#### GET /api/weights

Current weights (normalized, sum=1.0, u16-ready).

```bash
curl -H "X-API-Key: ***" http://localhost:8001/api/weights
```

**200**: `{"<hotkey>": <weight>, ...}`

---

#### GET /api/export

Full export.

```bash
curl -H "X-API-Key: ***" http://localhost:8001/api/export
```

**200**: `{"submissions": [...], "pending_tasks": [...], "weights": {...}}`

---

#### GET /api/v1/benchmark/meta

Benchmark metadata.

```bash
curl -H "X-API-Key: ***" http://localhost:8001/api/v1/benchmark/meta
```

**200**:
```json
{
  "name": "LIBERO Synthetic v0 Benchmark", "version": "v0.1.0",
  "phase": "launch_prep", "updated_at": "2026-07-01T00:00:00Z", "maintainer": "OpenRoboto Core",
  "spec": {
    "suite": "LIBERO-pro v0", "tasks_per_round": 40, "trials_per_task": 3,
    "sim_engine": "MuJoCo 3", "timestep_ms": 2,
    "control": "7-DoF joint deltas + gripper @ 20 Hz",
    "observations": "wrist + front RGB 224, proprio @ 20 Hz"
  }
}
```

---

### 6. Benchmark Worker (admin_key)

#### GET /api/v1/benchmark/task/{task_id}/score

Check task readiness for score submission (admin_key required).

```bash
curl -H "X-API-Key: ***" http://localhost:8001/api/v1/benchmark/task/task_xxx_1/score
```

**200**:
```json
{"task_id": "task_xxx_1", "status": "ready_to_score"}
```

---

#### PATCH /api/v1/benchmark/task/{task_id}/status

Update benchmark task progress (admin_key required). Worker reports current stage for real-time visibility.

```bash
curl -X PATCH \
  -H "X-API-Key: ***" \
  -H "Content-Type: application/json" \
  -d '{"status": "downloading"}' \
  http://localhost:8001/api/v1/benchmark/task/task_xxx_1/status
```

**Request Body**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `status` | string | Yes | One of: `downloading`, `prechecking`, `evaluating` |

**200**:
```json
{"ok": true, "task_id": "task_xxx_1", "stage": "downloading"}
```

---

#### POST /api/v1/benchmark/task/{task_id}/score

Submit benchmark scores (admin_key required).

```bash
curl -X POST \
  -H "X-API-Key: ***" \
  -H "Content-Type: application/json" \
  -d '{
    "success": true,
    "miner_hotkey": "5MinerBexampleexampleexampleexampleeBBBBBBBBBBBB",
    "hf_repo_id": "miner-b/pi05-BBBBBBBBBBBB",
    "hf_commit": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "round_num": 1,
    "init_seed": 2475522565,
    "env_scores": [
      {"env_name": "libero_spatial", "score": 0.66, "samples": 400, "duration_sec": 2924.5, "error": ""},
      {"env_name": "libero_object", "score": 0.6575, "samples": 400, "duration_sec": 3455.9, "error": ""},
      {"env_name": "libero_goal", "score": 0.51, "samples": 400, "duration_sec": 3752.7, "error": ""},
      {"env_name": "libero_10", "score": 0.32, "samples": 400, "duration_sec": 7324.4, "error": ""},
      {"env_name": "libero_object_swap", "score": 0.53, "samples": 100, "duration_sec": 971.4, "error": ""},
      {"env_name": "libero_spatial_swap", "score": 0.43, "samples": 100, "duration_sec": 858.2, "error": ""}
    ],
    "per_task_scores": [],
    "total_score": 0.517917,
    "duration_sec": 947.8,
    "error": ""
  }' \
  http://localhost:8001/api/v1/benchmark/task/task_5MinerBexampleexampleexampleexampleeBBBBBBBBBBBB_1/score
```

**Request Body Fields**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `success` | bool | Yes | Whether eval succeeded |
| `miner_hotkey` | string | Yes | Miner hotkey |
| `hf_repo_id` | string | Yes | HF repo ID |
| `hf_commit` | string | Yes | HF commit hash |
| `round_num` | int | Yes | Round number |
| `init_seed` | int | No | Initial seed used for evaluation |
| `env_scores` | array | Required if success=true | Per-env scores — **must include all 6 environments** |
| `env_scores[].env_name` | string | Required if success=true | Env name |
| `env_scores[].score` | float | Required if success=true | Score [0,1] |
| `env_scores[].samples` | int | Required if success=true | Sample count (≥ 0) |
| `env_scores[].duration_sec` | float | Required if success=true | Duration (seconds) |
| `env_scores[].error` | string | No | Error message |
| `total_score` | float | Yes | Overall score (0 if success=false) |
| `duration_sec` | float | Yes | Total duration |
| `error` | string | Required if success=false | Error description |
| `per_task_scores` | array | No | Per-task detail scores |

**Two submission modes**:

**Mode A — Success (`success=true`)**:
- Must provide all 6 env_scores
- Backend validates: each env_name present, score in [0,1], samples ≥ 0
- Creates challenge attempt and writes all env_scores to `eval_scores` / `challenge_scores`
- submission status → `evaluated`

**Mode B — Failure (`success=false`)**:
- `env_scores` can be empty `[]`
- Must provide `error` with failure reason
- **No** env_scores validation
- **No** scores written to `eval_scores`
- **No** challenge created / **no** ranking entry
- submission status → `eval_failed`

**200**:
```json
{"ok": true, "task_id": "task_5MinerAexamp..._1"}
```

**Error Responses**:

| HTTP | Description |
|------|-------------|
| 400 | Invalid format / missing task_id / missing required env / score out of range |
| 401 | Invalid or missing admin_key |
| 413 | Payload too large (>50KB) |
| 429 | Rate limited (>30 req/min/IP) |

---

### 7. Benchmark Progress (admin_key)

#### POST /api/v1/benchmark-progress

Update task progress (stage, progress percentage, current env). Admin_key required.

```bash
curl -X POST \
  -H "X-API-Key: ***" \
  -H "Content-Type: application/json" \
  -d '{"task_id": "task_xxx_1", "stage": "running", "progress": 0.45, "current_env": "libero_spatial"}' \
  http://localhost:8001/api/v1/benchmark-progress
```

**Request Body**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | string | Yes | Task ID |
| `stage` | string | Yes | `downloading`, `prechecking`, or `running` |
| `progress` | float | No | Progress 0.0–1.0 |
| `current_env` | string | No | Current environment being evaluated |

**200**: `{"ok": true}`

---

### 8. Payments (admin_key)

#### GET /api/v1/payments?round_num=0

All payment records. `round_num=0` = all rounds.

```bash
curl -H "X-API-Key: ***" "http://localhost:8001/api/v1/payments?round_num=0"
```

**200**:
```json
{
  "success": true,
  "payments": [{
    "hotkey": "5MinerAexamp...", "round_num": 1,
    "tx_hash": "0x7e10d5b7d4d25c03...", "amount_tao": 0.01,
    "status": "confirmed",
    "created_at": "2026-07-17T08:06:44Z", "updated_at": "2026-07-17T08:06:51Z"
  }],
  "total": 1
}
```

---

#### GET /api/v1/payments/{hotkey}?round_num=0

Payment status for a specific miner.

```bash
curl -H "X-API-Key: ***" "http://localhost:8001/api/v1/payments/5MinerAexamp...?round_num=0"
```

**200**:
```json
{
  "success": true, "hotkey": "5MinerAexamp...", "round_num": 1,
  "status": "confirmed", "history": [...]
}
```

---

#### GET /api/v1/payments/summary?round_num=0

Payment summary.

```bash
curl -H "X-API-Key: ***" "http://localhost:8001/api/v1/payments/summary?round_num=0"
```

**200**:
```json
{
  "success": true,
  "summary": {"unpaid": 2, "pending": 0, "confirmed": 1, "rejected": 0, "total": 3},
  "round_num": 1
}
```

**Payment Status Values**:
| Status | Description |
|--------|-------------|
| `unpaid` | No burn tx submitted |
| `pending` | Burn tx submitted, awaiting chain confirmation |
| `confirmed` | Verified on-chain (tx exists + signer matches + amount matches + target hotkey matches) |
| `rejected` | Verification failed (signer/amount/hotkey mismatch, expired, replay) |

---

### 9. Deprecated

#### POST /api/eval → **410 Gone**

Use chain scanner or Benchmark Worker API.

---

### Allowed Environments

The 6 environments that must all be present in a successful score submission:

| Env Name | Description |
|----------|-------------|
| `libero_spatial` | Spatial reasoning tasks (400 samples) |
| `libero_object` | Object recognition tasks (400 samples) |
| `libero_goal` | Goal-oriented tasks (400 samples) |
| `libero_10` | Complex 10-step tasks (400 samples) |
| `libero_object_swap` | Object swap variant (100 samples) |
| `libero_spatial_swap` | Spatial swap variant (100 samples) |

---

### Submission Status Values (Unified Model)

| Status | Description | Terminal |
|--------|-------------|----------|
| `received` | Chain announcement seen | No |
| `burn_checking` | Burn verification in progress | No |
| `burn_passed` | Burn verified, awaiting seed | No |
| `burn_rejected` | Burn verification failed | **Yes** |
| `pending` | Enqueued, waiting for worker | No |
| `seed_failed` | Seed computation failed (drand unavailable), auto-retried | No |
| `evaluating` | Worker evaluating (stage: `downloading`/`prechecking`/`running`) | No |
| `evaluated` | Evaluation completed successfully | **Yes** |
| `eval_failed` | Evaluation failed | **Yes** |
| `rejected` | Rejected (plagiarism, model hash failure, validation failure) | **Yes** |

**Backward compatibility**: Old status values (`done` → `evaluated`, `failed` → `eval_failed`, `enqueued` → `pending`) are automatically mapped in API responses. The `sm` status mapping dictionaries in the database preserve old keys alongside new keys.