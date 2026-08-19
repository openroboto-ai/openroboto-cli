# OpenRoboto external validator workspace

Created by `openroboto init --validator`. This process reads the weights the
subnet publishes and sets them on chain. It does not evaluate anything and holds
no operator credentials.

## Do this first

```bash
$EDITOR validator.yaml     # subnet.netuid + network, your wallet, backend.public_key
openroboto doctor --config validator.yaml
```

## Run

```bash
openroboto validator run              # resident; polls and sets weights
openroboto validator run --once       # a single pass, for cron or debugging
```

It applies `weight_interval_min` between weight submissions, so leaving it
resident is the intended mode.

## What it does each pass

1. Reads `control.json` to refresh the public read credential.
2. Fetches current weights from the read-only `/api/weights` endpoint.
3. Maps hotkeys to current metagraph UIDs.
4. Normalizes positive weights to the Bittensor range.
5. Calls `set_weights`.

You are setting real emissions on mainnet netuid 80. Confirm `subnet.netuid` and
`subnet.network` before the first run — a wrong netuid writes weights to another
subnet.

## What is in here

| Path | What it is |
|---|---|
| `validator.yaml` | Your configuration, including wallet selection. **Never commit it** |
| `.gitignore` | Keeps credentials and logs out of version control |
| `logs/` | Log files, created on first run |

## Reference

- Validator guide and the weight-setting contract:
  <https://github.com/openroboto-ai/openroboto-cli/tree/main/docs/VALIDATOR.md>
- `openroboto validator run --help`
