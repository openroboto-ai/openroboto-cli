# Evaluation Burn Payment

## Purpose

Each evaluated submission may require a public burn amount. The current value is published in `control.json` as `payment.burn_rate_tao`. Burning creates an economic cost for queue usage without transferring the fee to the subnet operator.

## Miner flow

1. Read the current public payment fields.
2. Confirm the network, netuid, wallet, amount, and optional price limit.
3. Submit the burn through `payment.py`.
4. Record the transaction hash and block number.
5. Include that reference in the model chain commitment.

The submission CLI performs this flow in `openroboto submit`.

## Public fields

```json
{
  "payment": {
    "enabled": true,
    "burn_rate_tao": 0.1,
    "limit_price_rao": 0
  }
}
```

These are example values that match the rate published at the time of writing
(0.1 TAO). Always use the current round's published document — a burn that does
not match the published rate is rejected, and rejected burns are not refunded.

## Verification

An independent reviewer should verify that the transaction exists on the selected network, is associated with the submitting wallet, burns the required amount, and is referenced by the same model commitment. Service-side payment records and operational checks are outside this repository.

### Verification rules

A submission's burn reference is checked against the following rules. A submission that fails any of them does not enter the evaluation queue.

1. **Extrinsic hash, not extrinsic ID.** The commitment's `b` field must contain the burn extrinsic **hash** (`0x…`, 32 bytes). Explorer-style identifiers such as `8783031-0016` are not accepted. Matching is a strict exact comparison; truncated or prefixed values fail.
2. **Existence and signer.** The transaction must exist in the referenced block (`bb`) and be signed by the submitting hotkey or by the coldkey that owns it.
3. **Amount and target.** The burned amount must meet the published `payment.burn_rate_tao`, and the burn target hotkey must match the submitting hotkey.
4. **Time window.** The burn block must be within a bounded window of the commitment block — **50 blocks** (`scanner.burn_block_window`, symmetric: the check is `abs(burn_block - commit_block) > window`). Burn immediately before committing anyway; an old burn cannot be attached to a new commitment, and an expired one is not refunded. `openroboto announce` refuses to submit once the window has passed.
5. **No reuse.** Each burn transaction can back exactly one submission. Replayed or shared transaction hashes are rejected.
6. **Free period.** When the published `payment.burn_rate_tao` is `0`, burn verification is skipped and no payment is required.

### Submission status model

Public APIs report each submission with a unified status:

| Status | Meaning | Terminal |
|---|---|---|
| `received` | Discovered on chain, not yet verified | No |
| `burn_checking` | Burn verification in progress | No |
| `burn_passed` | Burn verified; waiting for seed computation | No |
| `burn_rejected` | Burn verification failed | **Yes** |
| `pending` | Verified and queued for evaluation | No |
| `seed_failed` | Seed computation failed (drand unavailable); retried automatically | No |
| `evaluating` | Evaluation in progress | No |
| `evaluated` | Evaluation complete | **Yes** |
| `eval_failed` | Evaluation failed | **Yes** |
| `rejected` | Rejected (for example, duplicate model hash or invalid commitment fields) | **Yes** |

Older API responses may use the legacy vocabulary: `done` → `evaluated`, `failed` → `eval_failed`, `enqueued` → `pending`, `waiting` → `evaluating`.

## Safety

- A burn is irreversible.
- Stop if the public payment fields are missing or invalid.
- Confirm the previous transaction result before retrying.
- Never place wallet secrets or passwords in source control.
