# Workstation Model Interface and Deployment Protocol

本文档定义 miner 模型与真实机器人 workstation 之间的统一接口。Miner 只需要遵守本文档规定的 observation、state、action、normalization 和 control timing。机器人 SDK、硬件寄存器转换以及底层安全检查均由 workstation 负责。

文档中标记为 **TODO** 的内容需要在正式发布前确定。

---

## 1. Action Convention

### 1.1 Action Space

动作空间采用 joint-space position control，而不是 end-effector pose control，因此不涉及 end-effector 的平移坐标系、旋转表示、旋转轴顺序或旋转组合方式。

对于 xArm6 和单自由度夹爪，模型输出共包含 7 个维度，顺序固定为：

`[Δq1, Δq2, Δq3, Δq4, Δq5, Δq6, g]`

其中：

- `Δq1` 到 `Δq6` 分别表示 xArm6 六个关节的 delta joint position target。
- `g` 表示夹爪的 absolute position target，不采用 delta 表示。
- `q1` 到 `q6` 的物理单位统一为 radian。
- `q1` 到 `q6` 按照 xArm6 原生关节顺序排列，从基座侧 Joint 1 到腕部 Joint 6。
- 夹爪只有一个 action dimension。

模型实际输出的是经过 normalization 后的 action。所有 7 个维度根据对应 checkpoint 的 `norm_stats.json` 进行 denormalization 后，才能转换为真实机器人控制量。

### 1.2 Delta Definition

一个 action chunk 中所有 `Δq` 均相对于**产生该 action chunk 时读取到的同一个 robot state**定义。

假设一次 inference 输出一个 action chunk，则 action 0、action 1、action 2 等所有 action 中的 `Δq` 都使用本次 inference 开始时读取到的 joint position 作为 reference。

执行 action chunk 的过程中，不会为每一个 action 重新读取 current joint state 并重新计算 reference。

因此，workstation 的处理方式为：

1. inference 开始时读取一次 reference joint state；
2. 模型生成整个 action chunk；
3. 对整个 action chunk 进行 denormalization；
4. 对 action chunk 中每一个 action，使用同一个 reference joint state 加上对应的 `Δq`，得到该 action 的 absolute joint target；
5. 将这些 absolute joint targets 依次发送给 xArm。

action chunk 内的 `Δq` 不是相对于前一个 action，也不进行逐步累计。

### 1.3 Model-to-SDK Conversion

Miner 不直接调用 xArm SDK。

Workstation 会将模型输出转换为 absolute joint position targets，并直接发送到 xArm 的 joint servo position interface。

机器人反馈的 joint state 统一转换为 radian 后提供给模型。

Miner 不需要处理：

- xArm SDK 内部单位转换；
- xArm communication protocol；
- Inspire-Robots 原始寄存器；
- gripper hardware value conversion。

### 1.4 Scaling and Clipping

模型 action 根据 `norm_stats.json` 进行 normalization 和 denormalization。

π0.5 使用 quantile normalization，使每个 action dimension 的 1% quantile 和 99% quantile 分别对应约 -1 和 +1。

模型侧不额外定义其他 action scaling。

模型输出不会因为超过 -1 到 +1 而进行额外 clipping。

Workstation 也不会通过 clipping 把危险 action 强行压回安全范围。若 denormalization 后的 target 超出机器人安全限制，则由 workstation 拒绝该指令并报告 safety error。

---

## 2. State Vector

模型使用的 proprioceptive state 共包含 7 个维度：

`[q1, q2, q3, q4, q5, q6, g]`

其中：

- `q1` 到 `q6` 为当前实际读取到的 absolute joint position；
- joint position 单位统一为 radian；
- `g` 为当前 absolute gripper position；
- 不提供 end-effector pose；
- 不提供 end-effector rotation；
- 不提供 joint velocity；
- 不提供 joint torque；
- 不提供 force / torque sensor information。

state 在输入 π0.5 前，根据对应 checkpoint 的 `norm_stats.json` 进行 normalization。

---

## 3. Gripper Action

夹爪在 state 和 action 中均只有一个维度。

Canonical gripper position 定义为连续的 0 到 1：

- `0` 表示 fully open；
- `1` 表示 fully closed；
- 0 到 1 之间表示连续的中间开合位置。

action 中的 `g` 为 absolute gripper position target，不使用 delta representation。

模型内部的 gripper dimension 与其他 action dimensions 一样，根据 `norm_stats.json` 进行 normalization 和 denormalization。

Inspire-Robots EG2-4C2 的实际硬件 position value 与 canonical `g` 之间的转换完全由 workstation 处理，miner 不需要关心硬件寄存器的原始范围。

---

## 4. Action Chunk and Control Timing

### 4.1 Prediction Horizon

π0.5 每次 inference 输出 50 个 future actions。

因此：

`action chunk shape = 50 × 7`

Prediction horizon 固定为 50，不由 miner 修改。

### 4.2 Execution Horizon

每次模型预测 50 个 actions，但 workstation 不执行完整的 50 个 actions。

