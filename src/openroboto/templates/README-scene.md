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
