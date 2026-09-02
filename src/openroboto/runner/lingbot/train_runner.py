"""
LingBot-VLA 2.0 Runner — isolated training container entry

Same job as `../train_runner.py` does for π₀.₅, same contract (red line #2):
the host mounts data and a strategy script, this process builds a base model,
and hands it over as

    def train(cfg: dict, episodes: list, policy) -> (metrics, proof)

Usage:
    docker run --gpus all \
      -v /host/data:/data/input \
      -v /host/output:/data/output \
      -v /host/strategy.py:/data/scripts/strategy.py \
      -e CUSTOM_TRAIN=/data/scripts/strategy.py \
      -e TRAIN_DATA=/data/input/train.json \
      -e OUTPUT_DIR=/data/output \
      -e EPOCHS=3 -e BATCH_SIZE=4 -e LR=1e-4 -e LORA_R=32 -e LORA_ALPHA=64 \
      lingbot-runner:latest


What `policy` is
================
A `torch.nn.Module` — LingBot's own `PreTrainedModel`, built by their
`build_foundation_model()`, loaded in **bfloat16**, base **frozen**, with
**LoRA adapters injected** by their own `add_lora_to_model()`. Its
`.processor` attribute holds the Qwen3-VL processor, because without one a
strategy script cannot turn an episode into model input.

So a miner's script does the ordinary things to it:

    policy.train()
    opt = torch.optim.AdamW([p for p in policy.parameters() if p.requires_grad])
    loss = policy(**batch).loss
    loss.backward(); opt.step()

and exports with `merge_lora_and_export(policy, cfg["output_dir"])` below.
That is the whole reason for this shape: the π₀.₅ runner hands over an openpi
policy object, and a strategy script written against one is written against
the other. Nothing in red line #2 moves.


Four decisions, and why
=======================
**1. Build in a single process. No `init_process_group`, no FSDP2.**
LingBot's own `tasks/vla/train_lingbotvla.py` builds the model *after*
`dist.init_process_group("nccl")` and immediately shards it with
`build_parallelize_model()` — which is what made "hand a built model across a
process boundary" look impossible. It is not: `build_foundation_model()`
touches `torch.distributed` in exactly one place, `get_parallel_state()`, and
that function is written to work uninitialised (it warns once and returns a
default `ParallelState`). Sharding is a separate call this runner never makes.
One process, one card, one model object — which is the only shape that can be
passed to `train(cfg, episodes, policy)`.

🔴 **The trap that comes with it**, and the reason `init_device` is not
configurable here: uninitialised, `ParallelState.global_rank` is **-1**, not 0.
`build_foundation_model()` decides whether to skip loading weights with

    if (init_device == "cpu" and get_parallel_state().global_rank != 0) or init_device == "meta":
        empty_init = True

so `init_device="cpu"` in a single process means **-1 != 0 → empty_init →
randomly initialised 6.38 B parameters, with no error and no warning**. Hours
of training later the export is noise. `"cuda"` is the only value this code
passes for a real run; `"meta"` is allowed for a structure-only smoke check
(`scripts/verify_lingbot_runner.py`), which never loads weights by design.

**2. Load in bfloat16.** The published checkpoint is fp32 — 6,375,907,511
parameters × 4 bytes = 25.5 GB, and a full-parameter fine-tune of it needs
weights + gradients + AdamW moments ≈ 102 GB, which is why the vendor's own
minimum is 4 × A6000 at ~49 GB each. bf16 halves the weights to 12.8 GB, and
with the base frozen there are no fp32 gradients or optimiser moments for it.
`build_foundation_model(torch_dtype="bfloat16")` does this natively (it is
`getattr(torch, torch_dtype)`), so the fp32 copy is never materialised.

**3. Freeze the base, train LoRA.** LingBot ships `peft==0.15.2` in
`requirements.txt` and a working `add_lora_to_model()` in
`lingbotvla/utils/lora_utils.py`, and **calls it from nowhere** — their own
recipe is a full-parameter fine-tune. Wiring it up is what brings the arithmetic
onto one card: 12.8 GB frozen bf16 base + ~0.2 GB LoRA parameters + ~0.6 GB of
optimiser state for them + activations ≈ 14–18 GB.
⚠️ Note the export consequence: a LoRA adapter on its own is **not** a
submittable artifact (`openroboto check` rejects `BARE_LORA_ADAPTER`, and
nothing merges it later — not this CLI, not the evaluator). See
`merge_lora_and_export()`.

**4. `episodes` stays what it has always been.** LingBot's official trainer
reads a LeRobot dataset *directory* (parquet + mp4 + `meta/info.json`); this
runner does not use their data pipeline at all, so that never comes up. The
strategy script receives the same decoded JSON episode list the π₀.₅ runner
passes, and uses `policy.processor` to turn it into model input. This is the
difference that lets the container interface stay fixed.


Verified on a GPU, 2026-08-26
=============================
`scripts/verify_lingbot_runner.py`, A100-SXM4-80GB, driver 580.126.09, all
stages green — which is what puts `adapters.sim_lingbot.training` at `DOCKER`.
Five of the six things this runner depends on are observed, not argued:

  2. `build_foundation_model()` completes with no process group ✅ — single
     process, `global_rank=-1`, `world_size=1`, no `init_process_group`, no
     FSDP2. Concluding that the vendor's training entry point "cannot fit the
     container interface" rests on the opposite claim, and it is false.
  3. `LORA_TARGET_MODULES` matches real modules ✅ — 396 of them, 38.9 M
     trainable parameters. It did *not* match on the first run; the vendor's
     signature default names another architecture entirely. See below.
  4. `moe_implementation="fused"` works outside their sharded setup ✅
  5. torch 2.8.0's cu128 wheel runs on the CUDA 12.4 runtime base image ✅
  6. `merge_lora_and_export()` produces a flat checkpoint root ✅

⚠️ Item 1 is **still arithmetic.** Measured peak was 12.4 GiB and that is
weights only, before any batch: the verification builds, merges and exports,
it never runs a training step. The 14–18 GiB figure below is that 12.4 plus an
estimate for optimizer state and activations. On a 24 GB card it leaves
11.6 GiB of headroom, which is a miner's report to make, not this run's.

⚠️ Seven of the vendor's defaults had to be overridden to get here, and they
share one shape: **the vendor's own entry point never takes this path**, so
those defaults have never executed anywhere. Each is commented at its call
site with the symptom, the vendor `file:line`, and why the fix is what it is.
Expect an eighth the first time anything here meets a real batch.
"""

