# Parallel real-robot tracks

> **Paused 2026-10-27.** No real-robot season is open while the xArm 6 workcell is
> upgraded. The rules below describe the track when it resumes; finished seasons
> remain published. This is not a competition-opening notice. Only competitions
> published as open by the selected environment can be entered.
> The confirmed workstation interface is published in
> [WORKSTATION_DATA.md](WORKSTATION_DATA.md).

OpenRoboto has run two parallel physical-robot tracks alongside simulation. Both
are paused while the workcell is upgraded; their rules are kept here for when the
track resumes:

| Physical track | Model family | Relationship |
|---|---|---|
| Real Robot — π0.5 | `pi0.5` | Continues as its own competition |
| Real Robot — LingBot-VLA 2.0 | `lingbot-vla-2.0` | Runs alongside π0.5, without replacing it |

Each competition has its own model requirements, submission window, entry fee,
roster, qualification baseline and results. A shared workcell does not make a
submission valid in both tracks. Enter the specific competition you intend to mine.

## Select the right competition

The competitions API identifies physical competitions with `track=real` and
distinguishes their model families with `base_model_family`. The returned `id`
identifies the competition in submissions and roster requests. Do not substitute
the season sequence, the model name, or a simulation competition ID.

The Real Robot page offers a separate selector for each published physical
competition. Changing the selection changes the displayed fee, window, entry
instructions and roster together. A test-environment competition is not proof
that the corresponding mainnet competition is open.

## Model interface and hardware adapter

The π0.5 workstation interface uses 7-D state `[q1..q6, g]` and 7-D action
`[Δq1..Δq6, g]`, with joints in radians and unitless gripper command labels
`0=open`, `1=close`. Predictions contain 10 steps; all joint deltas use the same
inference-start reference. The executor selects at most the first 3 joint targets,
retimes them with a velocity/acceleration-limited spline, and sends absolute joint
commands at 100 Hz. Training actions have a nominal 30 Hz sampling rate. The
evaluation program handles hardware-readout conversion, gripper-driver mapping,
and xArm SDK calls. Miners do not need to implement those hardware adapters.

π0.5 and LingBot retain their own checkpoint and runtime requirements.
Export a complete checkpoint for the selected model family.
Do not infer the real-robot interface from a LIBERO simulation example.

See [the confirmed interface](WORKSTATION_DATA.md) for
normalization, camera input, synchronous execution and safety semantics.
Publishing the contract is not proof of a tested deployment.

## Tasks, qualification and rewards

Both physical tracks share the [seven-task evaluation catalog](REAL_TASKS.md),
with up to 10 trials per task and 70 trials per submitted model in its selected
competition; a task whose first 3 trials all fail stops there. The catalog includes five single-object placement tasks,
two-object rearrangement and plug insertion.

Use the selected competition's published task specification and baseline evidence.
A task description is not a measured baseline score; a baseline from one model
family must not be assumed to qualify the other. Hardware faults and model
failures must remain distinguishable in the evaluation record.

## Current emission allocation

**The real-robot track is paused** while the xArm 6 workcell is upgraded. No
real-robot season is open and the track holds no emission share during the pause.
Its former share moved to egocentric data collection, leaving sim and data adding
up to 100% — nothing is burned at the track level. Finished seasons keep their
published standings and unlock schedules.

| Track | Share of track emissions | Status |
|---|---|---|
| Simulation | 15% | in effect |
| Egocentric data collection | 85% | in effect |
| Real Robot — π0.5 / LingBot-VLA 2.0 | 0% | paused · workcell hardware upgrade |
| Total | 100% | |

These percentages divide emissions between tracks; they are separate from
ranking weights within simulation and settlement shares within a real-robot
prize pool. A finished real-robot season keeps the share it accrued while it was
live; an entry in one track does not share another track's pool.

The simulation Top 3 weights of 70 / 20 / 10 are relative weights within the
simulation track's 15% allocation. Real-robot settlement remains 95% to the
champion and 5% to qualified entries within that track's seasonal pool, with the
existing vesting periods. See [the real-track settlement rules](REAL_TRACK.md).

The current allocation does not retroactively redistribute previously accrued
season rewards or change locked task specifications. Entry fees are separate
from emission rewards. Do not identify a prize pool by its entry-fee recipient,
or assume one hotkey is the accounting reference for both physical tracks.

## Read next

- [Real-robot miner guide](MINER_REAL.md): preparation, checks and submission verification.
- [Workstation and reference data](WORKSTATION_DATA.md): responsibilities and planned public artifacts.
- [Payment guide](PAYMENT.md): payment behavior and failure recovery.
