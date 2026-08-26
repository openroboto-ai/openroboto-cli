"""Which code path a competition takes.

The subnet runs more than one competition at a time, and they are not judged
alike: the simulation competition on π0.5 (openpi) and the one on LingBot-VLA
2.0 accept completely different checkpoint layouts. The adapter string in
`miner.yaml` -- written there by `openroboto init` from the competition the
miner picked -- says which one this workspace mines.

The **strings** are shared with the backend; the implementations behind them are
not (the backend decides admission and rounds, this decides which local tool
chain to run).

## 🔴 Two orthogonal dimensions, two tables (2026-08-26)

`adapter` used to answer both "which track" and "which base model". It never
could: `real_xarm6` names a *robot arm*, not a model, so the real track had
nowhere to say what it runs on and this table simply guessed `LINGBOT` for it --
a guess that was backwards, since the plan is to bring xArm 6 up on π0.5 first.

    ADAPTERS      keyed by `competition.adapter`             -> training
    FORMAT_PROFILES  keyed by `competition.base_model_family` -> format profile

Which rule book judges a checkpoint follows the **base model**; whether this
package ships a container to train it follows the **competition** (its dataset
and image exist or they do not). The suffixes in `sim_openpi` / `sim_lingbot`
are historical from here on -- **do not read a base model out of an adapter
name**. The backend column `competitions.base_model_family` is the answer, and
it travels into `miner.yaml` with the rest of the competition row.

🔴 **Values never live here.** Image names, template names, fees and addresses
are competition data and are read from `competition.params` -- writing one into
this table means a new release of the CLI to change a number. The columns below
answer only "which code path"; a column whose value is the same for every
adapter is not a dispatch, it is a constant with extra steps.

⚠️ **How the fee is paid is not one of the columns.** `burn` or `transfer`
comes from `params.fee.kind`, which is the season's own data. A column here
saying "the real track transfers" would be a second copy of that fact, and the
two would disagree on the first season that breaks the pattern -- while paying.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from openroboto.config import ConfigError

OPENPI: Final = "openpi"
LINGBOT: Final = "lingbot"

#: Training runs in the container this package builds.
DOCKER: Final = "docker"
#: There is nothing to run yet -- and `openroboto train` says so instead of
#: pretending. A no-op would leave an empty output directory behind, which the
#: next `openroboto check` would then judge, giving a verdict about nothing.
UNAVAILABLE: Final = "unavailable"


@dataclass(frozen=True)
class Adapter:
    """What one competition's *track* decides. One column, on purpose.

    `format_profile` left this class on 2026-08-26: it follows the base model,
    which now has its own column on the competition row and its own table below.
    A one-column dataclass rather than a plain `dict[str, str]` because the next
    per-competition code path has somewhere to go -- collapse it if that never
    happens.
    """

    #: Whether **this package** ships a container `openroboto train` can run for
    #: this competition. Not "is there an image on this machine": an image named
    #: by `params.training.image` can sit in `docker images` with anything at all
    #: inside it -- see `commands/build.py`, which is why this column also decides
    #: whether `openroboto build` has something honest to build.
    training: str = DOCKER


ADAPTERS: Final = {
    "sim_openpi": Adapter(),
    # `DOCKER` since 2026-08-26, on evidence rather than arithmetic. Three
    # reasons were written here over time for holding it at `UNAVAILABLE`; all
    # three are now discharged, and they are worth a line each so nobody
    # re-derives them.
    #
    # "This package ships no LingBot Dockerfile": **fixed.**
    # `runner/lingbot/` ships one, and `runner_context(LINGBOT)` selects it.
    #
    # "LingBot cannot fit `train(cfg, episodes, policy)`": **wrong.** That
    # conclusion came from reading the vendor's own entry point, which builds
    # the model inside each rank after `dist.init_process_group` and shards it
    # with FSDP2 -- nothing to hand across a process boundary, and a LeRobot
    # dataset *directory* where `episodes` is a list. But the vendor's entry
    # point is not the only way into their code: `build_foundation_model()`
    # runs in a single process (`get_parallel_state()` is written to work
    # uninitialised), sharding is a separate call nobody has to make, and their
    # unused `add_lora_to_model()` brings a 6.38 B model onto one card. The
    # data pipeline never comes up because this runner does not use theirs.
    #
    # "What is actually missing is a run": **it has been run.**
    # `scripts/verify_lingbot_runner.py` on an A100-SXM4-80GB, all stages
    # green -- the container builds, `build_foundation_model()` returns a model
    # whose every parameter was filled from the released checkpoint,
    # `LORA_TARGET_MODULES` matches 396 real modules for 38.9 M trainable
    # parameters, `moe_implementation="fused"` works unsharded, and
    # `merge_lora_and_export()` writes a flat checkpoint root. Seven of the
    # vendor's own defaults had to be overridden to get there; each one is
    # commented at its call site in `runner/lingbot/train_runner.py`.
    #
    # ⚠️ Measured peak was **12.4 GiB, weights only, before any batch**. The
    # 14-18 GiB written in that file is weights *plus* activations and remains
    # arithmetic -- the verification builds and exports, it does not run a
    # training step. A 24 GB card has 11.6 GiB of headroom for activations;
    # that is the number a miner reports back on, not one this run proved.
    "sim_lingbot": Adapter(),
    # The dataset (`xarm6-libero-seed-v1`) and the training image do not exist
    # yet, so there is nothing to install and nothing to run.
    #
    # ⚠️ This row used to carry `format_profile=LINGBOT`, which was a **guess** --
    # and the wrong one: xArm 6 is being brought up on π0.5 first. Nothing here
    # ever knew, because the base model is a property of the season and this
    # table is keyed by hardware. It is now read off the season row instead.
    "real_xarm6": Adapter(training=UNAVAILABLE),
}
"""Every adapter this client knows, and what each step does for it."""

DEFAULT_ADAPTER: Final = "sim_openpi"
"""A `miner.yaml` written before competitions existed has no adapter at all.
It is the π0.5 simulation competition -- the same rule the chain side uses when
a commitment carries no competition id, and the promise made in MIGRATION.md
§2: a config without the section keeps working exactly as it did."""


def resolve(adapter: str) -> Adapter:
    """The adapter string from `miner.yaml` → the code paths it selects.

    An adapter this client does not know is an **error**, never a fall back to
    the simulation default. Falling back is wrong twice over: it judges a
    LingBot checkpoint by the π0.5 rules, which reports "no model weights found"
    for a perfectly good upload; and it lets a miner believe they are entering
    the real-track competition while everything around them runs the simulation
    one -- a belief they pay to correct.
    """
    if not adapter:
        return ADAPTERS[DEFAULT_ADAPTER]
    try:
        return ADAPTERS[adapter]
    except KeyError:
        raise ConfigError(
            f"miner.yaml names competition adapter `{adapter}`, which this client "
            f"does not know (it knows: "
            f"{', '.join(sorted(ADAPTERS))}).\n"
            f"  → pip install -U openroboto\n"
            f"  → if it still says this after upgrading, this competition has no "
            f"CLI support released yet -- watch for the announcement\n"
            f"  Refusing to guess: the rules that judge your checkpoint come from "
            f"the competition, and applying the wrong ones tells you your upload "
            f"is broken when it is not -- or that it is fine when it is not."
        ) from None


#: `competitions.base_model_family` -> the local format profile.
#:
#: 🔴 **The keys are the backend's vocabulary, verbatim**, and they are also what the
#: evaluator's `detect_model_family()` returns -- do not spell them any other way.
#: The *values* are this package's own: a profile string is a **directory name**
#: (`runner/`, `runner/lingbot/`, see `openroboto.runner_context`), which is why
#: `LINGBOT` is `"lingbot"` and not `"lingbot_vla"`. Shared strings, separate
#: implementations, same as `ADAPTERS`.
FORMAT_PROFILES: Final = {
    "openpi": OPENPI,
    "lingbot_vla": LINGBOT,
}
"""Every base model this client can judge a checkpoint against."""

#: The two adapters whose name provably names their base model. Used **only** for a
#: `miner.yaml` written before `base_model_family` existed -- the same backwards
#: compatibility as `DEFAULT_ADAPTER`, and bounded the same way: these two mappings
#: can be proven from the seeded competition rows, and there is no third.
#:
#: 🔴 `real_xarm6` is deliberately absent. Its name says *hardware*; guessing a base
#: model from it is what this whole split removed, and the previous guess here was
#: wrong. A real-track workspace with no `base_model_family` gets a refusal.
_FAMILY_BY_LEGACY_ADAPTER: Final = {
    "sim_openpi": "openpi",
    "sim_lingbot": "lingbot_vla",
}


def base_model_family(adapter: str, family: str = "") -> str:
    """Which base model this workspace mines, from the season snapshot.

    `family` is `competition.base_model_family` out of `miner.yaml`. It is empty
    for a config written before that key existed (or by a CLI pinned to an older
    protocol package, which drops the field on the way in), and only then does
    `adapter` get consulted -- for the two names that provably carry their base
    model. An empty adapter is the pre-competition π0.5 workspace,
    `DEFAULT_ADAPTER`.

    An unknown or unresolvable base model is an **error**, never a fall back to
    π0.5. The rules that judge a checkpoint come from the competition, and
    applying the wrong ones tells a miner their upload is broken when it is not
    -- or that it is fine when it is not, which they find out after paying.
    """
    resolved = family or _FAMILY_BY_LEGACY_ADAPTER.get(adapter or DEFAULT_ADAPTER, "")
    if resolved in FORMAT_PROFILES:
        return resolved
    raise ConfigError(
        f"miner.yaml does not say which base model competition "
        f"`{adapter or DEFAULT_ADAPTER}` runs on"
        + (
            f" (it says `{resolved}`, which this client does not know)"
            if resolved
            else ""
        )
        + ".\n"
        f"  → openroboto init --refresh   (re-reads the competition from the backend)\n"
        f"  → pip install -U openroboto   (if it still says this afterwards)\n"
        f"  Refusing to guess: the base model decides which rules judge your "
        f"checkpoint, and the adapter name does not say -- `real_xarm6` names a "
        f"robot arm. Guessing costs you the entry fee, not a retry.\n"
        f"  This client knows: {', '.join(sorted(FORMAT_PROFILES))}."
    )


def format_profile(adapter: str, family: str = "") -> str:
    """Which set of layout rules this competition's checkpoints are judged by."""
    return FORMAT_PROFILES[base_model_family(adapter, family)]