import json
import logging
import os
import sys

logger = logging.getLogger(__name__)


# ─── Vendor pins ──────────────────────────────────────────
# Kept next to each other rather than spread through the code: these five
# values are the entire "which model is this" answer, and the failure mode of
# getting one wrong is a training run that finishes on the wrong base.

BASE_MODEL_REPO = "robbyant/lingbot-vla-v2-6b"
BASE_MODEL_REVISION = "11c703bf6a5c1f45b3b69168482da11fdbba53d7"
"""Pinned to a commit, not to `main`.

`openroboto_protocol.model_format` fingerprints this exact revision, and
`docs/MIGRATION.md` names it — a floating revision would mean the miner trained
on one base model and is judged against another.
"""

PROCESSOR_REPO = "Qwen/Qwen3-VL-4B-Instruct"
"""The tokenizer/processor half, ~8.9 GB, a **separate** repository.

The base checkpoint cannot supply it: `config.json` in that repo holds exactly
one key, `{"vlm_family": "qwen3_vl"}` — no `model_type`, no `architectures`,
and not a single `.py` in the whole repository. `AutoModel` and
`trust_remote_code` both fail on it; only LingBot's own code can read it.
"""

CONFIG_KEY = "LingbotVLAV2Config"
"""Which config class `get_config_registry()` hands back.

The value the vendor's own `configs/vla/robotwin/robotwin.yaml` uses. Their
`ModelArguments` default is `LingbotVLAConfig` — the **v1** class — so leaving
it unset would build a different architecture against v2 weights.
"""

MOE_IMPLEMENTATION = "fused"
"""Also from `robotwin.yaml`. ⚠️ Unverified outside their sharded setup; if the
fused kernels turn out to need something FSDP2 sets up, `"eager"` is the
documented alternative (`build_foundation_model` validates the pair)."""

LORA_TARGET_MODULES = "q_proj,k_proj,v_proj,o_proj"
"""The attention projections, read off this checkpoint's own tensor names.

🔴 **Not** `add_lora_to_model`'s signature default of `"q,k,v,o,ffn.0,ffn.2"`.
Those names belong to some other architecture; on an A100 (2026-08-26) peft
answered

    ValueError: Target modules {'o', 'v', 'k', 'ffn.0', 'ffn.2', 'q'} not
    found in the base model.

The vendor never calls `add_lora_to_model` anywhere, so its default has never
been matched against anything.

Derived from `model.safetensors.index.json`, counting leaf module names across
all 1708 tensors: `q_proj` 108, `k_proj` 108, `v_proj` 108, `o_proj` 72.
(`gate_proj` / `up_proj` / `down_proj`, 108 each, are the MLP half — left out
so the adapter stays small; adding them is a knob, not a fix.)
"""


# ─── Config from env ──────────────────────────────────────


def get_config() -> dict:
    """Read training config from environment variables.

    🔴 Identical to the π₀.₅ runner's `get_config()`, key for key. The host
    side (`training/container.py::build_docker_command`) sets one fixed list of
    environment variables for every competition, and a strategy script reads
    `cfg` — so a key that exists in one runner and not the other is a script
    that works in one image and raises `KeyError` in the other.
    `tests/test_lingbot_runner.py` compares the two.

    Returns:
        Config dict containing checkpoint_path, train_data, epochs, batch_size, etc.
    """
    return {
        "checkpoint_path": os.getenv("CHECKPOINT_PATH", "/data/cache/lingbot_base"),
        "train_data": os.getenv("TRAIN_DATA", "/data/input/train.json"),
        "val_data": os.getenv("VAL_DATA", ""),
        "output_dir": os.getenv("OUTPUT_DIR", "/data/output"),
        "epochs": int(os.getenv("EPOCHS", "3")),
        "batch_size": int(os.getenv("BATCH_SIZE", "4")),
        "learning_rate": float(os.getenv("LR", "1e-4")),
        "warmup_ratio": float(os.getenv("WARMUP_RATIO", "0.05")),
        "lora_r": int(os.getenv("LORA_R", "32")),
        "lora_alpha": int(os.getenv("LORA_ALPHA", "64")),
        "hotkey": os.getenv("HOTKEY", "unknown"),
    }


