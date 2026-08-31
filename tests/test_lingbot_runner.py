"""The LingBot training container, as far as a machine with no GPU can go.

What is checkable here is small and specific: that the build context is
complete, that its pins agree with each other, and that the two runners hand a
strategy script the same `cfg`. Everything that needs a card --
`build_foundation_model()`, the VRAM figure, LoRA target modules matching real
modules -- lives in `scripts/verify_lingbot_runner.py`, which ran green on an
A100 on 2026-08-26 and is what moved `adapters.sim_lingbot.training` to
`DOCKER`.

The one thing these tests are *not* is reassurance. Green here means the
container is well-formed, not that it trains -- and re-running them proves
nothing about a checkpoint or a driver, so they cannot notice if either moves
out from under the verified run.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

from openroboto import adapters, runner_context

LINGBOT_CONTEXT = runner_context(adapters.LINGBOT)
RUNNER_CONTEXT = runner_context(adapters.OPENPI)
OPENPI_CONTEXT = runner_context(adapters.OPENPI)


def _load(path: Path, name: str) -> ModuleType:
    """Import a container-side script by path.

    They are not importable as `openroboto.runner.*` -- no `__init__.py`, and
    they are not meant to be imported by this package at all. Loading by path
    is also what proves the top of the file is stdlib-only: a stray
    `import torch` at module level fails right here.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def lingbot() -> ModuleType:
    return _load(LINGBOT_CONTEXT / "train_runner.py", "lingbot_train_runner")


@pytest.fixture(scope="module")
def openpi() -> ModuleType:
    return _load(OPENPI_CONTEXT / "train_runner.py", "openpi_train_runner")


# ─── The context is a context ─────────────────────────────


@pytest.mark.parametrize("profile", sorted(set(adapters.FORMAT_PROFILES.values())))
def test_every_format_profile_has_a_complete_build_context(profile: str) -> None:
    """Every file the Dockerfile COPYs has to be in the wheel.

    The generalisation of the openpi-only check in `test_commands.py`: a
    missing file shows up as `docker build` failing on a miner's machine while
    everything here stays green, and completeness of a build context is not
    something code review can guarantee.
    """
    context = runner_context(profile)
    dockerfile = context / "Dockerfile"
    assert dockerfile.is_file(), f"no Dockerfile for profile `{profile}` at {context}"

    copied = [
        line.split()[1]
        for line in dockerfile.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("COPY ")
    ]
    assert copied, "a build context whose Dockerfile COPYs nothing is pointless"
    for name in copied:
        assert (context / name).is_file(), (
            f"{profile}: Dockerfile COPYs {name}, which is not in the package"
        )


def test_the_lingbot_context_installs_lingbot_and_the_openpi_one_installs_openpi() -> (
    None
):
    """The whole point of selecting a context by profile.

    If these two ever resolved to the same directory -- or if one of them grew
    the other's install line -- `lingbot-runner:1.2` would be built with π0.5
    inside it. That is the failure `commands/build.py` exists to prevent, and
    the one nothing downstream can see: `docker images` lists it, `doctor`
    calls it ready, `train` runs it.

    Comments are stripped first. Both Dockerfiles talk about each other on
    purpose (they share a container interface, and each explains why it differs
    from the other), so a substring search over the whole file would be a test
    of the prose.
    """
    assert LINGBOT_CONTEXT != OPENPI_CONTEXT

    def directives(context: Path) -> str:
        text = (context / "Dockerfile").read_text(encoding="utf-8")
        return "\n".join(
            line
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ).lower()

    assert "openpi" in directives(OPENPI_CONTEXT)
    assert "openpi" not in directives(LINGBOT_CONTEXT)
    assert "lingbot" in directives(LINGBOT_CONTEXT)


# ─── Red line #2: one `cfg`, two images ───────────────────


