# OpenRoboto Public Roles

## Miner

- reads the public round, payment, dataset, and training fields;
- trains a compatible VLA model through the public runner;
- publishes an immutable Hugging Face commit;
- pays the evaluation fee and announces the submission on chain;
- reproduces the seed and runs the public evaluation toolkit locally.

Public implementation: the `openroboto` CLI (`openroboto train` / `check` / `submit`) plus `openpi-runner/` for the training container.

## Evaluation worker

- downloads the announced model commit;
- validates model structure;
- runs the public LIBERO evaluation and baseline logic;
- derives per-task initial states from the published seed;
- returns the result to the scoring service through a credential supplied at runtime.

Public implementation: the separate `validator` repository.

## Weight-setting validator

- reads the public weight endpoint;
- maps hotkeys to current subnet UIDs;
- normalizes positive weights;
- calls Bittensor `set_weights`.

Public implementation: `validator.py` and required helpers in `utils/`.

## Scoring service and subnet owner

The scoring service verifies submissions, stores results, and exposes public read-only results. The subnet owner publishes the public round contract. Their deployment code, private data, credentials, databases, and operational controls are not included here.

