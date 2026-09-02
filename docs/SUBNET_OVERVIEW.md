# OpenRoboto Subnet — Protocol & Incentive Mechanism

> **Status**: current · **Updated**: 2026-09-02 · **Audience**: miners, external validators, auditors
> **Scope**: What the subnet rewards, and every rule that decides whether a submission counts.
> **Note**: Numbers named here (fee, round, dataset URLs) are published **per season on the
> competition row** — `GET /api/v1/competitions`, copied into your `miner.yaml` by
> `openroboto init`. Where this document names a number, that row wins.

**Bittensor subnet for open robot-learning models. Mainnet netuid 80.**

This document explains how the subnet works, end to end: what miners submit, what it costs, how evaluation is randomized and executed, how ranking and on-chain weights are derived, and what keeps the loop honest. Nothing here needs to be taken on trust — each mechanism leaves a public trace you can check yourself: a chain transaction, an API response, a Hugging Face commit, a drand beacon round.

---

## 1. What this subnet does

Miners fine-tune an open vision-language-action (VLA) base model — whichever one the season they enter names in `base_model_family`; **LingBot-VLA 2.0** for the current simulation season, **π0.5 ([openpi](https://github.com/Physical-Intelligence/openpi))** for the archived one — and publish their fine-tunes as **complete model checkpoints on Hugging Face**. Any training recipe is fine, LoRA included — but what you upload must be the full merged model, not a bare adapter (see §3 for the exact artifact requirements). The subnet evaluates every submission in simulation (LIBERO task suites in MuJoCo), ranks the results against the base-model baseline and each other, and pays miners through Bittensor emissions proportional to rank.

The output of the subnet is public: every submitted model is an open artifact anyone can download, and every score is reproducible from a deterministic, publicly verifiable seed.

## 2. Roles

| Role | Runs | Responsibility |
|---|---|---|
| **Miner** | `openroboto train` + `openroboto submit` | Fine-tune the season's base model (any recipe), export a full merged checkpoint, upload to own HF repo, pay the evaluation fee (burn or transfer, see §4), announce on chain |
| **Backend** | `backend/` service | Scan the chain for announcements, verify payment, derive seeds, manage the evaluation queue, compute rankings, serve the public API |
| **Benchmark worker** | separate GPU machine(s) | Poll the queue, load the pinned HF revision, run LIBERO suites in MuJoCo, push scores back (authenticated) |
| **Validator** | `openroboto validator run` | Read the settled ranking from the API, normalize, call `set_weights` on chain |
| **Owner** | `owner/tools/` | Open and close competitions. Each season's spec — fee, dataset, base model, training params — is published on its own row and served by `GET /api/v1/competitions` |

The backend is deliberately decoupled: miners never talk to it directly (they only write to the chain and Hugging Face), and validators only read from it.

## 3. Submission lifecycle

```
miner                      chain (netuid 80)                   backend + worker + validator
─────                      ──────────────────                  ────────────────────────────
fine-tune the season's base
merge → full checkpoint
upload to Hugging Face ──► pay the eval fee (burn or transfer)
                           commitment {repo, commit, competition id, payment tx}
                                        │
                                        ▼
                           ChainScanner (60 s poll) ──────────► payment verified? ──no──► burn_rejected (terminal)
                                                                    │ yes
                                                                    ▼
                                                                model_hash check (LFS fingerprint)
                                                                    │
                                                                    ├── plagiarism detected → rejected (terminal)
                                                                    └── OK → seed computation
                                                                        │
                                                                        ├── drand available → pending (enqueued)
                                                                        └── drand unavailable → seed_failed (retryable)
                                                                            │
                                                                            ▼
                                                                        retry next scan cycle
                                                                            │
                                                                            ├── success → pending
                                                                            └── failure → seed_retry_count++

                                                              worker evaluates (6 suites)
                                                              scores posted → evaluated / eval_failed
                                                              ranking resolved
                           set_weights ◄────────────────────────  validator reads ranking
```

A submission moves through these states (unified status model, defined in `backend/protocol/status.py`):

| Status | Meaning | Terminal |
|--------|---------|----------|
| `received` | Announcement seen on chain, not yet verified | No |
| `burn_checking` | Burn verification in progress | No |
| `burn_passed` | Burn verified, awaiting seed computation | No |
| `burn_rejected` | Burn verification failed | **Yes** |
| `pending` | Payment verified, task enqueued for evaluation | No |
| `seed_failed` | Seed computation failed (drand unavailable), auto-retried each scan cycle | No |
| `evaluating` | Worker is evaluating (stage: `claimed`/`downloading`/`prechecking`/`running`) | No |
| `evaluated` | Evaluation completed, scores recorded | **Yes** |
| `eval_failed` | Evaluation ran but failed (e.g. corrupted weights); no score, no ranking entry | **Yes** |
| `rejected` | Payment or format verification failed, or plagiarism detected; never enqueued | **Yes** |
| `superseded` | A newer submission for the same `(hotkey, competition)` pushed this one out | **Yes** |

**Old status mapping** (backward compatible): `done` → `evaluated`, `failed` → `eval_failed`, `enqueued` → `pending`, `waiting` → `evaluating`.

One commitment per hotkey per round. Re-submitting overwrites the previous entry and resets it to the start of the pipeline.

### What the uploaded repo must contain

The evaluation worker loads **complete model checkpoints only**. Before any GPU time is spent, every submission goes through a structural pre-check; a submission that fails it is marked `eval_failed`, and the rejection reason is recorded so the miner can see exactly why. The same rules are in `openroboto-protocol`, which is what `openroboto check` and the pre-payment gate in `openroboto submit` run — locally, for free, before any fee is paid.

🔴 **Which rule book applies follows the season's `base_model_family`**, not the adapter name and not this document. The π0.5 rules are below; LingBot-VLA 2.0 accepts a different tree, and `openroboto check` applies whichever one your workspace's season names.

| Requirement (π0.5 seasons) | Detail |
|---|---|
| Checkpoint format (one of) | openpi JAX: a `params/` directory (orbax OCDBT) · openpi PyTorch: a `model.safetensors` file |
| Normalization stats | `assets/physical-intelligence/libero/norm_stats.json` (state dim 8, action dim 7) |
| Architecture | Must match π0.5 (`pi05_libero` inference config); total parameter count within 2.5B–4.5B |
| **Not accepted** | **A bare LoRA adapter** (`adapter_config.json` + `adapter_model.safetensors`). The worker performs no merging — if you train with LoRA, merge the adapter back into the base and export the full checkpoint before uploading. There is no `openroboto merge`; the export belongs in your training script. |

The pre-check is pure CPU and public — run it yourself before paying the submission fee. There is no second repository to clone any more:

```bash
openroboto check /path/to/checkpoint
# exit 0 = will be accepted, exit 1 = would be rejected (reasons printed)
```

## 4. The evaluation fee

Every submission is preceded by a small payment. **How it is collected is the season's own data**, `params.fee.kind`, and there are exactly two kinds:

| `params.fee.kind` | On chain | Recipient |
|---|---|---|
| `burn` | `add_stake_burn` — alpha is bought and destroyed | none; the TAO ceases to exist |
| `transfer` | `Balances.transfer_keep_alive` into `params.fee.coldkey` | the address the season publishes |

The simulation seasons burn; the real-hardware track transfers. `openroboto submit` branches on the value the backend served seconds earlier — never on the track or the adapter — and refuses any third word rather than defaulting to the burn, because a burn on a `transfer` season pays nobody and leaves the submission unpaid with the TAO gone.

- **Why charge?** Each submission consumes real GPU hours (6 simulation suites per model). A per-submission fee makes flooding the queue economically irrational.
- **Why burn on the simulation track?** There is no recipient, so the operator earns nothing from those fees and has no incentive to farm submissions or sell evaluation slots.
- **How much?** Published on the competition row as `params.fee.amount_tao` — **0.1 TAO** on the simulation season, **2 TAO** on the real-hardware one. One number cannot serve both, which is why it is per season and not subnet-wide. An amount of 0 opens a free period (payment verification is skipped entirely).
- **Payment is bound to the submission.** The payment transaction hash (`b`) and block number (`bb`) are embedded in the commitment payload itself (see §11), so a submission cannot claim someone else's payment.

The backend verifies each payment **against the chain, fail-closed**:

1. The transaction exists in the claimed block (single-block lookup, no trust in the miner's claim).
2. The signer is the submitting hotkey, or the coldkey that owns it. On a `transfer` season the signing coldkey **must** be the on-chain owner of the submitting hotkey, or the submission is rejected for `fee_payer_not_owner` with the TAO gone.
3. The amount meets the season's published fee, and — on a `transfer` season — the destination is `params.fee.coldkey`.
4. The tx has not been used by any prior submission (anti-replay, DB + in-memory check).
5. The payment block is within a bounded window of the commitment block — currently **50 blocks (~10 minutes)**. This is the second half of the anti-replay design: a burn cannot be stockpiled and attached to a later submission, and a stale burn cannot be reused after a failed attempt.

If any check fails, the submission is marked `burn_rejected` and never enters the queue — and the TAO is **not refunded**, burned or transferred. Payment records are auditable via `/api/v1/payments/*` (admin) and summarized publicly.

**Payment hash verification uses strict exact match** — no `startswith` prefix matching. This prevents false positives from truncated `extrinsic_hash`.

> **Practical implication for miners:** the fee and the commitment must land on chain within ~2 minutes of each other. `openroboto submit` pays and announces back-to-back for exactly this reason, and it is the only command that can pay — the individual steps were removed in 1.0 so that a gap cannot open between them.

## 5. Deterministic, unpredictable seeds

Each task's evaluation seed is derived from **two independent public beacons**, neither of which the miner or the operator controls:

1. **Bittensor block hash** of the block containing the miner's commitment (miners cannot choose which block includes their transaction), and
2. **drand** — the public distributed randomness beacon ([drand.love](https://drand.love)), fetched at task creation.

```
seed = uint32( last 4 bytes of SHA256( "{block_hash}:{competition_id}:{drand_randomness}" ) )
```

🔴 The middle input is the **competition id** — `competitions.id`, the season your submission was admitted to. It is **not** the `r` field of your commitment payload (§11), which you write yourself and which nothing validates once `cid` is present, and it is **not** the round number an API response displays (that is the season's `seq`, which restarts per track). The parameter of `derive_seed` is still spelled `round_num` for compatibility; the formula concatenates by position. [SEED_GENERATION.md](./SEED_GENERATION.md) is the authority here.

The seed is unpredictable before submission, frozen after it, and reproducible by anyone: the API exposes `block_hash`, `drand_round`, `drand_random`, and the derived `seed` for every task, and the drand value can be checked byte-for-byte at `https://api.drand.sh/public/{round}`. Even if one beacon were compromised, the other still guarantees unpredictability.

**Seed failure handling**: If drand is temporarily unavailable (network issue, rate limit), the task enters `seed_failed` status — a non-terminal, retryable state. The scanner automatically retries seed computation on each scan cycle. On success, the task moves to `pending` (enqueued). On continued failure, `seed_retry_count` is incremented. This is an infrastructure issue, not a submission fault — submissions are never rejected due to drand unavailability.

Full derivation and verification code lives in [SEED_GENERATION.md](./SEED_GENERATION.md).

## 6. Evaluation

Submissions are evaluated in MuJoCo on the LIBERO task suites. The current round scores six: `libero_spatial`, `libero_object`, `libero_goal`, `libero_10`, plus two perturbation variants, `libero_object_swap` and `libero_spatial_swap`.

The swap suites earn their place: an internal red-team study showed the four base suites alone could be saturated for roughly $50 of GPU time by memorizing public demonstrations. The swap perturbations showed zero transfer from that attack — an attacker has to pay separately for each perturbation dimension — which is what makes the composition defensible. Any change to the scoring composition lands as a new round, never silently mid-round.

The worker loads the exact HF revision pinned in the commitment, so the artifact under evaluation is frozen at submission time. Scores are pushed back over an authenticated, fail-closed endpoint (`POST /api/v1/benchmark/task/{id}/score`, admin key): unauthenticated score submission is rejected, and a failed evaluation (`eval_failed`) writes no scores and creates no ranking entry.

The worker can also report progress via `PATCH /api/v1/benchmark/task/{id}/status` (stages: `downloading`, `prechecking`, `running`), which updates the `stage` column in the database for real-time visibility.

## 7. Ranking — challenge rules (king-of-the-hill)

The subnet does not rank by raw score alone; it uses a challenge system designed to reward beating the incumbent, not tying it:

1. Scored miners are ordered by earliest scoring time; the first becomes the initial **champion** (Rank 1).
2. Each subsequent miner **challenges the current champion**. The challenge succeeds only if

   ```
   challenger_score > champion_score + champion_margin      # champion_margin default 0.01
   ```

   Note the comparison is **strictly greater than**. A gap of *exactly* `champion_margin` is **not** enough to take the crown — the challenge fails, and so does anything below it. Ties lose on purpose: an exact copy of the champion's weights scores exactly the same, so "resubmit the leader's model" can never win. If you want Rank 1, you have to beat the incumbent by *more* than the margin.

3. If the challenge succeeds, the challenger becomes the new champion, the old champion drops to Rank 2, and the rest shift down.
4. If it fails, the challenger **does not appear on the board at all**.
5. The board is capped at **Top 3**, with emission weights **70 / 20 / 10**.

Consequences worth noting: copying the current champion's weights cannot dethrone it (an identical copy ties, and a tie is a failed challenge); and a settled round's champion is **held** until someone clears the bar in a later round.

## 8. Weights on chain

The backend resolves the ranking and exposes it via the API. A lightweight validator reads it, normalizes to u16, and calls `set_weights` on netuid 80 — with strict success checking (the call's actual `is_success` flag is verified rather than assumed). Anyone can confirm the emitted weights:

```bash
btcli subnet metagraph --netuid 80 --network finney
```

## 9. Anti-gaming summary

| Attack | Countermeasure |
|---|---|
| Flood the queue with junk submissions | Per-submission entry fee (§4); one commitment per hotkey per round (§3) |
| Overfit to a known evaluation seed | Seed derived from block hash + drand, unknowable pre-submission (§5); seed_failed retry instead of silent failure |
| Memorize public demos to saturate suites | Swap-perturbation suites with zero attack transfer (§6) |
| Swap model weights after scoring | Commitment pins the exact HF commit hash; changing weights changes the hash and resets the submission (§3, §11) |
| Resubmit someone else's weights under a new name | Weight fingerprinting via HF LFS metadata (sha256 of every shard) — stored as `repo_hash` in DB |
| Impersonate another miner's repo | Nothing in the repo *name* defends against this — see §11. What binds a submission to a miner is the commitment: it is signed by the hotkey, and the exact HF commit it names is pinned |
| Point at someone else's fee payment | Payment tx embedded in the commitment payload; signer must match the submitting hotkey or its owning coldkey (§4) |
| Reuse one payment tx across submissions | Anti-replay check, DB + in-memory (§4); strict exact match for tx hash |
| Copy the champion's model to steal Rank 1 | Challenge margin: a tie is a failed challenge (§7) |
| Forge scores into the backend | Score endpoint is authenticated and fail-closed (§6) |

## 10. Public API

Live at **`https://api.openroboto.ai`** — the endpoints below are public, **no API key required**:

| Endpoint | Returns |
|---|---|
| `GET /healthz` | Liveness, and the `netuid` the backend watches |
| `GET /api/v1/rounds/current` | Active round, baseline model, champion |
| `GET /api/v1/leaderboard` | Ranked rows with scores, delta vs baseline, audit links |
| `GET /api/v1/queue/status` | Evaluation queue: per-task status (pending / evaluating / evaluated / eval_failed) |
| `GET /api/v1/submissions/{task_id}` | Single-submission audit record (`task_id` = `task_{hotkey}_{round}`) |

The website renders the same data live: [openroboto.ai/#/benchmark](https://www.openroboto.ai/#/benchmark) (leaderboard) and [openroboto.ai/#/queue](https://www.openroboto.ai/#/queue) (queue).

**What this CLI itself calls** is a different, overlapping set — seven read-only addresses, all in `src/openroboto/backend_api.py`:

| Endpoint | Used by |
|---|---|
| `GET /api/v1/competitions` | `init`, `submit` (the season check before the fee) |
| `GET /api/v1/competitions/{id}/roster` | `status`, and `submit`'s duplicate-entry gate |
| `GET /api/v1/submissions/history` | `status` |
| `GET /api/v1/scan-rejections` | `status` |
| `GET /healthz` | `init --backend-url` against a self-hosted backend, to ask which netuid it watches |
| `GET /api/v1/weights` | `validator run` |
| `GET /api/weights` | `validator run`, fallback for a backend that has not migrated |

The backend's full endpoint contracts live in the backend repository (`docs/specs/`); authenticated worker and admin routes are deliberately not documented here.

## 11. Chain commitment format

The miner's announcement is a JSON payload stored on chain via Commitments (fits `Data::BigRaw`, ≤ 512 bytes):

```json
{
  "s":  "<hotkey SS58, full>",
  "h":  "<block hash at announcement, hex, no 0x>",
  "c":  "<HF commit hash, 40 hex chars>",
  "r":  <round number>,
  "i":  "<HF repo id, e.g. user/lingbot-vla-2.0-xxxxxxxxxxxx>",
  "b":  "<payment tx hash, hex, no 0x>",
  "bb": <payment block number>,
  "cid": <competition id>,
  "m":  "<model fingerprint, real track only>"
}
```

`cid` and `m` were added in protocol 0.7.0; a payload written before that carries the first seven keys only.

This single payload binds together the miner's identity (`s`), the exact model artifact (`i` + `c`), the fee payment (`b` + `bb`), the season (`cid`) and the round (`r`). The backend's chain scanner decodes it, runs the §4 payment checks, and creates the evaluation task.

**Repo naming:** the CLI generates `{hf-username}/{base_model_family}-{last 12 characters of the hotkey SS58}` — one repository per season, named after the base model that season runs. It is a **default, not a rule**: any repository the miner can read is accepted, because the backend fetches whatever the commitment's `i` field points at. Set `huggingface.repo_id` to name it yourself.

⚠️ This paragraph previously said the suffix was **required** and that it made "repo squatting and impersonation detectable at scan time". No such check exists in the backend or in `openroboto-protocol` (grepped 2026-09-02), and a submission named nothing like it has been scored on the live leaderboard. Stating an unimplemented rule is worse than stating none: it reads as a defence somebody may rely on.

⚠️ The prefix said `pi05-` until 2026-09-02. One repository serves a miner's whole career, so a base model's name in it outlives that base model — see `huggingface/repository.py` for what that cost.

## 12. Season configuration

Everything about a season — its round, status, fee, dataset, base model and reference training parameters — is published on **the competition row**, served by `GET /api/v1/competitions`. `openroboto init` copies the row of the season you pick into your `miner.yaml`, so `build` / `train` / `check` never go online, and `openroboto submit` re-reads it from the backend in the moment before the fee is paid. There is no privileged channel between the operator and any miner.

`control.json` still exists and still must not 404, but **only external validators read it, and only for `public_key`**. Everything a season decides is a column on the competition row; see [VALIDATOR.md](./VALIDATOR.md#the-controljson-contract).

## 13. Links

| What | Where |
|---|---|
| Website (live data) | <https://www.openroboto.ai> |
| Leaderboard | <https://www.openroboto.ai/#/benchmark> |
| Public queue | <https://www.openroboto.ai/#/queue> |
| API health | <https://api.openroboto.ai/healthz> |
| Open Data Pool | <https://huggingface.co/buckets/openroboto-ai/datapool> |
| Base model, current season (LingBot-VLA 2.0) | <https://huggingface.co/robbyant/lingbot-vla-v2-6b-robotwin> |
| Base model, archived season (openpi π0.5) | <https://github.com/Physical-Intelligence/openpi> |
| LIBERO benchmark | <https://github.com/Lifelong-Robot-Learning/LIBERO> |
| drand beacon | <https://drand.love> |
| Miner guide | [MINER.md](./MINER.md) · [MINER_DEPLOY.md](./MINER_DEPLOY.md) |
| Validator guide | [VALIDATOR.md](./VALIDATOR.md) |
| Seed derivation spec | [SEED_GENERATION.md](./SEED_GENERATION.md) |
| control.json contract (validators) | [VALIDATOR.md](./VALIDATOR.md#the-controljson-contract) |
| Evaluation fee rules | [PAYMENT.md](./PAYMENT.md) |
| Doc index | [README.md](./README.md) |
