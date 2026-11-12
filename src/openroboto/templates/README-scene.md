# Author a scene task

This workspace uses the season's schema-3 execution profile. `catalog.json`
contains the complete published robot, geometry, program and resource limits.
`task.json`, `scene.xml` and `assets/` are the editable public execution inputs.
The bundled starter is an original CC0 import probe: replace its goal and scene
with your task. It is not an admitted task or a supplied reference solution.
When the backend publishes an active reference-panel execution contract, init
uses it in task.json and catalog.json. Keep that contract: submit compares its
exact digest with the live server before uploading or paying.

1. Edit the scene and the `TabletopProgramChecker` definition. Keep dependency
   sizes and SHA256 values in `task.json` consistent with the actual files.
   Keep the checker definition identical to `task_definition`.
2. Write `solve.py` with `solve(runtime, task, instance)`. The CLI records real
   actions separately for every initial state. Keep source code outside E/P.
   If the solver reads local data files, list them explicitly under
   `authoring.solve_inputs` in `miner.yaml` (paths relative to the workspace).
   Only those files and the solver are mounted read-only; the workspace config
   and credentials are never mounted. These data files are not uploaded.
3. Run `openroboto check`. It builds the official bundled image when needed,
   renders the canonical instruction into a new E, records missing references
   into a new private P, binds both roots, and replays physical admission.
   Docker is required; host MuJoCo and worker packages are not required.
4. After a successful check, run `openroboto submit`. It uploads the dual-root
   `task-package/` privately, verifies the fixed remote commit and the detached
   check receipt against the live season, then submits and registers its identity.
   If chain ingestion is pending, rerun `openroboto submit` to finish registration
   without paying again. `openroboto status` reports backend admission separately.

`openroboto check --static-only` checks structure without Docker and does not
prove reference playback or readiness. Full local success does not grant server
admission, panel results, originality or rewards. Server capability, runtime and
receipt validity must be declared by the season operator.

`evidence/` retains every attempt, failed private diagnostics, previous packages
and the detached receipt. It never belongs inside `task-package/`. The reference
changes the private and joint digests; every altered E requires a fresh P.

To import an existing authored scene into a new source directory, use
`openroboto create authored/task.json --from-execution /path/to/original/E`,
then set `authoring.definition: authored/task.json` in `miner.yaml`. This creates
a new runtime binding and retains the source E and old evidence unchanged.
The next full check records a fresh reference. It does not migrate old proofs.

## Paying a Gamma submission fee

The season publishes its payment kind and exact fee in
`competition.params.fee` in `miner.yaml`. For a Gamma season, `kind` is `gamma`
and `amount_gamma` is the fixed Gamma price; the CLI does not convert it from
TAO each time you submit. Run `openroboto init --refresh` if the live fee changed.
`init` and `check` remain free.

Before your first paid submission, fund the Gamma deposit at your miner hotkey
on the selected subnet. This CLI pays locally: the deposit must belong to the
coldkey that owns your miner hotkey and name that same coldkey as its spender.
The CLI never uploads your wallet keys or asks a backend to spend for you.

```bash
# Replace every placeholder with the values of your own wallet and season.
btcli gamma mint --netuid <NETUID> --hotkey <MINER_HOTKEY> \
  --amount-tao <TAO_TO_TOP_UP> --spender <YOUR_COLDKEY> -w <WALLET>
# For an existing deposit with another spender:
btcli gamma set-spender --netuid <NETUID> --hotkey <MINER_HOTKEY> \
  --spender <YOUR_COLDKEY> -w <WALLET>
openroboto doctor
openroboto submit
```

Minting buys and burns subnet Alpha to create service credits. Gamma spending
destroys those credits; it does not transfer tokens to a collection wallet.
Keep TAO available for chain transaction fees. A deposit's daily spending cap
must allow the complete submission fee; doctor and submit explain how to fix
insufficient balance, spender authorization or allowance.

Submit runs the existing package, reference, receipt, identity and duplicate
checks before asking to pay. It records the signed transaction hash durably
before broadcast and requires the exact `GammaSpent` event for the subnet,
owner, miner hotkey, amount and immutable `repo@commit`. Transaction success
alone is not payment: a spend can be skipped.

If a chain call times out, rerun `openroboto submit` in this same workspace.
It reconciles the original transaction and never sends a replacement charge
while the outcome is unknown. Preserve both `state/competition_<id>.json` and
`state/competition_<id>.gamma.json`. The Gamma journal survives a damaged main
checkpoint. Same-host workspaces share an authoritative journal under
`~/.cache/openroboto/gamma-payments/<genesis>/<submission-slot>.json` and a payment
lock keyed by the backend's `(netuid, competition_id, hotkey, hf_commit)`
submission slot. A second directory reuses the original payment. If it names
another repository for the same slot, submit points to the original repository
and stops before charging. Preserve that cache
as well as the workspace copies. Do not submit the same payment wallet and
repository revision concurrently from different machines. `--force` reuses a confirmed
Gamma payment for the same submission instead of charging again; it cannot
clear an unresolved payment. Gamma waits for chain finalization. If the complete signed transaction era
expires and every block in that era is finalized with no such transaction,
submit can return to its full free checks and ask to authorize a new payment.
Missing or unfinalized blocks keep the payment unresolved. An already paid
submission whose announcement window expired needs operator review and is
never charged again automatically.