# ─── Where the weights come from ──────────────────────────


def resolve_weights(
    repo_id: str, revision: str, checkpoint_path: str, local_name: str
) -> str:
    """A local directory for `repo_id`: the mounted checkpoint if it is there,
    otherwise a download.

    Three tiers, cheapest first:

    1. `checkpoint_path` itself, when it is already a directory — that is what
       `CHECKPOINT_PATH` means for the base model.
    2. a sibling of it named `local_name`. `build_docker_command()` mounts
       `CHECKPOINT_PATH`'s **parent** at `/data/checkpoint`, so putting both
       model roots in one directory pre-loads ~34 GB through a mount that
       already exists — no change to red line #2's mount list.
    3. `snapshot_download`, into `HF_HOME` (`/data/cache` in the image).

    Tier 3 is correct but expensive, and expensive in a place a miner cannot
    see: without a `-v` for the cache it lands in the container's writable
    layer and is re-fetched next run.
    """
    if os.path.isdir(checkpoint_path) and local_name == os.path.basename(
        checkpoint_path.rstrip("/")
    ):
        return checkpoint_path

    sibling = os.path.join(os.path.dirname(checkpoint_path.rstrip("/")), local_name)
    if os.path.isdir(sibling):
        logger.info("📦 %s: using the mounted copy at %s", repo_id, sibling)
        return sibling

    from huggingface_hub import snapshot_download

    logger.info(
        "⬇️  %s@%s: not mounted, downloading (this is tens of GB)",
        repo_id,
        revision[:12] if revision else "main",
    )
    return snapshot_download(repo_id=repo_id, revision=revision or None)



def _yaml_number(value):
    """`"1e-4"` -> `0.0001`. Everything else is returned untouched.

    🔴 `yaml.safe_load` follows the **YAML 1.1** resolver, whose float pattern
    requires a dot or a sign in the exponent, so a bare `1e-4` comes back as a
    `str`. The vendor's `robotwin.yaml` writes three of its coefficients that
    way (`router_z_loss_coeff: 1e-4`, `sequence_wise_loss_coeff: 1e-3`,
    `lr: 1.0e-4` -- only the last one survives), and this runner reads that file
    as *data* rather than through their argument parser, which is what would
    otherwise have cast them.

    Left uncast, the string travels onto the model config and the first
    training step dies inside the vendor's own MoE loss with

        TypeError: '>' not supported between instances of 'str' and 'int'

    (`modeling_lingbot_vla_v2.py:1077`, `router_z_loss_coeff > 0`). Measured on
    an A100, 2026-08-26. `miner.yaml`'s own `learning_rate` comment warns about
    exactly this trap; the recipe file needed the same treatment.
    """
    if not isinstance(value, str):
        return value
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            continue
    return value


# ─── Building the policy ──────────────────────────────────


