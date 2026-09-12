# Workstation model interface and reference data

> Confirmed interface requirements published 2026-09-07, based on the
> specification supplied by the workstation team.
> This publishes a specification, not an end-to-end verification report or a
> competition-opening notice.

This is the model-facing joint-space contract for the physical xArm6 workstation,
used by the parallel π0.5 and LingBot tracks. Model-family loading and export
requirements remain separate. The source explicitly describes π0.5 preprocessing;
shared I/O does not establish that a LingBot loader has been implemented or tested.
Use the selected competition's official runtime reference.

## State and action contract

| Field | Ordered physical representation | Meaning |
|---|---|---|
| State, 7 values | `[q1, q2, q3, q4, q5, q6, g]` | Measured absolute joint positions and current absolute gripper position |
| Action, 7 values per step | `[Δq1, Δq2, Δq3, Δq4, Δq5, Δq6, g]` | Joint-position deltas and an absolute gripper target |
| Joint order and units | Native xArm6 Joint 1 through Joint 6, base to wrist | Radians |
| Canonical gripper | Continuous `g ∈ [0, 1]` | `0` fully open; `1` fully closed; intermediate positions allowed |

Both state and action have one gripper dimension. The canonical gripper range
describes physical values after action denormalization, not the model's raw
normalized output. No binary threshold or LIBERO `-1=open/+1=close` rule applies.

This contract does not provide end-effector position or rotation, joint velocity,
joint torque, or force/torque sensor readings. EEF base/tool-frame rotation,
axis-angle conventions and Cartesian deltas do not apply to these joint actions.

## Delta reference and SDK conversion

All joint deltas in one predicted chunk use the same measured joint state captured
at the start of that inference:

```text
q_ref = measured joint positions at inference start
physical_actions = denormalize(model_actions, checkpoint norm_stats)

for each executed step k:
    q_target[k] = q_ref + physical_actions[k, :6]
    g_target[k] = physical_actions[k, 6]
```

This is a semantic example, not an executable robot-control script.
Deltas are not relative to the preceding action and are not accumulated.
The workstation does not reread the joint state to redefine each step's reference.
It sends absolute joint targets through the xArm joint servo position interface.

The workstation handles SDK units, communication, radians conversion, gripper
registers and hardware-position conversion. Miners do not implement those adapters.

## Prediction and synchronous execution

The protocol requires a 50-step prediction horizon: π0.5 produces a `50 × 7`
action chunk. Miners must not change the prediction horizon or action convention.
The workstation executes the first 25 actions by default, discards the remaining
25, then acquires a new state and image for the next inference.

Execution is synchronous: read observation → infer → execute the prefix → hold
the last target → read the next observation. Inference and action execution do
not overlap. There is no asynchronous inference, temporal ensemble or Real-Time
Chunking. The current protocol specifies no inference timeout; the local
workstation waits for inference to complete.

## Camera and image preprocessing

| Item | Confirmed specification |
|---|---|
| Camera | One Intel RealSense D415, fixed third-person RGB view |
| Mount | In front of the robot, facing the robot and main work area |
| Raw stream | RGB, 640 × 480, 30 FPS |
| Acquisition | Continuous streaming throughout the episode; sample the latest frame for each observation |
| π0.5 image input | RGB, 224 × 224 via OpenPI-compatible `resize_with_pad` |
| Additional inputs | No wrist camera, second camera or depth input |
| Cropping | No extra center crop or manually selected ROI crop by default |

Camera FPS and inference frequency are distinct. The camera is not reinitialized
for every inference. The pipeline is
`D415 RGB 640×480 → resize with padding → RGB 224×224 → π0.5`.

### Camera-view reference

![Workstation camera view showing the xArm6, gripper and tabletop work area](images/real-track/workstation-camera-view.png)

Original viewer capture supplied by the workstation team, before model
preprocessing. The visible objects illustrate the scene and do not define an
evaluation task. The viewer controls are not part of the model image input.

## Normalization and checkpoint assets

π0.5 state and actions use OpenPI-compatible quantile normalization from the
checkpoint's actual fine-tuning `norm_stats.json`: the 1st and 99th percentiles
map approximately to -1 and +1. No additional model-side action scaling is defined.
Normalized outputs outside [-1, 1] are not clipped merely for exceeding that range.

