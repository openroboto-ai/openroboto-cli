# Parallel real-robot tracks

> Updated 2026-09-07. The confirmed workstation interface is now published in
> [WORKSTATION_DATA.md](WORKSTATION_DATA.md). This is not a competition-opening notice.
> Only competitions published as open by the selected environment can be entered.

OpenRoboto runs two parallel physical-robot tracks alongside simulation:

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

The two physical tracks share the published joint-space model-facing contract:
7-D state `[q1..q6, g]` and 7-D action `[Δq1..Δq6, g]`, with joints in radians
and one continuous absolute gripper target. The 50-step prediction uses a single
inference-start joint reference; the workstation executes the first 25 steps
by default. The official
evaluation program handles hardware-readout conversion, gripper-driver mapping,
and xArm SDK calls. Miners do not need to implement those hardware adapters.

This shared interface does not mean that π0.5 and LingBot use the same checkpoint
layout or loader. Export a complete checkpoint for the selected model family.
Do not infer the real-robot interface from a LIBERO simulation example.

See [the confirmed interface](WORKSTATION_DATA.md) for
normalization, camera input, synchronous execution and safety semantics.
Publishing the contract is not proof of a tested deployment.

## Tasks, qualification and rewards

Use the selected competition's published task specification and baseline evidence.
A task description is not a measured baseline score; a baseline from one model
family must not be assumed to qualify the other. Hardware faults and model
failures must remain distinguishable in the evaluation record.

## Current emission allocation

Emissions allocated across the three tracks are distributed as follows:

| Track | Share of track emissions |
|---|---|
| Simulation | 15% |
| Real Robot — π0.5 | 42.5% |
| Real Robot — LingBot-VLA 2.0 | 42.5% |
| Total | 100% |

This allocation is in effect. These percentages divide emissions between tracks;
they are separate from ranking weights within simulation and settlement shares
within a real-robot prize pool. Each real track accounts for its own seasonal
rewards; an entry in one track does not share the other track's pool.

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
