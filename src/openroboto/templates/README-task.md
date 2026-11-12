# Task authoring workspace

This workspace belongs to the task season in `miner.yaml`. Its execution profile
chooses the robot, objects, complete parameter ranges and resource profiles in
`catalog.json`. `task.json` is an editable authoring definition, not a built
submission package. Keep your local `solve.py` and `evidence/` outside the package.

1. `openroboto init`: choose a task season. Edit `task.json` using `catalog.json`.
   Fill in the wallet, HF and contributor identity settings in `miner.yaml`.
2. `openroboto check`: build the official bundled CPU image when needed, build
   the package, record missing reference actions using `solve.py`, then run the
   worker's structural and physical admission. Your script defines
   `solve(runtime, task, instance)` and must solve every instance. For the
   unchanged v1 one-cube template you can run `openroboto check --example`.
3. After `READY TO SUBMIT`, run `openroboto submit`. It uploads `task-package/`
   and checks the exact `repo@commit` again before asking to pay the season fee.
   It also requires the successful `evidence/check-receipt.json` from this
   workspace and season, with the same digest, current admission version and
   the season's declared validity period. Missing or expired receipts tell you
   to run `openroboto check` again. Submit does not rerun physics.

Docker is required; MuJoCo and the worker run inside the image. No Python runtime
path is needed. The first build downloads the pinned base image, Debian snapshot
and hash-locked dependencies. Later checks reuse the matching local image.

`openroboto check --static-only` validates the definition/package in seconds
without Docker. Static success does not create a physical passing verdict.
Its receipt cannot authorize payment unless the live season explicitly allows
static-only admission; submit labels that exception. Receipts and check reports
are kept outside the package and are not uploaded. A new failed check removes
the previous successful receipt.
`openroboto check --json` returns worker check names, passed/kind/detail, package
digest, admission version, runtime content digest and actual image digest.

Reference recording changes package identity. Reports and retained failed
attempts live in `evidence/`; admission mounts the package read-only. A failed
reference is never presented as ready. Fix `solve.py`, use `record-reference`
to replace a bad existing reference, then check again. `status` reads submitted
tasks; `doctor` checks the package/profile and the local runtime image.

The local empty dedup corpus cannot prove originality against the server's
private corpus. Server registration, admission and reward eligibility remain
server decisions. Check never uploads, calls a model, or pays a fee.

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
