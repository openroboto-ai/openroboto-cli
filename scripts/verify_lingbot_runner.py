#!/usr/bin/env python3
"""Check the LingBot runner against a real LingBot install — ten minutes, one card.

Everything in `src/openroboto/runner/lingbot/` was written on a machine with no
NVIDIA GPU. `docker build` never ran, `build_foundation_model()` was never
called, and the VRAM arithmetic that says a 6.38 B model post-trains on one
24 GB card is arithmetic. This script is the other half: run it on a box that
has a card and it turns each of those assumptions into a PASS or a FAIL.

🔴 **Until it passes, `adapters.sim_lingbot.training` stays `UNAVAILABLE`.**

Run it inside the image (that is where LingBot's code lives):

    openroboto build --context <the lingbot context>   # or: docker build -t lingbot-runner:latest ...
    docker run --rm --gpus all \
      -v $PWD/scripts:/data/scripts \
      -v ~/.cache/huggingface:/data/cache \
      --entrypoint python lingbot-runner:latest \
      /data/scripts/verify_lingbot_runner.py --weights-root /data/cache

Stages 4-6 need the base weights (~25.5 GB + ~8.9 GB). Point `--weights-root`
at a directory holding `lingbot-vla-v2-6b/` and `Qwen3-VL-4B-Instruct/`, or
leave it off and let them download into HF_HOME — slow, but it is the same
path a miner's first run takes, so it is worth watching once.

Exit code is 0 only when nothing FAILed. Stages that cannot run here SKIP, and
a skip is not a pass: the summary prints both.
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import os
import sys
import traceback
from pathlib import Path

RUNNER_CANDIDATES = (
    # Inside the image, where the Dockerfile COPYs it.
    Path("/app/train_runner.py"),
    # A checkout, run from anywhere.
    Path(__file__).resolve().parent.parent
    / "src/openroboto/runner/lingbot/train_runner.py",
)

#: What `build_policy()` passes by keyword. A rename upstream turns into a
#: TypeError hours into a run; here it is one line of output.
EXPECTED_SIGNATURES = {
    "lingbotvla.models:build_foundation_model": (
        "config_path",
        "config_cls",
        "weights_path",
        "torch_dtype",
        "init_device",
        "config_kwargs",
        "moe_implementation",
    ),
    "lingbotvla.models:build_processor": ("processor_path",),
    "lingbotvla.utils.lora_utils:add_lora_to_model": (
        "model",
        "lora_rank",
        "lora_alpha",
        "lora_target_modules",
    ),
    "lingbotvla.utils.lora_utils:freeze_parameters": ("model",),
    "lingbotvla.utils.arguments:ModelArguments": (
        "config_key",
        "config_path",
        "model_path",
        "tokenizer_path",
        "post_training",
        "adanorm_time",
        "moe_implementation",
    ),
}

results: list[tuple[str, str, str]] = []


def record(stage: str, status: str, detail: str = "") -> None:
    icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️ "}[status]
    print(f"{icon} {status:4}  {stage}" + (f"\n          {detail}" if detail else ""))
    results.append((stage, status, detail))


def stage(name: str):
    """Run a check, turn any exception into a FAIL instead of a traceback exit."""

    def wrap(fn):
        def run(*args, **kwargs):
            try:
                detail = fn(*args, **kwargs)
            except SkipStage as exc:
                record(name, "SKIP", str(exc))
                return None
            except Exception as exc:
                record(name, "FAIL", f"{type(exc).__name__}: {exc}")
                if os.getenv("VERIFY_TRACEBACK"):
                    traceback.print_exc()
                return None
            record(name, "PASS", detail or "")
            return detail

        return run

    return wrap


class SkipStage(Exception):
    """This machine cannot answer the question — not the same as answering no."""


def load_runner(explicit: str = ""):
    candidates = [Path(explicit)] if explicit else list(RUNNER_CANDIDATES)
    for path in candidates:
        if path.is_file():
            spec = importlib.util.spec_from_file_location("lingbot_train_runner", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            print(f"   runner: {path}")
            return module
    raise SystemExit(
        f"train_runner.py not found, looked in: {[str(p) for p in candidates]}"
    )


# ─── 1. What is installed ─────────────────────────────────


@stage("1. environment: torch + CUDA + flash-attn + peft")
def check_environment() -> str:
    import torch

    parts = [f"python {sys.version.split()[0]}", f"torch {torch.__version__}"]
    parts.append(f"cuda {torch.version.cuda}")
    if torch.cuda.is_available():
        name = torch.cuda.get_device_name(0)
        total = torch.cuda.get_device_properties(0).total_memory / 2**30
        parts.append(f"{torch.cuda.device_count()}× {name} ({total:.0f} GiB)")
    else:
        parts.append("NO CUDA DEVICE (stages 5-6 will skip)")

    import peft
    import transformers

    parts += [f"transformers {transformers.__version__}", f"peft {peft.__version__}"]
    try:
        import flash_attn

        parts.append(f"flash_attn {flash_attn.__version__}")
    except Exception as exc:
        # Not a hard fail: `build_foundation_model` takes attn_implementation.
        # But the default is flash_attention_2, so a broken wheel here is a
        # crash later, and this line is where it should be read.
        parts.append(f"⚠️  flash_attn UNUSABLE: {type(exc).__name__}: {exc}")
    return " | ".join(parts)


# ─── 2. Did the vendor's signatures move? ─────────────────


@stage("2. vendor signatures still accept what build_policy() passes")
def check_signatures() -> str:
    missing = []
    for target, expected in EXPECTED_SIGNATURES.items():
        module_name, attr = target.split(":")
        module = __import__(module_name, fromlist=[attr])
        params = inspect.signature(getattr(module, attr)).parameters
        for name in expected:
            if name not in params:
                missing.append(f"{target}({name})")
    if missing:
        raise AssertionError(
            "arguments gone from upstream: "
            + ", ".join(missing)
            + " -- the Dockerfile's LINGBOT_REF and train_runner.py disagree"
        )
    return f"{len(EXPECTED_SIGNATURES)} entry points, all arguments present"


# ─── 3. The single-process assumption ─────────────────────


@stage("3. single process: get_parallel_state() works with no process group")
def check_parallel_state() -> str:
    import torch.distributed as dist
    from lingbotvla.distributed.parallel_state import get_parallel_state

    if dist.is_initialized():
        raise SkipStage(
            "a process group is already initialised; run this bare, not under torchrun"
        )

    state = get_parallel_state()
    rank, world = state.global_rank, state.world_size
    if world != 1:
        raise AssertionError(
            f"world_size is {world}, expected 1 without a process group"
        )
    if rank == 0:
        # Not a failure of ours -- but decision 1 in train_runner.py rests on
        # rank being -1 here, and if upstream changed it to 0 then
        # init_device='cpu' silently became safe and the guard is now wrong.
        return "⚠️  global_rank is 0, not -1: the empty_init trap may be gone, re-read build_foundation_model()"
    if rank != -1:
        raise AssertionError(f"global_rank is {rank}, expected -1")
    return "global_rank=-1 world_size=1 -- builds run, and init_device='cpu' would empty-init (guarded)"


# ─── 4-6. Actually build it ───────────────────────────────


def _cfg(runner, weights_root: str, lora_r: int, lora_alpha: int) -> dict:
    cfg = runner.get_config()
    cfg.update({"lora_r": lora_r, "lora_alpha": lora_alpha})
    if weights_root:
        # `checkpoint_path`'s **parent** is what resolve_weights() searches, so
        # point at a non-existent child of the root the caller gave us.
        cfg["checkpoint_path"] = str(Path(weights_root) / "lingbot-vla-v2-6b")
    return cfg


@stage("4. meta-device build: config registry, architecture, LoRA targets")
def check_meta_build(runner, cfg: dict) -> str:
    policy = runner.build_policy(cfg, init_device="meta")
    total = sum(p.numel() for p in policy.parameters())
    trainable = sum(p.numel() for p in policy.parameters() if p.requires_grad)
    if trainable == 0:
        raise AssertionError("no trainable parameters after LoRA injection")
    return (
        f"{total / 1e9:.2f} B parameters, {trainable / 1e6:.1f} M trainable "
        f"({100 * trainable / total:.3f}%) -- no weights read, structure only"
    )


#: Stage 5 builds the model and stage 6 exports it; a module-level handoff
#: keeps the stage decorator's return value a plain detail string.
BUILT_POLICY = None


@stage("5. cuda bf16 build: does it fit, and in how much")
def check_cuda_build(runner, cfg: dict) -> str:
    global BUILT_POLICY
    import torch

    if not torch.cuda.is_available():
        raise SkipStage("no CUDA device -- this is the assumption that matters most")

    torch.cuda.reset_peak_memory_stats()
    policy = runner.build_policy(cfg, init_device="cuda")
    BUILT_POLICY = policy

    dtypes = {str(p.dtype) for p in policy.parameters() if not p.requires_grad}
    if dtypes != {"torch.bfloat16"}:
        raise AssertionError(f"frozen base is not pure bf16: {sorted(dtypes)}")
    trainable = [p for p in policy.parameters() if p.requires_grad]

    peak = torch.cuda.max_memory_allocated() / 2**30
    total_vram = torch.cuda.get_device_properties(0).total_memory / 2**30
    record(
        "5b. VRAM headroom (weights only, before any batch)",
        "PASS" if peak < total_vram * 0.75 else "FAIL",
        f"peak {peak:.1f} GiB of {total_vram:.0f} GiB. Activations are on top of "
        f"this -- the estimate written into train_runner.py is 14-18 GiB total.",
    )
    return (
        f"peak {peak:.1f} GiB | base bf16 frozen | "
        f"{sum(p.numel() for p in trainable) / 1e6:.1f} M trainable in "
        f"{sorted({str(p.dtype) for p in trainable})}"
    )


@stage("6. merge + export produces a flat checkpoint root")
def check_export(runner, policy, output_dir: str) -> str:
    if policy is None:
        raise SkipStage("stage 5 did not produce a model")
    import tempfile

    target = output_dir or tempfile.mkdtemp(prefix="lingbot-export-")
    runner.merge_lora_and_export(policy, target)

    top = sorted(p.name for p in Path(target).iterdir())
    weights = [n for n in top if n.endswith((".safetensors", ".bin"))]
    if not weights:
        raise AssertionError(f"no weight files at the top of {target}: {top}")
    if "adapter_model.safetensors" in top or "adapter_config.json" in top:
        raise AssertionError(
            "exported a bare LoRA adapter -- `openroboto check` rejects this as "
            "BARE_LORA_ADAPTER and nothing downstream merges it"
        )
    return f"{len(weights)} weight file(s) at the root of {target}: {top[:6]}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runner", default="", help="path to the lingbot train_runner.py"
    )
    parser.add_argument(
        "--weights-root",
        default="",
        help="directory holding lingbot-vla-v2-6b/ and Qwen3-VL-4B-Instruct/; "
        "without it stages 4-6 download tens of GB",
    )
    parser.add_argument(
        "--output-dir", default="", help="where stage 6 writes; default is a temp dir"
    )
    parser.add_argument("--lora-r", type=int, default=32)
    parser.add_argument("--lora-alpha", type=int, default=64)
    parser.add_argument(
        "--quick", action="store_true", help="stages 1-3 only (no weights, no GPU)"
    )
    args = parser.parse_args()

    print("LingBot runner verification\n" + "=" * 60)
    runner = load_runner(args.runner)
    print()

    check_environment()
    check_signatures()
    check_parallel_state()

    if args.quick:
        record("4-6. build / VRAM / export", "SKIP", "--quick")
    else:
        cfg = _cfg(runner, args.weights_root, args.lora_r, args.lora_alpha)
        check_meta_build(runner, cfg)
        check_cuda_build(runner, cfg)
        check_export(runner, BUILT_POLICY, args.output_dir)

    failed = [name for name, status, _ in results if status == "FAIL"]
    skipped = [name for name, status, _ in results if status == "SKIP"]
    print("\n" + "=" * 60)
    print(
        f"{len(results) - len(failed) - len(skipped)} passed, {len(failed)} failed, {len(skipped)} skipped"
    )
    if skipped:
        print("⏭️  a skip is not a pass: " + "; ".join(skipped))
    if failed:
        print("❌ " + "; ".join(failed))
        print("\n🔴 sim_lingbot.training must stay UNAVAILABLE.")
        return 1
    if skipped:
        print(
            "\n🟡 Nothing failed, but not everything ran. Flipping "
            "sim_lingbot.training to DOCKER needs stages 4-6 green on a card."
        )
        return 0
    print(
        "\n✅ All stages green -- this is the evidence "
        "`adapters.sim_lingbot.training = DOCKER` was waiting for."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
