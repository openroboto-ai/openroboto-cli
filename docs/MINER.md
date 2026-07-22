# Miner Guide

The OpenRoboto miner reads public round data, trains a compatible VLA model, publishes a pinned Hugging Face commit, pays the evaluation fee, and announces the submission on chain.

## Components

- `miner.py` handles preparation and training.
- `openpi-runner/` isolates OpenPI and its Python dependencies.
- `rt.py` handles upload, burn, and chain announcement.
- `payment.py` creates the evaluation-burn transaction.
- `utils/chain.py` encodes and verifies the public chain commitment.

## Quick start

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp miner.example.yaml miner.yaml
python miner.py --config miner.yaml
python rt.py submit --config miner.yaml --round 1
```

Required local values include the wallet selection, hotkey address, public `control.json` URL, Hugging Face username, and Hugging Face write token. A wallet password is optional and must never be committed.

## Training

`miner.py` downloads the public training resources and passes the selected checkpoint and hyperparameters to the runner. Runtime state is written under `state/`, and trained artifacts are written under the configured output directory. Both are ignored by Git.

An optional custom strategy path can point to miner-owned code outside this repository. The public runner contract and examples are in `openpi-runner/`.

## Submission

`rt.py submit` performs the following ordered actions:

1. upload the trained artifact to Hugging Face;
2. resolve the immutable repository commit;
3. burn the public evaluation fee;
4. announce the model and payment reference on chain.

The compact chain payload contains:

```json
{
  "s": "<miner-hotkey>",
  "h": "<commitment-block-hash>",
  "c": "<model-commit>",
  "r": 1,
  "i": "<namespace/model-repository>",
  "b": "<burn-transaction-hash>",
  "bb": 123456
}
```

`utils/chain.py` contains the public encoder and decoder. Reviewers can read the commitment from Bittensor, fetch the exact model commit, and reproduce the public seed and evaluation.

## Safety

- Keep `miner.yaml`, wallet files, tokens, passwords, runtime state, and model artifacts out of Git.
- Verify the public fee before burning.
- Stop if the Hugging Face upload or commit lookup fails.
- Confirm the chain commitment after submission.
- Use the published model commit rather than a mutable branch name when reproducing results.

