# Workstation and reference data

> Documentation draft · 2026-09-07. No numeric hardware/interface parameters,
> dataset download, calibration files or executable examples are released here.

This document will describe the physical xArm workcell and public reference data
used by the π0.5 and LingBot-VLA 2.0 real-robot tracks. Its purpose is to let miners
understand the observations their model receives and reproduce the model-facing
pipeline without implementing our hardware adapter.

## Responsibility boundary

| Miner supplies | Official evaluation program supplies |
|---|---|
| Complete checkpoint for the selected model family | Matching model loader and workcell runtime |
| Compatibility with the published observation/action contract | Hardware-readout conversion into that contract |
| Required model configuration, preprocessing assets and normalization metadata | Gripper-driver mapping and xArm SDK calls |
| An accessible, fixed HF revision | Controlled physical trials and their evaluation records |

The intended model-facing contract is shared with the official π0.5 real-robot
evaluator. Each model family still has its own loading/export requirements.
Hardware adaptation does not remove the need to publish observation and action
semantics for training and inference.

## Public reference package to be published

| Artifact | Purpose | Status |
|---|---|---|
| Workcell overview and camera sample frames | Understand the physical scene and model viewpoint | Pending |
| Versioned observation/action contract | Identify model fields, layout and meaning | Pending |
| Complete example input and output | Test model-side integration without moving a robot | Pending |
| Representative episode and data schema | Explain images, state, actions, instructions and timestamps | Pending |
| Preprocessing and normalization reference | Reproduce the input/output transformations | Pending |
| Official runtime reference and compatibility test | Verify each model family's loading and inference | Pending |
| Task and baseline evidence references | Connect evaluation conditions to published qualification results | Per competition; pending references |

No array dimensions, unit conventions, rotation representation, gripper field
count, control frequency, chunk execution length or normalization mode should be
inferred from these placeholders. These will be filled from the verified runtime,
not guessed from simulation or from the arm's physical degrees of freedom.

## Data publication requirements

When reference data is released, document its source, collection conditions,
field definitions, preprocessing, splits, allowed use and limitations. Publish a
fixed revision and checksums with any downloadable artifacts. Distinguish public
training/reference episodes from evaluation-only records and held-out material.
Do not describe a sample episode as a complete training dataset.

Evaluation records should identify the competition, model revision, workstation
protocol version, task, outcome and video. A workcell fault must be distinguishable
from model failure. Hardware configuration changes require a versioned record so
the two tracks can be compared under documented conditions.

## Operational details outside miner requirements

Miners do not need robot login credentials, network addresses, device serial
numbers, private camera feeds, raw gripper-driver conversions or emergency-stop
operations to submit a model. Keep secrets and site-specific access instructions
outside public documentation. Hardware operation and safety procedures remain
with the official workcell operators.

See [the real-robot miner guide](MINER_REAL.md) and
[parallel-track overview](REAL_TRACKS.md).
