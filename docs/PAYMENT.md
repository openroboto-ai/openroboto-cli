# Evaluation Fee Payment

> **Status**: current · **Updated**: 2026-11-12 · **Audience**: miners
> **Scope**: The evaluation fee: how much, how it is collected, to whom, within what window, and every way it can be wasted.
> **Note**: The fee is not refundable, whichever way it is collected. This is the most expensive document in the set to skim.

## Purpose

Each evaluated submission may require a public entry fee. **It is published on the competition row, as `params.fee`**, and `openroboto submit` confirms it against the backend in the second before it is paid. The fee creates an economic cost for queue usage.

> ⚠️ The fee comes from the competition row and nowhere else. A number that says how much but not which competition is not a way to pay: paid that way, a submission is filed under whichever season the backend defaults to, non-refundably.

## Fee kinds

`params.fee.kind` says how this season collects its fee. It is **the season's own data** — never derived from the track, the adapter or the base model — and `openroboto submit` branches on the value the backend served seconds earlier (`src/openroboto/payment/__init__.py`, `src/openroboto/competition.py`, `FEE_KINDS`).

| `params.fee.kind` | What happens on chain | Recipient |
|---|---|---|
| `burn` | `add_stake_burn` for `params.fee.amount_tao` — alpha is bought and destroyed (`payment/burn.py`) | none; the TAO ceases to exist |
| `transfer` | `Balances.transfer_keep_alive` for `params.fee.amount_tao` into `params.fee.coldkey` (`payment/transfer.py`) | the address the season publishes |
| `gamma` | `spend_gamma` for the fixed `params.fee.amount_gamma` from the miner-owned deposit (`payment/gamma.py`) | credits are destroyed; no transfer recipient |

The simulation seasons charge a `burn`; the real track charges a `transfer`. Three consequences worth knowing before you pay:

- **They are not interchangeable.** A burn of the right amount on a `transfer` season pays nobody, and the submission stays unpaid with the TAO gone. `submit` refuses unknown kinds rather than defaulting to the burn.
- **`transfer_keep_alive`, not `transfer_allow_death`** — a payment that would reap your coldkey fails and moves nothing instead of taking the remainder with it.
- **A transfer is verified backend-side**, out of the block the commitment names: the block, the destination, the amount, and that the signing coldkey owns the submitting hotkey. A workspace whose `subnet.hotkey_ss58` is owned by a *different* coldkey pays and is then rejected for `fee_payer_not_owner`. `doctor` cannot see that; the chain can.
- **The collection address is checked for shape before anything moves** (SS58, prefix 42, 48 characters, the backend's own pattern). An address that lost a character sends the fee where nobody holds the key, irreversibly.


## Paying a Gamma submission fee

The season publishes its payment kind and exact fee in
`competition.params.fee` in `miner.yaml`. For a Gamma season, `kind` is `gamma`
and `amount_gamma` is the fixed Gamma price; the CLI does not convert it from
TAO each time you submit. Run `openroboto init --refresh` if the live fee changed.
`init` and `check` remain free.

Before your first paid submission, fund the Gamma deposit at your miner hotkey
on the selected subnet. This CLI pays locally: the deposit must belong to the
coldkey that owns your miner hotkey and name that same coldkey as its spender.
The CLI never uploads your wallet keys or asks a backend to spend for you.

```bash
# Replace every placeholder with the values of your own wallet and season.
btcli gamma mint --netuid <NETUID> --hotkey <MINER_HOTKEY> \
  --amount-tao <TAO_TO_TOP_UP> --spender <YOUR_COLDKEY> -w <WALLET>
# For an existing deposit with another spender:
btcli gamma set-spender --netuid <NETUID> --hotkey <MINER_HOTKEY> \
  --spender <YOUR_COLDKEY> -w <WALLET>
openroboto doctor
openroboto submit
```

Minting buys and burns subnet Alpha to create service credits. Gamma spending
destroys those credits; it does not transfer tokens to a collection wallet.
Keep TAO available for chain transaction fees. A deposit's daily spending cap
must allow the complete submission fee; doctor and submit explain how to fix
insufficient balance, spender authorization or allowance.

Submit runs the existing package, reference, receipt, identity and duplicate
checks before asking to pay. It records the signed transaction hash durably
before broadcast and requires the exact `GammaSpent` event for the subnet,
owner, miner hotkey, amount and immutable `repo@commit`. Transaction success
alone is not payment: a spend can be skipped.

If a chain call times out, rerun `openroboto submit` in this same workspace.
It reconciles the original transaction and never sends a replacement charge
while the outcome is unknown. Preserve both `state/competition_<id>.json` and
`state/competition_<id>.gamma.json`. The Gamma journal survives a damaged main
checkpoint. Same-host workspaces share an authoritative journal under
`~/.cache/openroboto/gamma-payments/<genesis>/<submission-slot>.json` and a payment
lock keyed by the backend's `(netuid, competition_id, hotkey, hf_commit)`
submission slot. A second directory reuses the original payment. If it names
another repository for the same slot, submit points to the original repository
and stops before charging. Preserve that cache
as well as the workspace copies. Do not submit the same payment wallet and
repository revision concurrently from different machines. `--force` reuses a confirmed
Gamma payment for the same submission instead of charging again; it cannot
clear an unresolved payment. Gamma waits for chain finalization. If the complete signed transaction era
expires and every block in that era is finalized with no such transaction,
submit can return to its full free checks and ask to authorize a new payment.
Missing or unfinalized blocks keep the payment unresolved. An already paid
submission whose announcement window expired needs operator review and is
never charged again automatically.

## Miner flow

1. Read the entering season's `params.fee` (`openroboto init` wrote it into `miner.yaml`).
2. Confirm the network, netuid, wallet, amount, kind, and recipient.
3. Send the season's burn, transfer or Gamma spend (`src/openroboto/payment/`).
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

These are example values that match the rate published at the time of writing:
`sim/1` did charge 0.1 TAO. <!-- docs-fee-check: historical, sim/1 is archived -->
They are **historical**, and are kept only to show the shape of the block. The
real-hardware seasons charge a different amount *and* charge it by transfer,
which this block has no way to express at all. The amount and the kind that are checked are the ones on the
competition row; a payment that does not match is rejected, and rejected
payments are not refunded.

## Verification

An independent reviewer should verify that the transaction exists on the selected network, is associated with the submitting wallet, burns the required amount, and is referenced by the same model commitment. Service-side payment records and operational checks are outside this repository.

### Verification rules

A submission's payment reference is checked against the following rules. A submission that fails any of them does not enter the evaluation queue. Rules 1, 2, 4 and 5 also apply to Gamma. Gamma additionally requires the exact
`GammaSpent` amount and domain-separated reference for its subnet, competition,
submitting hotkey and immutable repository revision; both the deposit owner and
spender must be the submitting hotkey's owner coldkey. Gamma has no free-period
bypass. A successful transaction or `GammaSpendSkipped` event is insufficient.
The TAO-specific amount rules below remain unchanged.

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

## Safety

- Both a burn and a transfer are irreversible.
- Stop if the public payment fields are missing or invalid.
- Confirm the previous transaction result before retrying.
- Never place wallet secrets or passwords in source control.
