# Weight-Setting Validator Guide

> **Status**: current · **Updated**: 2026-09-02 · **Audience**: external weight-setting validators
> **Scope**: Reading published weights and setting them on chain via `openroboto validator run`.
> **Note**: Miners do not need this document.

## Responsibility

`commands/validator.py` is a lightweight Bittensor weight setter. It does not run benchmarks. The public evaluation implementation is maintained in the separate `validator` repository.

The process:

1. reads `public_key` out of the public `control.json` — that one field, nothing else;
2. scans public chain commitments;
3. requests current weights from the read-only `/api/v1/weights` endpoint, falling
   back to the pre-v1 `/api/weights` on a backend that has not migrated;
4. maps hotkeys to current metagraph UIDs;
5. normalizes positive weights to the Bittensor range;
6. calls `set_weights`.

## Configuration

```bash
openroboto init --validator    # writes validator.yaml
```

Set the Bittensor network, netuid, local wallet selection, public `control.json` URL, and read-only result-service URL. Leave the public read credential empty if the deployed read endpoint does not require it.

## Run

```bash
openroboto validator run                 # resident
openroboto validator run --once          # one pass, for cron or debugging
```

The process polls every 60 seconds and applies `weight_interval_min` before each weight submission.

## Security properties

- no import from a scoring-service or owner package;
- no scoring, payment-management, round-control, or dataset-management operation;
- no write credential for the result service;
- chain writes are limited to the validator wallet's Bittensor `set_weights` call.