def build_policy(cfg: dict, init_device: str = "cuda"):
    """Build the LingBot base model, freeze it, inject LoRA, return it.

    This is the LingBot answer to what the π₀.₅ runner does with
    `create_trained_policy()`. It calls only LingBot's own entry points —
    nothing about the architecture is reimplemented here, because the only code
    that can read that checkpoint is theirs.

    One step of theirs is deliberately **not** performed: norm stats. The
    vendor computes them in a second `torchrun` job over a LeRobot dataset
    directory (`scripts/compute_norm_stats.py`), and this runner has no such
    directory — see decision 4 in the module docstring. Normalisation belongs
    to the strategy script, next to the batching it already has to do.

    Args:
        cfg: config dict from `get_config()`; `lora_r` / `lora_alpha` are used.
        init_device: `"cuda"` for a real run, `"meta"` for a structure-only
            check with no weights. 🔴 **Never `"cpu"`** — see below.

    Returns:
        A `torch.nn.Module` in bf16 with trainable LoRA parameters, carrying
        the Qwen3-VL processor on `.processor`.
    """
    if init_device == "cpu":
        # See the module docstring, decision 1. This is a guard against a
        # silent wrong answer, not against a typo: `"cpu"` is the obvious
        # value to reach for on a machine without a card, it is accepted by
        # every signature on this path, and what it produces is a model full of
        # random numbers that trains and exports without one complaint.
        #
        # It is the **first statement in the function**, before the imports, so
        # that it raises everywhere -- including on a machine with no torch at
        # all, which is exactly where somebody reaches for "cpu".
        raise ValueError(
            "init_device='cpu' skips weight loading in a single process: "
            "without a process group `get_parallel_state().global_rank` is -1, "
            "and build_foundation_model() reads `global_rank != 0` as "
            "`empty_init=True`. Use 'cuda' to train, or 'meta' to check the "
            "structure without weights."
        )

    import lingbotvla
    import torch
    from lingbotvla.models import build_foundation_model, build_processor
    from lingbotvla.models.config_registry import get_config_registry
    from lingbotvla.utils.arguments import ModelArguments, TrainingArguments
    from lingbotvla.utils.lora_utils import add_lora_to_model, freeze_parameters

    weights_repo, weights_rev = _addressed(
        "BASE_WEIGHTS", BASE_MODEL_REPO, BASE_MODEL_REVISION
    )
    base_weights = resolve_weights(
        weights_repo,
        weights_rev,
        cfg["checkpoint_path"],
        "lingbot-vla-v2-6b",
    )
    processor_repo, processor_rev = _addressed("PROCESSOR", PROCESSOR_REPO, "")
    processor_path = resolve_weights(
        processor_repo, processor_rev, cfg["checkpoint_path"], "Qwen3-VL-4B-Instruct"
    )

    # The vendor's own two lines: `config_kwargs = {**vars(args.model),
    # **vars(args.train)}`, then the registry turns a config key into a config
    # class and that gets instantiated with all of it. Building the two
    # dataclasses instead of a hand-written dict is what keeps their defaults
    # and their `__post_init__` validation in play -- a dict would silently
    # drop whichever field they add next.
    model_args = ModelArguments(
        config_key=CONFIG_KEY,
        # 🔴 Not None, even though the annotation says `str | None`:
        # `build_foundation_model` does `if 'pi0' in config_path` with no guard,
        # and None raises TypeError there.
        config_path="",
        model_path=base_weights,
        tokenizer_path=processor_path,
        # `adanorm_time` is `true` in the vendor's robotwin.yaml and `False` by
        # dataclass default. It does change the architecture (AdaNorm gamma /
        # beta / gate parameters exist only when it is on), so the default
        # would build something the v2 weights do not fit.
        adanorm_time=True,
        # 🔴 `post_training=False`, against robotwin.yaml, which sets it `true`.
        # Read the next 40 lines before changing it back.
        #
        # Symptom with `post_training=True` (A100, 2026-08-26, stage 5):
        #
        #     KeyError: Unexpected key
        #     'model.current_video_align_head.projector.layers.0.1.0.bias'
        #     found in state dict during Post-Training. This is not allowed!!!
        #
        # Root cause: this checkpoint carries four auxiliary distillation heads
        # -- `depth_align_head`, `future_depth_align_head`,
        # `current_video_align_head`, `future_video_align_head`, 19 tensors each
        # plus one `*_align_embs` -- and we do not build them.
        # `modeling_lingbot_vla_v2.py:478-487` builds them only when
        # `config.align_params != {}`, and ours is empty. 80 of the
        # checkpoint's 1708 tensors therefore have nowhere to land, and
        # `module_utils.py:272` turns that into a KeyError when post_training
        # is on.
        #
        # ⚠️ The obvious reading of this flag -- "post_training changes the
        # architecture, turning it off will silently misplace weights" -- is
        # wrong, and it is worth writing down why, because it is the reason
        # this looked unsafe. `post_training` reaches exactly four places:
        #
        #   1. `loader.py:221` `map_ckpt_key`: `if key.startswith('expert_visual.')
        #      and not post_training: return "model.qwenvl_with_expert." + key`.
        #      🔴 This is the only line in the vendor tree where the flag can
        #      change *where a weight lands*, and it is dead for this
        #      checkpoint: all 1708 tensor names start with `model.`, zero
        #      start with `expert_visual.` (counted from
        #      `model.safetensors.index.json`). With no `expert_visual.` keys
        #      the function returns `key` unchanged either way.
        #   2. `module_utils.py:272`: extra checkpoint key -> raise (on) vs log
        #      and drop (off). This is the one we are turning off on purpose.
        #   3. `module_utils.py:285`: `assert len(parameter_names) == 0` --
        #      every *model* parameter must have been filled. This one is worth
        #      keeping, so it is re-implemented below rather than lost.
        #   4. `optimizer.py:153`: a 10x learning-rate gain on parameters whose
        #      name contains "depth". No depth parameters exist without the
        #      align heads, and this runner does not build the optimizer.
        #
        # `configuration_lingbot_vla.py:115` stores it on the config, and
        # `grep -rn post_training lingbotvla/models/vla/` finds no other hit --
        # the modeling code never reads it back. So it is a checkpoint-loading
        # strictness flag, not an architecture switch.
        #
        # Rejected alternative: pass `align_params` so the four heads get built.
        # It is not just "miners download two more models". The heads change
        # the *forward* contract: `modeling_lingbot_vla_v2.py:846` calls
        # `self.depth_emb_forward(outputs_embeds, depth_targets, img_masks,
        # future_depth_targets)` on every step whenever `align_params != {}`,
        # and those targets are produced by two frozen teacher networks that
        # `build_depth_model` (`vision_models/module_utils.py:71-88`) loads from
        # `moge_path` / `morgbd_path`. Turning the heads on to satisfy a weight
        # loader would oblige every miner batch to carry depth and future-video
        # targets it has no way to produce. Loading auxiliary weights we will
        # never train is the smaller lie than pretending to train them.
        post_training=False,
        moe_implementation=MOE_IMPLEMENTATION,
    )
    # 🔴 torchrun's three variables must be in the environment before this line.
    # `TrainingArguments.__post_init__` reads them back to back with no default
    # and no guard (`lingbotvla/utils/arguments.py`):
    #
    #     self.local_rank  = int(os.getenv("LOCAL_RANK"))
    #     self.global_rank = int(os.getenv("RANK"))
    #     self.world_size  = int(os.getenv("WORLD_SIZE"))
    #
    # Outside torchrun each is `int(None)` -> TypeError. Their own entrypoint is
    # always launched under torchrun, so a single-process build is a path they
    # do not have; a GPU run on 2026-08-26 walked into them one at a time.
    #
    # 🔴 `WORLD_SIZE` is "1", not "0": the next statement in that same
    # `__post_init__` divides by
    # `pipeline_parallel_size * ulysses_parallel_size * context_parallel_size *
    # tensor_parallel_size` and checks the remainder, so a zero world would trade
    # this TypeError for a modulo on nothing.
    #
    # `setdefault`, not assignment: under torchrun these are already set and
    # correct, and this runner has no business overriding them.
    for name, value in (("LOCAL_RANK", "0"), ("RANK", "0"), ("WORLD_SIZE", "1")):
        os.environ.setdefault(name, value)

    # 🔴 `output_dir` has no default -- it is the one required field on the
    # vendor's `TrainingArguments` (`lingbotvla/utils/arguments.py`, the first
    # `field()` in the class, declared with `metadata` but no `default`).
    # Calling `TrainingArguments()` raises TypeError, which is how a GPU run on
    # 2026-08-26 found this line: stages 4 and 5 both died here before a single
    # weight was read.
    #
    # We pass the container's own output mount rather than inventing a path.
    # Nothing in `build_foundation_model` writes to it -- the value only rides
    # along in `config_kwargs` -- but a wrong path here would be a checkpoint
    # written somewhere the miner never looks.
    # 🔴 `num_train_epochs` is required in practice even though it is typed
    # `Optional[int]`: the same `__post_init__` raises
    # `"At least one of num_train_epochs and max_steps must be specified"`
    # when both are None, and both default to None.
    #
    # ⚠️ Nothing in this runner trains -- the miner's strategy script owns the
    # loop -- so this value never drives an epoch count here. It is passed
    # because the object refuses to exist without it, and it is passed
    # `cfg["epochs"]` rather than a literal so that a miner reading `EPOCHS=3`
    # in `docker run` and a miner reading this file see the same number.
    train_args = TrainingArguments(
        output_dir=cfg["output_dir"],
        num_train_epochs=cfg["epochs"],
    )
    config_kwargs = {**vars(model_args), **vars(train_args)}

    # `build_foundation_model` ends with
    # `weights_path = vlm_repo_id if vlm_repo_id else weights_path`, so a
    # non-empty vlm_repo_id would quietly replace the checkpoint just resolved
    # with a bare VLM -- the wrong base model, no error.
    if config_kwargs.get("vlm_repo_id"):
        raise ValueError(
            f"vlm_repo_id is set to {config_kwargs['vlm_repo_id']!r}, which "
            f"overrides weights_path inside build_foundation_model(): training "
            f"would run on that repository instead of {BASE_MODEL_REPO}."
        )

    config_cls = get_config_registry().get_config_cls_from_config_key(CONFIG_KEY)

    # 🔴 The two dataclasses above cannot describe this checkpoint's
    # architecture, and merging them is not enough. `ModelArguments` has 21
    # fields and `TrainingArguments` 71; between them they declare **none** of
    # `action_dim`, `max_action_dim`, `max_state_dim`, `use_moe`,
    # `token_num_experts`, `token_moe_intermediate_size`,
    # `token_shared_intermediate_size`, `vlm_causal`, `loss_type`,
    # `tokenizer_max_length`, `attention_implementation` or `align_params` --
    # grep any of them in `lingbotvla/utils/arguments.py` and you get nothing.
    # So `{**vars(model_args), **vars(train_args)}` carries no architecture at
    # all, and `LingbotVLAV2Config` falls back to its own signature defaults,
    # which describe a *different, smaller* model than the released weights.
    #
    # Two GPU runs on 2026-08-26 show what that costs:
    #
    #     RuntimeError: The size of tensor a (14) must match the size of
    #     tensor b (55) at non-singleton dimension 0
    #
    # -- `action_dim` defaulting to 14 against `action_in_proj.weight`'s real
    # [768, 55] -- and, once the loader was loosened, 136 orphaned
    # `qwen_expert...mlp.experts.*` / `.shared_expert.*` / `.gate.weight`
    # tensors, because `use_moe` defaults to `False` and the checkpoint is a
    # 32-expert MoE.
    #
    # ⚠️ The vendor's own `robotwin.yaml` puts all of these under `train:`,
    # which their `parse_args` **cannot parse**: it ends with
    # `parser.parse_known_args()` followed by `if remaining_args: raise
    # ValueError(...)` (`arguments.py:953-955`), and it builds its dataclasses
    # from declared fields only. Their shipped config and their shipped parser
    # are from different revisions. This is the same shape as the other five
    # fixes in this file -- nobody upstream has run this path -- except that
    # here it means the yaml is *data*, not something their code can load, so
    # we read it as data.
    #
    # Why read their file instead of transcribing the numbers here: it is the
    # recipe that produced these weights, and every value in it that can be
    # checked against a tensor does check out -- `action_in_proj.weight`
    # [768, 55] and `state_proj.weight` [768, 55] against `action_dim: 55` /
    # `max_state_dim: 55`; `mlp.experts.gate_proj` [32, 512, 768] against
    # `token_num_experts: 32` / `token_moe_intermediate_size: 512`;
    # `shared_expert.gate_proj` [704, 768] against
    # `token_shared_intermediate_size: 704`. A hand-copied constant list would
    # be one vendor bump away from being wrong in exactly the silent way this
    # comment exists to prevent.
    #
    # The filter is the model config's own `__init__` signature, so only keys
    # that describe the model survive; the yaml's training-loop half (lr,
    # optimizer, save_steps, the fsdp2 settings) is dropped rather than
    # smuggled onto the config object.
    #
    # 🔴 The signature has to be collected across the MRO, not off
    # `config_cls.__init__`. `LingbotVLAV2Config.__init__` is `(self, **kwargs)`
    # -- it sets ten `kwargs.setdefault(...)` lines and delegates
    # (`configuration_lingbot_vla.py:181-195`); the ~70 real parameter names
    # live on its parent `LingbotVLAConfig`. Reading only the leaf signature
    # yields `{"self", "kwargs"}`, every yaml key is filtered out, the overlay
    # becomes a no-op, and the failure resurfaces 200 lines later as the same
    # `tensor a (14) ... tensor b (55)` this whole block exists to fix. That
    # cost a GPU run; hence the guard below.
    import inspect

    import yaml

    recipe_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(lingbotvla.__file__))),
        "configs",
        "vla",
        "robotwin",
        "robotwin.yaml",
    )
    with open(recipe_path, encoding="utf-8") as handle:
        recipe = yaml.safe_load(handle)
    understood = {
        name
        for klass in config_cls.__mro__
        for name, param in inspect.signature(klass.__init__).parameters.items()
        if param.kind in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY)
    } - {"self"}
    overlaid = {}
    for section in ("model", "train"):
        overlaid.update(
            {
                k: _yaml_number(v)
                for k, v in recipe.get(section, {}).items()
                if k in understood
            }
        )
    if not overlaid:
        raise RuntimeError(
            f"{recipe_path} contributed no architecture keys to "
            f"{CONFIG_KEY}. Either the recipe moved its `model:` / `train:` "
            f"sections or the config signature stopped naming its parameters; "
            f"either way the model would silently be built from defaults that "
            f"do not match the checkpoint."
        )
    config_kwargs.update(overlaid)
    logger.info(
        "📐 %d architecture keys from %s (action_dim=%s, use_moe=%s)",
        len(overlaid),
        recipe_path,
        overlaid.get("action_dim"),
        overlaid.get("use_moe"),
    )

    # Ours wins over the recipe -- these four are values robotwin.yaml states
    # for the vendor's own cluster run and cannot state for ours.
    #
    # `tokenizer_path` and `output_dir` are `/path/to/...` placeholders in that
    # file. `post_training` is `true` there and the reasoning for `False` is on
    # `ModelArguments` above -- it has to be repeated here because the recipe
    # overlay would otherwise put their value back. `align_params` is the
    # rejected alternative from that same comment: their block names the four
    # distillation heads *and* the two teacher checkpoints that feed them, and
    # an empty dict is what makes `modeling_lingbot_vla_v2.py:487` take the
    # `use_depth_align = False` branch.
    config_kwargs["tokenizer_path"] = processor_path
    config_kwargs["post_training"] = False
    config_kwargs["align_params"] = {}
    config_kwargs["output_dir"] = cfg["output_dir"]

    config = config_cls(**config_kwargs)

    logger.info(
        "🧩 Building %s from %s (%s, %s)",
        CONFIG_KEY,
        base_weights,
        "bfloat16",
        init_device,
    )
    model = build_foundation_model(
        config_path="",
        config_cls=config,
        weights_path=base_weights,
        torch_dtype="bfloat16",
        init_device=init_device,
        config_kwargs=config_kwargs,
        moe_implementation=MOE_IMPLEMENTATION,
    )

    if init_device != "meta":
        # Cheap, and it catches the empty-init trap even if the condition
        # upstream changes shape: real weights are not on the meta device.
        meta = [name for name, p in model.named_parameters() if p.device.type == "meta"]
        if meta:
            raise RuntimeError(
                f"{len(meta)} parameters are still on the meta device after "
                f"loading (first: {meta[0]}) -- the weights were not read. "
                f"This is what empty_init looks like from the outside."
            )

        # 🔴 This is `module_utils.py:285`'s
        # `assert len(parameter_names) == 0` brought back by hand. Setting
        # `post_training=False` above buys us "extra checkpoint keys are
        # tolerated" and charges us this assertion in the same statement; only
        # the first half is wanted.
        #
        # Losing it silently is the expensive direction. `load_model_weights`
        # calls `model.to_empty(device=init_device)` before it reads anything,
        # and `to_empty` *allocates without initialising*. A parameter the
        # checkpoint does not cover therefore keeps whatever bytes were in that
        # GPU page. With post_training on, the assert stops it; with it off,
        # the re-init loop underneath the assert is skipped too, so the model
        # builds, trains, exports, and evaluates as noise -- no traceback
        # anywhere. That is the failure this check exists to make loud.
        #
        # The reimplementation is exact rather than approximate: the vendor
        # seeds `parameter_names` from `model.named_parameters()` and removes
        # one per checkpoint key that maps into it, and `map_ckpt_key` is the
        # identity for this checkpoint (see the `post_training` note above), so
        # "what is left over" is precisely model parameters minus checkpoint
        # keys. Buffers are excluded on both sides -- the vendor handles those
        # through `buffer_dict`.
        #
        # Read from the shard headers rather than `model.safetensors.index.json`
        # so a single-file checkpoint is checked on the same path as a sharded
        # one; `safe_open` maps the header only, not the 27 GB.
        import glob

        from safetensors import safe_open

        shards = sorted(glob.glob(os.path.join(base_weights, "*.safetensors")))
        if not shards:
            raise RuntimeError(
                f"No *.safetensors under {base_weights}, so there is no way to "
                f"confirm every parameter was filled. Refusing to train on "
                f"weights that cannot be checked."
            )
        ckpt_keys: set[str] = set()
        for shard in shards:
            with safe_open(shard, framework="pt") as handle:
                ckpt_keys.update(handle.keys())

        # Before `add_lora_to_model`: the adapter's own parameters are new by
        # definition and are not in any checkpoint.
        param_names = {name for name, _ in model.named_parameters()}
        missing = sorted(param_names - ckpt_keys)
        if missing:
            raise RuntimeError(
                f"{len(missing)} parameters were never filled from "
                f"{base_weights} (first: {missing[0]}). `to_empty()` left them "
                f"uninitialised, so training would run on whatever was in that "
                f"memory. The checkpoint and this config disagree about the "
                f"architecture."
            )
        logger.info(
            "🔎 %d parameters all filled from %d shard(s); %d checkpoint "
            "tensors unused (the align heads we do not build)",
            len(param_names),
            len(shards),
            len(ckpt_keys - param_names),
        )

    freeze_parameters(model)
    # 🔴 `lora_target_modules_support` must be passed, even though its
    # signature default is `None`. The body does
    # `if lora_target_module not in lora_target_modules_support` before it
    # reaches peft (`lingbotvla/utils/lora_utils.py`), so the default makes it
    # `"q" not in None` -> `TypeError: argument of type 'NoneType' is not
    # iterable`. Nothing upstream calls this function -- `grep add_lora_to_model`
    # across the vendor repo finds only the definition -- so that default has
    # never run anywhere, which is why it ships broken.
    #
    # Passing our own list as the support set makes the check a tautology,
    # which is the honest shape: we are asserting these names are the ones we
    # mean, and the real verdict comes from the trainable-parameter count
    # below. A shorter support set would only make this raise earlier with a
    # worse message.
    add_lora_to_model(
        model,
        lora_rank=cfg["lora_r"],
        lora_alpha=cfg["lora_alpha"],
        lora_target_modules=LORA_TARGET_MODULES,
        lora_target_modules_support=LORA_TARGET_MODULES,
    )

    trainable = [p for p in model.parameters() if p.requires_grad]
    if not trainable:
        raise RuntimeError(
            f"LoRA injection left nothing trainable: no module matched "
            f"`{LORA_TARGET_MODULES}`. Training would run, report a loss, and "
            f"export the untouched base model."
        )
    total = sum(p.numel() for p in model.parameters())
    logger.info(
        "🔧 LoRA r=%d alpha=%d | trainable %.1f M / %.2f B (%.3f%%)",
        cfg["lora_r"],
        cfg["lora_alpha"],
        sum(p.numel() for p in trainable) / 1e6,
        total / 1e9,
        100.0 * sum(p.numel() for p in trainable) / max(total, 1),
    )

    # A plain attribute on an nn.Module: not a submodule, not a parameter, so
    # `state_dict()` and `save_pretrained()` do not see it. A strategy script
    # cannot build a batch without it, and making it a second positional
    # argument would break red line #2's signature.
    model.processor = build_processor(processor_path)
    if torch.cuda.is_available():
        logger.info(
            "🖥️  %s | %.1f GiB allocated",
            torch.cuda.get_device_name(0),
            torch.cuda.memory_allocated() / 2**30,
        )
    return model


