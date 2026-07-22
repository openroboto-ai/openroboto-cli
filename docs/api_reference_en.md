# Public Read-Only API Reference

This document lists the public read surface used by miners, validators, and independent reviewers. It intentionally excludes task data, score-ingestion routes, payment-management routes, and owner operations.

## Authentication

Deployments may publish a read credential in `control.json` as `public_key`. When present, send it as:

```http
X-API-Key: <public-read-key>
```

The credential grants read access only.

## Health

### `GET /health`

Returns service availability and version metadata. No authentication is required.

## Rounds

### `GET /api/v1/rounds/current`

Returns the current public round number and status.

### `GET /api/v1/rounds?limit=<n>`

Returns recent public round summaries.

## Rankings

### `GET /api/v1/leaderboard?round_id=<id>&limit=<n>&offset=<n>`

Returns a paginated leaderboard for a public round.

### `GET /api/rank`

Returns the current compact ranking view.

### `GET /api/miner/<hotkey>`

Returns the public summary for one miner hotkey.

## Weights

### `GET /api/weights`

Returns the hotkey-to-weight map consumed by `validator.py`.

Example shape:

```json
{
  "<miner-hotkey>": 0.75,
  "<another-miner-hotkey>": 0.25
}
```

### `GET /api/export`

Returns the public round and ranking export intended for independent review.

## Benchmark metadata

### `GET /api/v1/benchmark/meta`

Returns the public benchmark name, version, supported suite identifiers, and scoring metadata. The evaluation implementation and baseline tools are published in the separate validator repository.

## Excluded routes

This public reference does not document result submission, evaluation queues, held-out task payloads, payment administration, or owner control operations.

