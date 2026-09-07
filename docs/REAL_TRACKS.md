# Parallel real-robot tracks

> Updated 2026-09-07. The confirmed workstation interface is now published in
> [WORKSTATION_DATA.md](WORKSTATION_DATA.md). This is not a competition-opening notice.
> Only competitions published as open by the selected environment can be entered.

OpenRoboto is preparing two parallel physical-robot tracks alongside simulation:

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

Parallel-track emission allocations, their denominator, effective date and
prize-pool accounting references require a separate published policy. This interface update
does not change an existing season's locked rules or transfer its accrued rewards
to another competition. The existing [real-track reward rules](REAL_TRACK.md)
remain a separate reference, not a declaration that their historical allocation
applies to both new tracks.

## Read next

- [Real-robot miner guide](MINER_REAL.md): preparation, checks and submission verification.
- [Workstation and reference data](WORKSTATION_DATA.md): responsibilities and planned public artifacts.
- [Payment guide](PAYMENT.md): payment behavior and failure recovery.