默认执行 action chunk 中的前 25 个 actions，然后重新读取最新 observation 和 state，并进行下一次 inference。

因此：

- Prediction horizon：50 steps
- Execution horizon：25 steps

未被执行的后 25 个 actions 直接丢弃。

### 4.3 Control Frequency

当前推荐的 robot action execution frequency 为：

**50 Hz**

即每 20 ms 向 xArm 发送一个新的 joint position target。

在 50 Hz 和 25-step execution horizon 下，每次模型预测产生约 0.5 秒实际执行时间。

模型本身并不以 50 Hz 运行 inference。正常情况下，每执行 25 个 robot actions 后才进行一次新的 inference。

**TODO：在正式发布前，对当前 xArm6 workstation 进行 50 Hz joint servo streaming test，验证 command timing 和 jitter 是否稳定。**

如果实机验证表明 50 Hz 无法稳定运行，应在正式数据采集和模型 fine-tuning 开始前统一修改 control frequency。训练数据、fine-tuning 和 evaluation 必须始终采用相同的 control frequency。

### 4.4 Synchronous Inference

当前采用 synchronous action chunk execution，不使用 asynchronous inference、temporal ensemble 或 Real-Time Chunking。

完整流程如下：

1. 读取最新 robot state 和 RGB image；
2. 进行一次模型 inference，得到 50-step action chunk；
3. 以固定 control frequency 执行前 25 个 actions；
4. 执行完第 25 个 action 后停止推进当前 action chunk；
5. 机器人保持最后一个 target position；
6. 重新读取最新 observation 和 state；
7. 进行下一次 inference；
8. 重复上述过程。

模型 inference 与 robot action execution 不 overlap。

当前 protocol 不包含 inference timeout。模型部署在本地，workstation 等待当前 inference 完成后再开始执行新的 action chunk。

---

## 5. Camera Configuration

### 5.1 Camera Setup

评测只使用一个固定的第三人称 RGB 相机。

配置如下：

- Camera：Intel RealSense D415
- Number of cameras：1
- View：third-person
- Mounting：固定安装在机器人正面，面向机器人和主要操作区域
- Color order：RGB
- Wrist camera：无
- Depth input：不提供给模型
- Additional camera：无

模型只有一个视觉 observation。

### 5.2 Camera Streaming

D415 在整个 evaluation episode 中保持持续 streaming，不会在每次模型 inference 时重新初始化相机。

Raw RGB stream 固定为：

- Resolution：640 × 480
- FPS：30
- Color：RGB

模型每次需要 observation 时，从持续运行的 camera stream 中读取最新的一帧。

因此 camera FPS 和 model inference frequency 是两个不同的概念。相机持续以 30 FPS 工作，而模型只在需要进行下一次 inference 时采样最新图像。

### 5.3 Model Image Input

π0.5 的模型输入图像大小固定为：

**224 × 224**

Raw 640 × 480 RGB image 使用 OpenPI-compatible `resize_with_pad` 方式转换为 224 × 224。

默认不进行额外 center crop 或其他人工 ROI crop。

预处理流程固定为：

`D415 RGB 640×480 → resize with padding → RGB 224×224 → π0.5`

---

## 6. Camera Calibration

### 6.1 Camera Intrinsics

正式 workstation 将公布实际部署的 D415 在 640 × 480 RGB stream 下读取到的 factory-calibrated intrinsics，包括：

- fx
- fy
- cx
- cy
- distortion model
- distortion coefficients

**TODO：从最终实际部署的 D415 读取并记录 640 × 480 RGB profile 的 intrinsics。**

由于模型输入图像采用 resize-with-padding，workstation 同时公开完整的 image preprocessing procedure。

### 6.2 Camera Extrinsics

需要公布实际 D415 camera frame 与 xArm6 base frame 之间的 fixed extrinsic transformation。

该参数由实际相机安装位置决定。

**TODO：相机最终固定安装后，完成 D415 camera-to-xArm-base extrinsic calibration，并公布结果。**

---

## 7. Robot Hardware

### 7.1 Robot Arm

Robot arm：

**UFACTORY xArm6**

机械臂为固定基座的 6-DoF robot arm。

Miner-facing interface 不依赖特定 xArm firmware 或 SDK version。

所有 xArm SDK communication 均由 workstation 负责。

### 7.2 Gripper

Gripper：

**Inspire-Robots EG2-4C2**

模型只看到一个 canonical gripper position dimension。

硬件通信、寄存器以及 physical position conversion 均由 workstation 负责。

### 7.3 TCP Offset

需要确定机械臂 flange 到实际夹爪 TCP 的 fixed offset。

TCP offset 不参与模型 joint-space action 的直接解释，但用于 workstation geometry 和 safety checking。

**TODO：完成 EG2-4C2 最终安装后，测量并记录 TCP offset。**

### 7.4 Initial Pose

所有 evaluation episode 使用统一的 robot initial pose。

Initial pose 使用 6 个 absolute joint positions 表示，单位为 radian。

**TODO：确定并发布统一的 xArm6 initial joint pose。**

---

## 8. Safety Constraints

