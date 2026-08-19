> # ⚠️ 已归档 —— 内容已并入 `../SUBNET_OVERVIEW.md`
>
> **状态**：superseded · **归档日期**：2026-08-19
> **替代者**：`docs/SUBNET_OVERVIEW.md` §2 Roles
>
> **为什么**：这 35 行是 `SUBNET_OVERVIEW.md` §2 的子集，两处各写一遍角色分工，
> 改一处忘一处就会互相矛盾。归档时它仍写着「Public implementation: `validator.py`
> 和 `utils/`」—— 那已经是 `openroboto validator run` 了，正是漂移的实例。

---

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

