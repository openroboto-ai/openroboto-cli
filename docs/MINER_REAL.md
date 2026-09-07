# Real-robot miner guide

> Documentation draft · 2026-09-07. Numeric interface parameters and executable
> examples are pending. This guide does not announce a new competition as open.

## 1. Choose a model family and competition

π0.5 and LingBot-VLA 2.0 are parallel physical tracks. Select the appropriate
published `track=real` competition and check its `base_model_family`, `id`, status
and submission window. Use a separate workspace for each competition so cached
configuration and submission state do not cross between them.

Install the packaged CLI and inspect the available competition list:

```bash
pip install -U openroboto
openroboto init my-real-miner
cd my-real-miner
openroboto doctor
```

Before proceeding, verify the configured backend, network, subnet, wallet and HF
account. Local development and test competitions must not be confused with
mainnet. Do not copy a competition ID or fee from another environment.

## 2. Prepare a compatible checkpoint

Use the selected competition's official base reference and real-robot model
interface when they are published. Follow that model family's export procedure;
a shared observation/action contract does not make checkpoint formats identical.
Provide a complete loadable checkpoint with its required configuration,
preprocessing assets and normalization metadata, not an unmerged adapter alone.

The official evaluator handles the hardware adapter. Miners are responsible for
matching the published model-facing inputs and outputs. The exact interface
reference is pending; do not guess the state or gripper dimensions from this draft.

This guide does not claim that `openroboto train` currently supplies an end-to-end
real-robot training recipe. Training resources and a verified recipe will be
documented separately when available.

## 3. Check before paying

```bash
openroboto check /path/to/checkpoint --config miner.yaml
```

This is a local checkpoint-format check under the selected configuration. It is
not an actual model-loading test, hardware compatibility test, eligibility ruling
or guarantee of admission. The currently reviewed CLI also skips the real-track
remote layout gate during `submit`; do not describe that step as complete model
validation before payment.

Before spending, verify that the official runtime can load the selected model
and that the published sample-input test passes, once those resources are
available. Confirm the exact HF revision and that the evaluator can download it.
If the model contract or runtime reference needed to establish compatibility has
not been published, this draft is not sufficient evidence to pay for an entry.

## 4. Submit and verify

Only after the selected competition is open and the preparation checks are complete:

```bash
openroboto submit
openroboto status
```

Review the CLI's live competition, fee and recipient before confirming payment.
The submission fixes one HF revision; uploading a later commit does not replace
the already submitted version. Preserve local submission state and wallet keys.

Confirm all of the following independently of a local format-check result:

- The commitment landed on chain and names the intended competition and HF revision.
- The selected competition's roster includes the correct entry.
- The backend confirms payment and repository access, with no invalid reason.
- The entry counts as submitted. This still does not imply successful evaluation.

An absent SDK block number or delayed roster update is not authorization to pay
again. Inspect chain/status records before retrying, and do not use `--force` as
an automatic recovery step. See [PAYMENT.md](PAYMENT.md).

## Related documents

- [Parallel real-robot tracks](REAL_TRACKS.md)
- [Workstation and reference data](WORKSTATION_DATA.md)
- [Existing reward and challenge rules](REAL_TRACK.md)
