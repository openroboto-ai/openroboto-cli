# Evaluation Burn Payment

## Purpose

Each evaluated submission may require a public burn amount. The current value is published in `control.json` as `payment.burn_rate_tao`. Burning creates an economic cost for queue usage without transferring the fee to the subnet operator.

## Miner flow

1. Read the current public payment fields.
2. Confirm the network, netuid, wallet, amount, and optional price limit.
3. Submit the burn through `payment.py`.
4. Record the transaction hash and block number.
5. Include that reference in the model chain commitment.

The submission CLI performs this flow in `rt.py submit`.

## Public fields

```json
{
  "payment": {
    "enabled": true,
    "burn_rate_tao": 0.01,
    "limit_price_rao": 0
  }
}
```

These are example values. Always use the current round's published document.

## Verification

An independent reviewer should verify that the transaction exists on the selected network, is associated with the submitting wallet, burns the required amount, and is referenced by the same model commitment. Service-side payment records and operational checks are outside this repository.

## Safety

- A burn is irreversible.
- Stop if the public payment fields are missing or invalid.
- Confirm the previous transaction result before retrying.
- Never place wallet secrets or passwords in source control.