def merge_lora_and_export(policy, output_dir: str) -> str:
    """Fold the LoRA weights back into the base and write a full checkpoint.

    A strategy script has to call this (or do the same thing itself). A bare
    adapter directory is rejected before payment by `openroboto check` as
    `BARE_LORA_ADAPTER`, and nothing downstream merges it: there is no
    `openroboto merge` and the evaluator merges nothing either. `output_dir`
    **is** the checkpoint root — `openroboto submit` uploads it byte for byte
    as the Hugging Face repository root, so nothing may be nested under it.

    ⚠️ Not verified: that the result is a directory the evaluator can load.
    `scripts/verify_lingbot_runner.py` checks the shape; only an evaluation run
    checks the rest.
    """
    merged = 0
    for module in policy.modules():
        # peft's LoraLayer exposes `merge()`; injected adapters are plain
        # modules rather than a PeftModel wrapper, so there is no
        # `merge_and_unload()` to call here.
        if (
            hasattr(module, "merge")
            and callable(module.merge)
            and hasattr(module, "lora_A")
        ):
            module.merge()
            merged += 1
    logger.info("🧷 Merged %d LoRA layers back into the base", merged)

    os.makedirs(output_dir, exist_ok=True)
    policy.save_pretrained(output_dir, safe_serialization=True)
    processor = getattr(policy, "processor", None)
    if processor is not None:
        processor.save_pretrained(output_dir)
    return output_dir


