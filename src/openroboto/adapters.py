"""Which code path a competition takes.

The subnet runs more than one competition at a time, and they are not judged
alike: the simulation competition on π0.5 (openpi) and the one on LingBot-VLA
2.0 accept completely different checkpoint layouts. The adapter string in
`miner.yaml` -- written there by `openroboto init` from the competition the
miner picked -- says which one this workspace mines.

The **string** is shared with the backend; the implementation behind it is not
(the backend decides admission and rounds, this decides which local tool chain
to run). Only the format dispatch lives here today, because `openroboto check`
is the only command that has somewhere to dispatch to yet; `init` / `build` /
`train` / `submit` add their own columns when their code paths exist.

🔴 **Values never live here.** Image names, template names, fees and addresses
are competition data and are read from `competition.params` -- writing one into
this table means a new release of the CLI to change a number.
"""

from __future__ import annotations

from typing import Final

from openroboto.config import ConfigError

OPENPI: Final = "openpi"
LINGBOT: Final = "lingbot"

FORMAT_PROFILES: Final = {
    "sim_openpi": OPENPI,
    "sim_lingbot": LINGBOT,
    "real_xarm6": LINGBOT,
}
"""Adapter → which rule book judges a checkpoint."""

DEFAULT_ADAPTER: Final = "sim_openpi"
"""A `miner.yaml` written before competitions existed has no adapter at all.
It is the π0.5 simulation competition -- the same rule the chain side uses when
a commitment carries no competition id, and the promise made in MIGRATION.md
§2: a config without the section keeps working exactly as it did."""


def format_profile(adapter: str) -> str:
    """Which set of layout rules this competition's checkpoints are judged by.

    An adapter this client does not know is an **error**, never a fall back to
    the simulation default: falling back means judging a LingBot checkpoint by
    the π0.5 rules, which reports "no model weights found" for a perfectly good
    upload -- a verdict from the wrong rule book, delivered right before the
    miner decides whether to burn.
    """
    if not adapter:
        return FORMAT_PROFILES[DEFAULT_ADAPTER]
    try:
        return FORMAT_PROFILES[adapter]
    except KeyError:
        raise ConfigError(
            f"miner.yaml names competition adapter `{adapter}`, which this client "
            f"does not know (it knows: "
            f"{', '.join(sorted(FORMAT_PROFILES))}).\n"
            f"  → pip install -U openroboto\n"
            f"  Refusing to guess: the rules that judge your checkpoint come from "
            f"the competition, and applying the wrong ones tells you your upload "
            f"is broken when it is not -- or that it is fine when it is not."
        ) from None
