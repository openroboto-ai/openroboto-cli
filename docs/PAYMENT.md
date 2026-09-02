# Evaluation Fee Payment

> **Status**: current · **Updated**: 2026-09-02 · **Audience**: miners
> **Scope**: The evaluation fee: how much, how it is collected, to whom, within what window, and every way it can be wasted.
> **Note**: The fee is not refundable, whichever way it is collected. This is the most expensive document in the set to skim.

## Purpose

Each evaluated submission may require a public entry fee. **It is published on the competition row, as `params.fee`**, and `openroboto submit` confirms it against the backend in the second before it is paid. The fee creates an economic cost for queue usage.

> ⚠️ The `payment` block in `control.json` (shown below, still published) is one rate for a subnet that now runs several seasons at once, and **this CLI no longer reads it**. A number that says how much but not which competition is not a way to pay: paid that way, a submission is filed under whichever season the backend defaults to, non-refundably.

## Two fee kinds

`params.fee.kind` says how this season collects its fee. It is **the season's own data** — never derived from the track, the adapter or the base model — and `openroboto submit` branches on the value the backend served seconds earlier (`src/openroboto/payment/__init__.py`, `src/openroboto/competition.py`, `FEE_KINDS`).

| `params.fee.kind` | What happens on chain | Recipient |
|---|---|---|
| `burn` | `add_stake_burn` for `params.fee.amount_tao` — alpha is bought and destroyed (`payment/burn.py`) | none; the TAO ceases to exist |
| `transfer` | `Balances.transfer_keep_alive` for `params.fee.amount_tao` into `params.fee.coldkey` (`payment/transfer.py`) | the address the season publishes |

The simulation seasons charge a `burn`; the real track charges a `transfer`. Three consequences worth knowing before you pay:

- **They are not interchangeable.** A burn of the right amount on a `transfer` season pays nobody, and the submission stays unpaid with the TAO gone. `submit` refuses any third word rather than defaulting to the burn.
- **`transfer_keep_alive`, not `transfer_allow_death`** — a payment that would reap your coldkey fails and moves nothing instead of taking the remainder with it.
- **A transfer is verified backend-side**, out of the block the commitment names: the block, the destination, the amount, and that the signing coldkey owns the submitting hotkey. A workspace whose `subnet.hotkey_ss58` is owned by a *different* coldkey pays and is then rejected for `fee_payer_not_owner`. `doctor` cannot see that; the chain can.
- **The collection address is checked for shape before anything moves** (SS58, prefix 42, 48 characters, the backend's own pattern). An address that lost a character sends the fee where nobody holds the key, irreversibly.

## Miner flow

1. Read the entering season's `params.fee` (`openroboto init` wrote it into `miner.yaml`).
2. Confirm the network, netuid, wallet, amount, kind, and recipient.
3. Send the burn or the transfer (`src/openroboto/payment/`).
4. Record the transaction hash and block number.
5. Include that reference in the model chain commitment.

`openroboto submit` performs this whole flow, and it is the only command that does. It re-reads the season from the backend and prints who is being paid before a y/N prompt; there is no `--skip-precheck`.

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
(0.1 TAO). They are **historical**: `sim/1` really does charge 0.1 TAO, but
`real/1` charges 2 — and it charges it by transfer, which this block has no way
to express at all. The amount and the kind that are checked are the ones on the
competition row; a payment that does not match is rejected, and rejected
payments are not refunded.

## Verification

An independent reviewer should verify that the transaction exists on the selected network, is associated with the submitting wallet, burns the required amount, and is referenced by the same model commitment. Service-side payment records and operational checks are outside this repository.

### Verification rules

A submission's payment reference is checked against the following rules. A submission that fails any of them does not enter the evaluation queue. Rules 1, 2, 4 and 5 apply to both fee kinds; rule 3 is what differs.

1. **Extrinsic hash, not extrinsic ID.** The commitment's `b` field must contain the burn extrinsic **hash** (`0x…`, 32 bytes). Explorer-style identifiers such as `8783031-0016` are not accepted. Matching is a strict exact comparison; truncated or prefixed values fail.
2. **Existence and signer.** The transaction must exist in the referenced block (`bb`) and be signed by the submitting hotkey or by the coldkey that owns it.
3. **Amount and target.** The amount must meet the season's published entry fee (`params.fee.amount_tao`; the comparison is `>=` on Rao, so being one Rao short is a rejection). On a `burn` season the burn target hotkey must match the submitting hotkey; on a `transfer` season the destination must be `params.fee.coldkey` and the signer must be the coldkey that owns the submitting hotkey.
4. **Time window.** The payment block must be within a bounded window of the commitment block — **50 blocks** (`scanner.burn_block_window`, symmetric: the check is `abs(burn_block - commit_block) > window`). Burn immediately before committing anyway; an old burn cannot be attached to a new commitment, and an expired one is not refunded. `openroboto submit` pays and announces back-to-back so the window cannot lapse between them.
5. **No reuse.** Each burn transaction can back exactly one submission. Replayed or shared transaction hashes are rejected.
6. **Free period.** When the season's published entry fee is `0`, payment verification is skipped and nothing is charged. ⚠️ `free_period` is a `payment_status` word — "this payment counts as settled" — and **not** a third `fee.kind`.

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
| `superseded` | A newer submission for the same `(hotkey, competition)` pushed this one out | **Yes** |

Older API responses may use the legacy vocabulary: `done` → `evaluated`, `failed` → `eval_failed`, `enqueued` → `pending`, `waiting` → `evaluating`.

## Safety

- Both a burn and a transfer are irreversible.
- Stop if the public payment fields are missing or invalid.
- Confirm the previous transaction result before retrying.
- Never place wallet secrets or passwords in source control.