# ─── Training ─────────────────────────────────────────────


def run_training(cfg: dict) -> tuple:
    """Execute LingBot training, returns (metrics, proof).

    Args:
        cfg: configuration dictionary from get_config()

    Returns:
        (metrics, proof) two dicts
    """
    custom_train = os.getenv("CUSTOM_TRAIN", "")
    if custom_train and os.path.exists(custom_train):
        logger.info("🔧 Using custom training script: %s", custom_train)
        return _run_custom(cfg, custom_train)
    return _run_default(cfg)


def _addressed(env: str, repo: str, revision: str) -> tuple[str, str]:
    """`repo@revision` out of the environment, falling back to the built-in pair.

    🔴 **The season's row wins; the constants below it are only the fallback.**
    The addresses ride in on `BASE_WEIGHTS` / `PROCESSOR`, set by
    `openroboto train` from `params.training`. Constants as the only answer mean
    changing LingBot's base model takes a CLI release and a rebuild on every
    miner's machine -- while a π0.5 season does the same thing by editing one
    field.

    ⚠️ **Empty means: use the base this image was built around.** That is the
    normal case for a workspace written before the field existed, and it has to
    keep meaning exactly that -- anything else breaks every one of those configs.

    ⚠️ One string, split here rather than carried as two variables. `repo@rev`
    cannot drift; a pair of variables can, and "right repository, another
    version's commit" is the failure being avoided -- it trains happily and is
    judged against something else.
    """
    value = os.getenv(env, "").strip()
    if not value:
        return repo, revision
    name, _, rev = value.partition("@")
    return name or repo, rev


