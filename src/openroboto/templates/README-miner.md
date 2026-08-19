# OpenRoboto miner workspace

Created by `openroboto init`. Everything you need is here or inside the
`openroboto` command — there is no repository to clone.

## Do this first

```bash
# 1. Fill in your credentials
$EDITOR miner.yaml         # subnet.hotkey_ss58, huggingface.token, huggingface.username

# 2. Check the environment BEFORE anything costs money
openroboto doctor
```

`doctor` checks Python, config, `control.json` reachability, Docker, GPU, the
training image, your HF token and your wallet balance. It exists so that
"burned TAO, then found out the environment was wrong" cannot happen.

## Each round

```bash
openroboto build           # once: build the training image (~20 min first time)
openroboto train           # one round; writes tmp/robot_train_vla_miner/round_N/
openroboto check           # verify the checkpoint format — free, do not skip
openroboto submit          # upload → burn → announce
openroboto status          # what the subnet made of it, and why
```

**`check` before `submit`, every time.** Training produces a LoRA adapter, and a
bare adapter is **rejected** — the evaluator needs a complete merged checkpoint.
`check` applies the evaluator's own rules locally, for free. `submit` burns TAO,
and burns are **not refunded**.

## What is in here

| Path | What it is |
|---|---|
| `miner.yaml` | Your configuration. Holds your wallet password and HF token — **never commit it**. The `environment` field at the top picks mainnet / dev / local as one setting |
| `train_strategy.py` | Your training logic. This is the file to edit |
| `.gitignore` | Keeps credentials, state and multi-GB caches out of version control |
| `state/round_N.json` | Per-round progress. `submit` reads it to resume **and to reuse an existing burn instead of paying twice** |
| `tmp/robot_train_vla_miner/round_N/` | Training output — the checkpoint `check` and `submit` look at |
| `cache/pi05_base/` | π0.5 base checkpoint, downloaded once (several GB) |
| `logs/` | Log files |

Only the first three are yours to edit. The rest are created as needed.

## Writing your training strategy

`train_strategy.py` must define one function. The signature is fixed — the
training container calls it:

```python
def train(cfg: dict, episodes: list, policy=None) -> tuple:
    """Returns (metrics, proof). metrics must include final_loss and training_steps."""
```

`cfg` carries `checkpoint_path`, `epochs`, `batch_size`, `lr`, `lora_r`,
`lora_alpha`, `hotkey` and `output_dir`. Your script is mounted into the
container at run time, so **changing it does not require rebuilding the image**.

Point at a different file per run with `openroboto train -s other_strategy.py`,
or set `custom_train_script` in `miner.yaml`.

Want a more heavily commented starting point? `openroboto init -s example`.

## If something goes wrong

```bash
openroboto status          # rejection reasons, straight from the subnet
```

- **Burned but not announced** → `openroboto announce --round N`. It reuses the
  recorded burn; **do not burn again**. The burn must reach the chain commitment
  within 50 blocks (~10 minutes), and `announce` will tell you if that window has
  already closed instead of charging you another fee.
- **`submit` interrupted** → just re-run it. Completed steps are skipped and the
  existing burn is reused.
- **`build` fails** → `openroboto doctor` first; it names the missing piece.

## Reference

- Full miner guide, payment rules, seed verification:
  <https://github.com/openroboto-ai/openroboto-cli/tree/main/docs>
- `openroboto <command> --help` for any command