State statistics describe absolute joint positions and canonical gripper position.
Action statistics describe joint deltas and the absolute canonical gripper target,
not absolute joint targets. For training targets, convert absolute joint targets
to deltas against the inference/chunk reference before normalization.

The observation state is normalized before π0.5 inference. Model outputs are
denormalized before forming absolute joint targets and running safety checks:

```text
state: hardware readings → absolute canonical [q1..q6, g] → normalization
action: normalized output → denormalization → [Δq1..Δq6, g]
        → q_ref + Δq → safety check → robot
```

Keep the native OpenPI schema:

```json
{
  "norm_stats": {
    "state":   { "mean": [], "std": [], "q01": [], "q99": [] },
    "actions": { "mean": [], "std": [], "q01": [], "q99": [] }
  }
}
```

The empty arrays illustrate field names only and are not valid submission stats.
Every array must contain 7 values in the state/action order specified above.
The stats must exactly match those actually used for the submitted checkpoint's
fine-tuning. Do not substitute another checkpoint's statistics. The protocol
does not additionally require normalization-type, stats-version or reorder
metadata. Retaining mean/std fields does not change the quantile convention.

## Hardware and safety

The arm is a fixed-base UFACTORY xArm6, with an Inspire-Robots EG2-4C2 single-DOF
gripper. The miner interface does not depend on a particular xArm firmware or SDK
version; the workstation owns that integration.

The supplied protocol specifies enabled limits of **250 mm/s TCP speed** and
**90°/s joint speed** throughout evaluation. These are specified limits, not new
measurements performed for this documentation update. Targets must also respect
legal xArm6 joint limits and the workstation's Cartesian safety boundary, which
applies to every part of the arm.

The workstation rejects unsafe commands and reports a safety error; a safety
violation ends the episode as a failure. It does not clip dangerous targets into
a safe range.

The flange-to-gripper TCP offset is used for geometry and safety checks, not for
direct interpretation of joint-space actions. Each episode must use a common
initial pose, represented as six absolute joint positions in radians.

### Measured installation reference

The [2026-09-12 workstation measurements](WORKSTATION_MEASUREMENTS.md) provide
the normal-gripper TCP translation, a rounded reference initial pose with radian
conversion, the camera-to-base transform and a one-second command-timing test.
The [measurement JSON](data/workstation-measurements-2026-09-12.json) contains the
same numerical reference. Installation-specific measurements supplement this
interface; they do not change a season's model contract or runtime configuration.

## Tasks and responsibilities

The [real-robot task catalog](REAL_TASKS.md) lists the seven shared tasks and
English instructions for π0.5 and LingBot-VLA 2.0. Each model runs 10 trials per
task, for 70 trials in its selected competition.

Each public task release must contain its unique task name, the exact fixed
language prompt passed to the model, and an example video illustrating the task
and successful completion. The task catalog is not a training dataset or
measured qualification result.

Miners supply the 7-D contract, 50-step predictions, one third-person RGB
observation input, the fixed task prompt, a complete checkpoint and matching
normalization statistics. The workstation integrates acquisition, preprocessing,
normalization/denormalization, joint-target conversion, prefix execution, safety,
TCP configuration and initial-pose control. A complete family-specific loader
and runtime test remain necessary; `openroboto check` alone does not establish
runtime compatibility or admission.

## Reference-data publication

The camera-view reference above accompanies the confirmed interface.
Complete executable example input/output, representative episodes, runtime test
artifacts and per-task video/baseline evidence are not supplied in this update.

When data is released, include its source, collection conditions, field schema,
preprocessing, splits, allowed use, limitations, fixed revision and checksums.
Distinguish public reference/training episodes from held-out evaluation material.
A sample episode is not a complete training dataset. Evaluation records should
identify competition, model revision, workstation protocol, task, outcome and
video, distinguishing workcell faults from model failures.

Keep robot credentials, private feeds and site access details outside public docs.
Hardware operation remains with official operators; this document does not
authorize robot execution.

See [the miner guide](MINER_REAL.md) and [parallel-track overview](REAL_TRACKS.md).
