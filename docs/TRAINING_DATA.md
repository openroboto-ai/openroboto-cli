# Public training data

Browse the [Open Data Pool](https://www.openroboto.ai/#/datapool) for public
OpenRoboto and partner datasets. A dataset's publication does not change the
active competition, its required model interface or its evaluation task set.

## AXIS Franka

[openroboto-ai/axis-franka-datapool](https://huggingface.co/datasets/openroboto-ai/axis-franka-datapool)
is a Franka Panda simulation dataset. The `v0.2-metadata` release contains:

| Field | Value |
|---|---|
| Tasks | 200 |
| Complete episodes | 35,396 |
| Frames | 5,553,077 |
| Format | LeRobot v3.0 |
| State | 9D joint position |
| Action | 9D joint-position controller target |
| Nominal source rate | 30 Hz; source static-frame filtering retained |
| Included | States, actions, language instructions, episode metadata, statistics and selection manifest |
| Images and videos | Not included in this release |
| Projected total including future video | 20 GB; estimate, not current hosted size |

The expansion preserves the original 16,815 episodes, their indices and files.
The original release remains available at `v0.1-metadata`.

The projected 20 GB includes future video from the 256 × 256 head camera only;
wrist and depth streams are excluded. It is a budget estimate, not a measured
video encode or the current download size. Actual hosted size is reported
separately on the Data Pool page.

### Representation and compatibility

Both native vectors use this order:

```text
[finger_1, finger_2, arm_joint_1, ..., arm_joint_7]
```

Actions are joint-position controller targets, not Cartesian deltas. Original
numeric values are retained without normalization, resampling or action-space
conversion in the native release. The dataset card and `loader.py` describe
adapter-specific conventions separately.

AXIS is kept separate from LIBERO because their action spaces differ. It is also
not a drop-in dataset for the [7D xArm workstation interface](WORKSTATION_DATA.md).
Transfer requires a validated adaptation of the embodiment and action space;
matching a vector length alone does not establish compatibility. This metadata
release contains no visual observations for image-conditioned training.

### Download and load

The examples pin the published metadata commit
`6cbc21d4f06cbb5289d192a4e6b2365f3b5f4278` for reproducibility.

```bash
hf download openroboto-ai/axis-franka-datapool \
  --repo-type dataset \
  --revision 6cbc21d4f06cbb5289d192a4e6b2365f3b5f4278 \
  --local-dir ./axis-franka-datapool
```

Install `lerobot>=0.4`, `torch` and `numpy`, then load without videos:

```python
from lerobot.datasets.lerobot_dataset import LeRobotDataset

dataset = LeRobotDataset(
    "openroboto-ai/axis-franka-datapool",
    revision="6cbc21d4f06cbb5289d192a4e6b2365f3b5f4278",
    download_videos=False,
)
```

The [dataset card](https://huggingface.co/datasets/openroboto-ai/axis-franka-datapool/blob/6cbc21d4f06cbb5289d192a4e6b2365f3b5f4278/README.md),
`meta/info.json` and `selection_manifest.json` at the selected revision define
the release and its provenance. Read the partner
[release terms](https://huggingface.co/datasets/openroboto-ai/axis-franka-datapool/blob/6cbc21d4f06cbb5289d192a4e6b2365f3b5f4278/TERMS.md)
before use. Detailed release documentation is maintained in the dataset repository.
