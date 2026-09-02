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

`doctor` checks Python, the protocol package, config, the competition this
workspace mines (and what entering it costs), your HF token, your wallet
balance, Docker, GPU and the training image. It runs offline. It exists so that
"paid the entry fee, then found out the environment was wrong" cannot happen.

## Each round

```bash
openroboto build           # once: build the training image (~20 min first time)
openroboto train           # one round; writes tmp/robot_train_vla_miner/round_N/
openroboto check           # verify the checkpoint format — free, do not skip
openroboto submit          # upload → pay the entry fee → announce
openroboto status          # what the subnet made of it, and why
```

**`check` before `submit`, every time.** The bundled `train_strategy.py` does not
train and **exports no checkpoint** — the export is the step marked for you to
write, and it is the one that decides whether the round is worth anything. Two
rules for it:

- the training output directory **is the checkpoint root** (`submit` uploads it
  verbatim as your HF repository root), so export at the top of it, not into a
  subdirectory the evaluator will not descend into;
- export the **full** checkpoint, not a LoRA adapter. There is **no `openroboto
  merge` command** and the evaluator merges nothing either — that work belongs in
  `train_strategy.py`, which runs in the training container where the model
  libraries are.

`openroboto train` tells you what the run actually left behind. `check` applies the
evaluator's own rules locally, for free. `submit` spends the entry fee — burned
or transferred, whichever this season charges — and it is **not refunded**.

## What is in here

| Path | What it is |
|---|---|
| `miner.yaml` | Your configuration. Holds your wallet password and HF token — **never commit it**. The `environment` field at the top picks mainnet / dev / local as one setting |
| `train_strategy.py` | Your training logic. This is the file to edit |
| `.gitignore` | Keeps credentials, state and multi-GB caches out of version control |
| `state/round_N.json` | Per-round progress. `submit` reads it to resume **and to reuse a fee already paid instead of paying twice** |
| `tmp/robot_train_vla_miner/round_N/` | Training output — the checkpoint `check` and `submit` look at |
| `cache/` | Base checkpoint for this season's base model, downloaded once (several GB) |
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

- **Paid but not announced** → `openroboto submit --round N`. It resumes from
  `state/round_N.json`: the upload is not repeated and the fee recorded there is
  reused, so **you do not pay twice**. Only the on-chain commitment is sent. The
  payment must reach that commitment within 50 blocks (~10 minutes), and `submit`
  says so if the window has already closed instead of charging you again.
- **`submit` interrupted** → just re-run it. Completed steps are skipped and the
  fee already paid is reused.
- **`build` fails** → `openroboto doctor` first; it names the missing piece.

## Reference

- Full miner guide, payment rules, seed verification:
  <https://github.com/openroboto-ai/openroboto-cli/tree/main/docs>
- `openroboto <command> --help` for any command