def _run_custom(cfg: dict, custom_script: str) -> tuple:
    """Load and execute an externally mounted custom training script.

    Script interface contract -- the same one the π₀.₅ image honours:

        def train(cfg: dict, episodes: list, policy) -> tuple:
            # cfg: config dict read from env vars
            # episodes: loaded training data list
            # policy: LingBot model, bf16, base frozen, LoRA injected
            # return: (metrics_dict, proof_dict)

    Args:
        cfg: configuration dictionary
        custom_script: path to the custom training script

    Returns:
        (metrics, proof) two dicts
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("custom_train", custom_script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    if not hasattr(mod, "train"):
        raise ValueError(
            f"{custom_script} Missing train(cfg, episodes, policy) entry function"
        )

    episodes = _load_episodes(cfg["train_data"])
    logger.info("📊 Loaded %d episodes", len(episodes))
    policy = build_policy(cfg)

    metrics, proof = mod.train(cfg, episodes, policy)

    output_dir = cfg["output_dir"]
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, "metrics.json"), "w") as f:
        json.dump(metrics, f, indent=2)
    with open(os.path.join(output_dir, "proof.json"), "w") as f:
        json.dump(proof, f, indent=2)

    return metrics, proof


def _run_default(cfg: dict) -> tuple:
    """There is no default recipe for this competition, and saying so beats
    inventing one.

    The π₀.₅ image has a default flow because it predates strategy scripts; it
    fakes a loss curve and exports nothing, which is a pipeline smoke test.
    Repeating that here would buy the same nothing at the cost of a second copy,
    and this image cannot even be started without a card. Meanwhile the vendor's
    own recipe is a 32-GPU, 50 000-step full-parameter fine-tune — not a default
    anybody's single machine can run.
    """
    raise RuntimeError(
        "This image has no built-in training flow: mount a strategy script and "
        "set CUSTOM_TRAIN.\n"
        "  → `openroboto init` writes one, `openroboto train -s <file>` mounts it\n"
        "  → it receives (cfg, episodes, policy); policy is the LingBot model, "
        "bf16, base frozen, LoRA injected\n"
        f"  (output_dir={cfg['output_dir']}, nothing was written)"
    )


def _load_episodes(path: str) -> list:
    """Load episode JSON data from the given path.

    Byte-identical to the π₀.₅ runner's loader on purpose: `episodes` is the
    second argument of red line #2's signature, and it has to mean the same
    thing in both images.

    Args:
        path: path to the JSON file

    Returns:
        list of episodes
    """
    with open(path, "r") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("episodes", data.get("data", [data]))
    return data


def _get_gpu_name() -> str:
    """Get GPU device name via torch, or return 'cpu' if unavailable.

    Returns:
        GPU device name string
    """
    try:
        import torch

        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except ImportError:
        pass
    return "cpu"


# ─── Main ─────────────────────────────────────────────────


def main():
    """Main entry point: init logging, read config, run training, print JSON result."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s,%(msecs)03d [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    cfg = get_config()

    logger.info("🦿 LingBot-VLA 2.0 Runner")
    logger.info("   Base model: %s@%s", BASE_MODEL_REPO, BASE_MODEL_REVISION[:12])
    logger.info("   Checkpoint: %s", cfg["checkpoint_path"])
    logger.info("   Train data: %s", cfg["train_data"])
    logger.info("   Output:     %s", cfg["output_dir"])
    logger.info(
        "   Epochs:     %s | BS: %s | LR: %s",
        cfg["epochs"],
        cfg["batch_size"],
        cfg["learning_rate"],
    )
    logger.info("   Device:     %s", _get_gpu_name())

    metrics, proof = run_training(cfg)

    # Print final result as JSON for host parsing
    print("---RESULT---")
    print(json.dumps({"metrics": metrics, "proof": proof}, indent=2))


if __name__ == "__main__":
    sys.exit(main())