所有机器人安全限制统一由 workstation 执行。

当前启用以下速度限制：

- **TCP speed limit：250 mm/s**
- **Joint speed limit：90°/s**

上述限制在整个 evaluation 过程中始终开启。

此外，workstation 将定义统一的 **Cartesian safety boundary**。该边界覆盖桌面/下方、机器人后方以及左右侧向的安全范围，并要求机械臂的任何部分都不得超出该边界。Joint targets 同时不得超出 xArm6 的合法 joint limits。

如果任何动作导致机械臂超出 Cartesian safety boundary、合法 joint limits 或其他已启用的硬件安全限制，workstation 将拒绝继续执行，并将该 episode 直接判定为 **failure**。Workstation 不通过 clipping 修改模型 action 以规避越界。

**TODO：测量并确定最终 Cartesian safety boundary 的具体数值范围，包括桌面/下方、后方以及左右侧向边界。**

---

## 9. Normalization

### 9.1 Normalization Rule

π0.5 的 state 和 action 均使用 checkpoint 对应的 `norm_stats.json` 进行 normalization。

本接口采用 OpenPI π0.5 的 quantile normalization convention。

对于每一个 state 和 action dimension，训练数据的 1% quantile 和 99% quantile 分别映射到约 -1 和 +1。

### 9.2 Normalization Position

数据处理顺序固定为：

`hardware value → canonical physical representation → delta joint conversion → normalization → π0.5`

模型输出处理顺序固定为：

`π0.5 → denormalization → physical Δq / gripper target → absolute joint target conversion → safety check → robot`

因此 action 的 normalization statistics 对应：

`[Δq1, Δq2, Δq3, Δq4, Δq5, Δq6, g]`

而不是 absolute joint target。

### 9.3 norm_stats.json

每个 checkpoint 必须同时提供其训练过程中实际使用的、OpenPI-compatible `norm_stats.json`。

文件保持 OpenPI 原生 schema，包括：

```json
{
  "norm_stats": {
    "state": {
      "mean": [],
      "std": [],
      "q01": [],
      "q99": []
    },
    "actions": {
      "mean": [],
      "std": [],
      "q01": [],
      "q99": []
    }
  }
}
```

对于当前 xArm6 + single gripper setup，所有 state 和 action statistics arrays 均包含 7 个值，并按照本文档已经规定的 state/action 顺序解释。

不额外要求 normalization type、norm stats version 或 state/action reorder metadata。

提交的 `norm_stats.json` 必须与提交 checkpoint 实际 fine-tuning 时使用的 normalization statistics 完全一致。

---

## 10. Task Specification

每个公开 evaluation task 均包含以下三项完整信息：

1. **Task Name**  
   任务的唯一名称。

2. **Language Prompt**  
   实际提供给模型的固定 language instruction。Evaluation 时使用的 prompt 与公开版本保持一致。

3. **Example Video**  
   提供一段对应 task 的示例视频，用于说明：
   - task 的具体内容；
   - 机器人需要完成的大致操作过程；
   - 什么样的最终状态可以被视为完成或成功。

正式 task release 中，每个 task 均按照上述格式完整公布。

---

## 11. Miner and Workstation Responsibilities

### Miner Responsibilities

Miner 负责：

- 使用规定的 7D state/action interface；
- 输出 50-step action chunk；
- 使用对应 checkpoint 的 `norm_stats.json`；
- 保证 checkpoint 与 norm stats 配套；
- 接收单个第三人称 RGB observation；
- 使用每个 task 对应的固定 language prompt；
- 不自行修改 action semantics、prediction horizon 或 control convention。

### Workstation Responsibilities

Workstation 负责：

- xArm SDK communication；
- Inspire-Robots gripper communication；
- robot state acquisition；
- radian conversion；
- gripper hardware value conversion；
- normalization / denormalization pipeline integration；
- delta joint action 到 absolute joint target 的转换；
- fixed-frequency joint target execution；
- D415 image acquisition；
- image resize and padding；
- TCP speed limit enforcement；
- joint speed limit enforcement；
- Cartesian safety boundary checking；
- TCP configuration；
- initial pose control。

---

## 12. Remaining TODOs

正式发布前仍需要完成以下事项。

### Control

- **TODO：验证当前 xArm6 workstation 是否能够稳定进行 50 Hz joint servo streaming，并记录实际 command timing 和 jitter。**
- **TODO：如果 50 Hz 无法稳定运行，在正式数据采集和 fine-tuning 开始前确定最终 control frequency，并统一应用于训练和 evaluation。**

### Camera Calibration

- **TODO：读取实际 D415 在 640 × 480 RGB profile 下的 intrinsics。**
- **TODO：完成 D415 camera-to-xArm-base extrinsic calibration。**

### Robot Geometry

- **TODO：测量 EG2-4C2 安装后的 TCP offset。**
- **TODO：确定统一 initial joint pose。**

### Safety Boundary

- **TODO：测量并确定最终 Cartesian safety boundary 的具体数值范围，包括桌面/下方、后方以及左右侧向边界。**