def test_both_runners_read_the_same_environment_into_the_same_cfg(
    lingbot: ModuleType, openpi: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 `build_docker_command()` sets one fixed list of environment variables
    for every competition (red line #2), and a strategy script reads `cfg`.

    A key present in one runner and absent in the other is a script that works
    in one image and raises `KeyError` in the other -- after the container has
    started, hours into a round. The values may differ (they name different
    base models); the keys may not.
    """
    for name in (
        "CHECKPOINT_PATH",
        "TRAIN_DATA",
        "VAL_DATA",
        "OUTPUT_DIR",
        "CUSTOM_TRAIN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EPOCHS", "7")
    monkeypatch.setenv("LORA_R", "16")

    lingbot_cfg, openpi_cfg = lingbot.get_config(), openpi.get_config()
    assert set(lingbot_cfg) == set(openpi_cfg)
    # And they read the same variables, not just declare the same keys.
    assert lingbot_cfg["epochs"] == openpi_cfg["epochs"] == 7
    assert lingbot_cfg["lora_r"] == openpi_cfg["lora_r"] == 16


def test_episodes_mean_the_same_thing_in_both_images(
    lingbot: ModuleType, openpi: ModuleType, tmp_path: Path
) -> None:
    """`episodes` is the second argument of `train(cfg, episodes, policy)`.

    LingBot's own trainer reads a LeRobot dataset directory; this runner
    deliberately does not use their data pipeline, precisely so that the
    argument keeps meaning what it has always meant.
    """
    payload = '{"episodes": [{"prompt": "pick"}, {"prompt": "place"}]}'
    path = tmp_path / "train.json"
    path.write_text(payload, encoding="utf-8")
    assert lingbot._load_episodes(str(path)) == openpi._load_episodes(str(path))


def test_the_default_flow_refuses_instead_of_inventing_a_recipe(
    lingbot: ModuleType, tmp_path: Path
) -> None:
    """No strategy script means no training, said out loud.

    The alternative -- a fake loss curve, as the π0.5 image does for historical
    reasons -- writes a `metrics.json` that makes `openroboto train` look like
    it succeeded, and the empty output directory is then judged by
    `openroboto check`.
    """
    with pytest.raises(RuntimeError, match="CUSTOM_TRAIN"):
        lingbot._run_default({"output_dir": str(tmp_path)})
    assert list(tmp_path.iterdir()) == []


# ─── Pins that have to agree with each other ──────────────


def test_cpu_is_refused_as_an_init_device(lingbot: ModuleType) -> None:
    """🔴 The empty-init trap, guarded.

    Without a process group `get_parallel_state().global_rank` is -1, and
    `build_foundation_model()` reads `init_device == "cpu" and global_rank != 0`
    as "skip loading the weights". `"cpu"` is the obvious value to reach for on
    a machine with no card, every signature on the path accepts it, and what it
    produces is 6.38 B randomly initialised parameters that train and export
    without one complaint.

    This runs on the CPU here because the guard is the first statement in the
    function -- nothing is imported or downloaded before it raises.
    """
    with pytest.raises(ValueError, match=r"empty-init|empty_init"):
        lingbot.build_policy({"lora_r": 8, "lora_alpha": 16}, init_device="cpu")


def test_the_pinned_base_model_is_a_commit_not_a_branch(lingbot: ModuleType) -> None:
    """A floating revision means the miner trains on one base model and is
    judged against another; the protocol package fingerprints this exact one."""
    assert re.fullmatch(r"[0-9a-f]{40}", lingbot.BASE_MODEL_REVISION)
    assert lingbot.BASE_MODEL_REPO == "robbyant/lingbot-vla-v2-6b"


def test_the_dockerfile_pins_lingbots_code_to_a_commit() -> None:
    """Same argument one level up: `train_runner.py` calls
    `build_foundation_model()` and `add_lora_to_model()` by keyword, so a
    branch that renames an argument turns a pinned CLI release into a container
    that cannot build a model."""
    dockerfile = (LINGBOT_CONTEXT / "Dockerfile").read_text(encoding="utf-8")
    # ⚠️ The arg was called `LINGBOT_REF` until 2026-08-31; both runner contexts
    #    now use `CODE_REF` so that `openroboto build` can pass a season's pin
    #    without knowing which base model it is (an unmatched build arg is
    #    silently ignored by docker, which would leave the image on its default).
    ref = re.search(r"ARG CODE_REF=(\S+)", dockerfile)
    assert ref, "the Dockerfile does not pin CODE_REF at all"
    assert re.fullmatch(r"[0-9a-f]{40}", ref.group(1)), (
        f"CODE_REF={ref.group(1)!r} is not a commit -- a branch would mean the "
        f"image drifts under a pinned CLI release"
    )


def test_the_flash_attn_wheel_matches_the_torch_and_python_pins() -> None:
    """🔴 One URL pins cu12 × torch2.8 × cxx11abi × cp311.

    Bumping torch or the interpreter without rewriting that URL is a silent
    mismatch: `--no-deps` installs it happily and the ABI blows up at import,
    inside the container, on the miner's machine. The four dimensions live in
    two places in one file, so compare them here.
    """
    dockerfile = (LINGBOT_CONTEXT / "Dockerfile").read_text(encoding="utf-8")
    wheel = re.search(r"flash_attn-[\w.+]+-(cp\d+)-cp\d+-linux_x86_64\.whl", dockerfile)
    assert wheel, "no prebuilt flash-attn wheel in the Dockerfile"

    torch_pin = re.search(r"torch==(\d+\.\d+)", dockerfile)
    assert torch_pin, "torch is not pinned"
    assert f"torch{torch_pin.group(1)}" in wheel.group(0), (
        f"the wheel is built for a different torch than the pinned "
        f"{torch_pin.group(1)}: {wheel.group(0)}"
    )

    # Both spellings mean the same request. `--python python3.11` asks uv to find
    # an interpreter *named* that on PATH; `--python 3.11` asks for the version
    # and, with `--python-preference only-managed`, makes uv download it. The
    # Dockerfile moved to the second one because apt's python3.11 on ubuntu 22.04
    # is 3.11.0~rc1 and segfaults Triton -- so this regex has to accept it, or the
    # check that guards the ABI goes quiet exactly when the ABI moved.
    python_pin = re.search(r"uv venv [^\n]*--python (?:python)?(\d)\.(\d+)", dockerfile)
    assert python_pin, "the venv interpreter is not pinned"
    assert wheel.group(1) == f"cp{python_pin.group(1)}{python_pin.group(2)}", (
        f"the wheel is built for {wheel.group(1)}, the venv is "
        f"python{python_pin.group(1)}.{python_pin.group(2)}"
    )


def test_both_runner_contexts_use_the_same_build_arg_names() -> None:
    """🔴 `openroboto build` passes `CODE_REPO` / `CODE_REF` **without knowing
    which base model the season uses**, so both Dockerfiles have to answer to
    the same two names.

    Docker does not complain about a build arg that matches nothing: it ignores
    it. So a mismatch here is not a build failure -- the image comes out on its
    built-in default, `docker images` lists it, `train` runs it, and the miner
    trains against a source the season did not name. Nothing anywhere says so.
    """
    for context in (RUNNER_CONTEXT, LINGBOT_CONTEXT):
        dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
        assert "ARG CODE_REPO=" in dockerfile, f"{context.name}: no CODE_REPO"
        assert "ARG CODE_REF=" in dockerfile, f"{context.name}: no CODE_REF"


# ─── Where the addresses come from ────────────────────────


def test_no_environment_means_the_pins_this_image_was_built_around(
    lingbot: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compatibility promise, and the reason this is a fallback rather than
    a required variable.

    Every workspace written before `params.training.base_weights` existed sets
    nothing, and for those "nothing" has to keep meaning what it always meant --
    the base this image was built around. A required variable would turn all of
    them into a crash on a machine that was training fine yesterday.
    """
    monkeypatch.delenv("BASE_WEIGHTS", raising=False)
    assert lingbot._addressed("BASE_WEIGHTS", "org/model", "abc123") == (
        "org/model",
        "abc123",
    )


def test_the_seasons_address_replaces_both_halves(
    lingbot: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`repo@revision` is one string on purpose: it cannot half-apply."""
    monkeypatch.setenv("BASE_WEIGHTS", "other/model@deadbeef")
    assert lingbot._addressed("BASE_WEIGHTS", "org/model", "abc123") == (
        "other/model",
        "deadbeef",
    )


def test_a_season_naming_a_repo_with_no_revision_does_not_keep_the_old_commit(
    lingbot: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The built-in revision belongs to the built-in repository.

    Carrying it over to a repository the season just named is the exact failure
    the single-string form exists to prevent: "right repository, another
    version's commit" resolves, downloads, trains, and is judged against
    something else. Unpinned is visibly unpinned; a wrong pin is not.
    """
    monkeypatch.setenv("BASE_WEIGHTS", "other/model")
    assert lingbot._addressed("BASE_WEIGHTS", "org/model", "abc123") == (
        "other/model",
        "",
    )


def test_whitespace_is_not_an_address(
    lingbot: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`docker run -e BASE_WEIGHTS=` and a stray space arrive as the same
    intention -- the season named nothing. Without the strip, the second one
    resolves to a repository called `""`."""
    monkeypatch.setenv("BASE_WEIGHTS", "   ")
    assert lingbot._addressed("BASE_WEIGHTS", "org/model", "abc123") == (
        "org/model",
        "abc123",
    )
