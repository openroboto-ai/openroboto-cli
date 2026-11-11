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
