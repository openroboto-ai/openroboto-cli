"""Which code path a competition takes.

The subnet runs more than one competition at a time, and they are not judged
alike: the simulation competition on π0.5 (openpi) and the one on LingBot-VLA
2.0 accept completely different checkpoint layouts. The adapter string in
`miner.yaml` -- written there by `openroboto init` from the competition the
miner picked -- says which one this workspace mines.

The **string** is shared with the backend; the implementation behind it is not
(the backend decides admission and rounds, this decides which local tool chain
to run).

🔴 **Values never live here.** Image names, template names, fees and addresses
are competition data and are read from `competition.params` -- writing one into
this table means a new release of the CLI to change a number. The columns below
answer only "which code path", and there are two of them because two commands
have more than one path to take; a column whose value is the same for every
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
    """Which code path each step takes for one competition."""

    #: Which rule book judges a checkpoint (`openroboto check`).
    #:
    #: ⚠️ Not a protocol-package argument: 0.7.0 has no `profile=` parameter,
    #: it has two parallel functions (`check_checkpoint_layout` /
    #: `check_lingbot_layout`). This only chooses between them.
    format_profile: str
    #: Whether `openroboto train` has a container to run for this competition.
    training: str = DOCKER


ADAPTERS: Final = {
    "sim_openpi": Adapter(format_profile=OPENPI),
    "sim_lingbot": Adapter(format_profile=LINGBOT),
    # The dataset (`xarm6-libero-seed-v1`) and the training image do not exist
    # yet, so there is nothing to install and nothing to run.
    "real_xarm6": Adapter(format_profile=LINGBOT, training=UNAVAILABLE),
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


def format_profile(adapter: str) -> str:
    """Which set of layout rules this competition's checkpoints are judged by."""
    return resolve(adapter).format_profile
