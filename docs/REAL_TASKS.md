# Real-robot evaluation tasks

The real-robot tracks for **π0.5** and **LingBot-VLA 2.0** share the following
seven tasks. Each submitted model runs **10 trials per task**, for **70 trials**
in its selected competition. Results and qualification remain separate for
each track.

## Task catalog

| Task | Instruction | Capability | Trials per model |
|---|---|---|---|
| Upside-down Cup to Plate | Pick up the upside-down cup and put it onto the plate. | Basic pick and place of an inverted object | 10 |
| Fruit to Basket | Put the fruit in the basket. | Grasping and container placement | 10 |
| Marker to Bowl | Put the marker in the bowl. | Grasping elongated objects and handling orientation | 10 |
| Spoon to Bowl | Pick up the spoon and put it in the bowl. | Grasping small, thin objects and precise placement | 10 |
| Cup to Basket | Put the cup in the basket. | Grasping upright objects and transporting them into a container | 10 |
| Two-object Rearrangement | Put the upside-down cup on the plate, then put the fruit in the basket. | Object-to-target association and two-stage task execution | 10 |
| Plug Insertion | Insert the plug into the socket. | Precise alignment, insertion and contact manipulation | 10 |

Two-object Rearrangement is one task. Each of its 10 trials includes both
placements in the stated order; the two stages are not separate trials.

## Evaluation scope

Scoring, the baseline gate, qualification and the champion line are defined in
[REAL_TRACK.md](REAL_TRACK.md#qualification-and-the-champion); use the selected
season's published, locked success criteria. Sharing the task set
does not merge the two tracks' results or make one model family's baseline
valid for the other. This task catalog does not change an already locked season.

Each evaluated trial is tied to its competition, model revision, task,
trial record, outcome and video. Workcell faults remain distinguishable from
model failures under the [recording rules](REAL_TRACK.md).

The green-block [baseline task](REAL_TRACK.md#baseline-task-the-qualification-gate)
runs before these seven tasks as the qualification gate; it is not an eighth
scored task. The task catalog is not a training dataset or
evidence of measured model performance.

## Related documentation

- [Parallel real-robot tracks](REAL_TRACKS.md)
- [Workstation model interface](WORKSTATION_DATA.md)
- [Real-robot miner guide](MINER_REAL.md)
- [Qualification, rewards and challenges](REAL_TRACK.md)
