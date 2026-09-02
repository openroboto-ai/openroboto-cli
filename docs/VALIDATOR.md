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

### The `control.json` contract

`control.json` is a public, read-only JSON document served over plain HTTP at
`https://<backend host>/control.json`. **One field concerns you:**

```jsonc
{ "public_key": "<read-only API key>" }
```

That key goes into `backend.public_key` in `validator.yaml`; `GET /api/weights`
answers 401 without it. `openroboto validator run` re-fetches the document every
cycle with an `If-None-Match` conditional request, so a rotated key is picked up
without restarting the process — which is why the URL must keep answering rather
than 404.

Treat every other key in the document as absent: they are not part of this
contract, they are not read by this CLI, and a value taken from one of them
describes the subnet rather than any particular competition. Everything about a
season — its status, dataset, base checkpoint and entry fee — is served per
season by `GET /api/v1/competitions`.

## Run

```bash
openroboto validator run                 # resident
openroboto validator run --once          # one pass, for cron or debugging
```

The process polls every 60 seconds and applies `weight_interval_min` before each weight submission.

## Security properties

- no import from a scoring-service or owner package;
- no scoring, payment-management, season-control, or dataset-management operation;
- no write credential for the result service;
- chain writes are limited to the validator wallet's Bittensor `set_weights` call.
