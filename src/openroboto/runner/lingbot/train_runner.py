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


🔴 Not verified on a GPU
========================
Written on a machine with no NVIDIA card, so `docker build` never ran and
neither did any of this. The signatures below were read from
`Robbyant/lingbot-vla-v2` at the commit the Dockerfile pins; the reasoning is
as good as the reading. Specifically unproven:

  1. that 14–18 GB is the real number, on a real card, with real batches
  2. that `build_foundation_model()` completes with no process group — the
     reasoning above says yes, only a run says so
  3. that `LORA_TARGET_MODULES` matches modules that exist in this
     architecture (peft raises when it matches nothing, so this fails loudly)
  4. that `moe_implementation="fused"` works outside their sharded setup
  5. that torch 2.8.0's cu128 wheel runs on the CUDA 12.4 runtime base image
  6. that `merge_lora_and_export()` produces a checkpoint the evaluator loads

`scripts/verify_lingbot_runner.py` walks 2–4 and 6 in about ten minutes.
Until it has been run, `adapters.sim_lingbot.training` stays `UNAVAILABLE` and
`openroboto train` refuses this competition.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

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

LORA_TARGET_MODULES = "q,k,v,o,ffn.0,ffn.2"
"""`add_lora_to_model`'s own default, repeated here so it is visible and one
edit away.

⚠️ Unverified against this architecture. peft matches these as name suffixes;
when nothing matches it raises rather than silently training zero parameters,
and `build_policy()` checks the trainable count on top of that.
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

def resolve_weights(repo_id: str, revision: str, checkpoint_path: str, local_name: str) -> str:
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
    layer and is re-fetched next round.
    """
    if os.path.isdir(checkpoint_path) and local_name == os.path.basename(checkpoint_path.rstrip("/")):
        return checkpoint_path

    sibling = os.path.join(os.path.dirname(checkpoint_path.rstrip("/")), local_name)
    if os.path.isdir(sibling):
        logger.info("📦 %s: using the mounted copy at %s", repo_id, sibling)
        return sibling

    from huggingface_hub import snapshot_download

    logger.info("⬇️  %s@%s: not mounted, downloading (this is tens of GB)", repo_id, revision[:12] if revision else "main")
    return snapshot_download(repo_id=repo_id, revision=revision or None)


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

    import torch
    from lingbotvla.models import build_foundation_model, build_processor
    from lingbotvla.models.config_registry import get_config_registry
    from lingbotvla.utils.arguments import ModelArguments, TrainingArguments
    from lingbotvla.utils.lora_utils import add_lora_to_model, freeze_parameters

    base_weights = resolve_weights(
        BASE_MODEL_REPO, BASE_MODEL_REVISION, cfg["checkpoint_path"], "lingbot-vla-v2-6b"
    )
    processor_path = resolve_weights(
        PROCESSOR_REPO, "", cfg["checkpoint_path"], "Qwen3-VL-4B-Instruct"
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
        # Both `true` in the vendor's robotwin.yaml and both `False` by
        # dataclass default. They change the architecture, so the defaults would
        # build something the v2 weights do not fit.
        post_training=True,
        adanorm_time=True,
        moe_implementation=MOE_IMPLEMENTATION,
    )
    train_args = TrainingArguments()
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
    config = config_cls(**config_kwargs)

    logger.info("🧩 Building %s from %s (%s, %s)", CONFIG_KEY, base_weights, "bfloat16", init_device)
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

    freeze_parameters(model)
    add_lora_to_model(
        model,
        lora_rank=cfg["lora_r"],
        lora_alpha=cfg["lora_alpha"],
        lora_target_modules=LORA_TARGET_MODULES,
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
        cfg["lora_r"], cfg["lora_alpha"],
        sum(p.numel() for p in trainable) / 1e6, total / 1e9,
        100.0 * sum(p.numel() for p in trainable) / max(total, 1),
    )

    # A plain attribute on an nn.Module: not a submodule, not a parameter, so
    # `state_dict()` and `save_pretrained()` do not see it. A strategy script
    # cannot build a batch without it, and making it a second positional
    # argument would break red line #2's signature.
    model.processor = build_processor(processor_path)
    if torch.cuda.is_available():
        logger.info("🖥️  %s | %.1f GiB allocated", torch.cuda.get_device_name(0), torch.cuda.memory_allocated() / 2**30)
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
        if hasattr(module, "merge") and callable(module.merge) and hasattr(module, "lora_A"):
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
        raise ValueError(f"{custom_script} Missing train(cfg, episodes, policy) entry function")

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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s,%(msecs)03d [%(name)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    cfg = get_config()

    logger.info("🦿 LingBot-VLA 2.0 Runner")
    logger.info("   Base model: %s@%s", BASE_MODEL_REPO, BASE_MODEL_REVISION[:12])
    logger.info("   Checkpoint: %s", cfg["checkpoint_path"])
    logger.info("   Train data: %s", cfg["train_data"])
    logger.info("   Output:     %s", cfg["output_dir"])
    logger.info("   Epochs:     %s | BS: %s | LR: %s", cfg["epochs"], cfg["batch_size"], cfg["learning_rate"])
    logger.info("   Device:     %s", _get_gpu_name())

    metrics, proof = run_training(cfg)

    # Print final result as JSON for host parsing
    print("---RESULT---")
    print(json.dumps({"metrics": metrics, "proof": proof}, indent=2))


if __name__ == "__main__":
    sys.exit(main())
