"""Which subnet, and which backend, this config is talking to.

Four settings have to agree with each other, and nothing used to check that they
did: `subnet.network`, `subnet.netuid`, `urls.control_json` and `backend.url`.
They were four independent switches for one decision, and changing only some of
them is not a harmless mistake -- both half-states cost the miner money:

- **control.json from dev, netuid still 80.** The dev backend publishes
  `burn_rate_tao: 0.01` while production publishes `0.1`. The burn goes to
  *mainnet* at a tenth of the required fee, production checks the amount and
  rejects it, and burns are not refunded.
- **netuid 313, backend still production.** The submission goes to testnet while
  `openroboto status` asks production about it. Nothing is ever found, and there
  is no error message anywhere that explains why.

So `environment` is one name for the whole decision, and `check_coherent()`
refuses to go on chain when the pieces disagree.

There is a third half-state, and it is the one those four fields cannot see:
**the season came from one backend and the money leaves on another's chain.**
Nothing in the config contradicts anything else -- the contradiction is between
the file and an event that happened while it was being written. That fact is
`competition.source`, written by `openroboto init`, and `check_coherent()` takes
it as an argument for exactly this reason.

**It deliberately does not supply `subnet.netuid`.** That field has no default on
purpose: a config that forgets it must fail, not quietly pick a subnet, because
picking the wrong one burns real TAO (see the comment on `Settings.netuid`).
Environments supply URLs, and then verify the netuid you set yourself.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class Environment:
    """One coherent set: a chain, a subnet, and the backend that watches them.

    `None` means "this environment does not decide that; your config does". It is
    spelled `None` rather than `0` or `""` on purpose -- a zero that secretly means
    "absent" is the shape of a bug this project has already paid for.
    """

    name: str
    network: str | None
    netuid: int | None
    host: str | None
    """Hosted backend. `None` = there is no hosted address; you supply the URLs."""

    @property
    def backend_url(self) -> str:
        return f"https://{self.host}" if self.host else ""

    @property
    def control_json_url(self) -> str:
        return f"https://{self.host}/control.json" if self.host else ""


MAINNET = Environment(
    name="mainnet",
    network="finney",
    netuid=80,
    host="api.openroboto.ai",
)
"""Real emissions, real TAO. The evaluation fee is whatever control.json says
(0.1 TAO as of 2026-08-19) and it is not refundable."""

DEV = Environment(
    name="dev",
    network="test",
    netuid=313,
    host="api-dev.openroboto.ai",
)
"""Testnet. TAO comes from the faucet, so a wrong burn costs nothing.

✅ True as of 2026-08-21: the rebuilt backend is deployed at
`api-dev.openroboto.ai` with `NETWORK=test` / `NETUID=313`, and its
`control.json` burn rate matches production's, so a burn verified here means the
same thing on mainnet.

(The note that used to sit here said dev still watched mainnet and that this
entry described where dev was going rather than where it was. That stopped being
true when the rebuilt backend was deployed and pointed at 313.)
"""

LOCAL = Environment(
    name="local",
    network=None,
    netuid=None,
    host=None,
)
"""Your own backend, wherever it is running -- `http://localhost:8001` while
developing, a staging box, a colleague's machine.

It pins nothing, because it cannot know what your backend watches. What it does
enforce is that you **said** where it is: with no URLs configured, the fields would
keep their built-in defaults, which are the *production* ones -- you would believe
you were testing locally while talking to mainnet's backend. So `local` with unset
URLs is refused, and `local` pointing at a hosted environment is refused too, since
that is a contradiction rather than a setup.
"""

ENVIRONMENTS: dict[str, Environment] = {env.name: env for env in (MAINNET, DEV, LOCAL)}

DEFAULT_ENVIRONMENT = MAINNET.name
"""Unset means mainnet, so an existing miner.yaml keeps behaving exactly as it
did. Every field an environment fills in is still overridable one by one."""


def resolve(name: str) -> Environment:
    """Look up an environment by name.

    Raises:
        KeyError: unknown name. Callers turn this into a message naming the valid
            ones -- a typo here must not silently fall back to mainnet.
    """
    return ENVIRONMENTS[name]


def host_of(url: str) -> str:
    """Hostname of a URL, empty string if there isn't one."""
    return urlparse(url).hostname or ""


def origin_of(url: str) -> str:
    """`host:port` of a URL, lowercased; empty string if there isn't one.

    The port is part of the answer here, unlike in `host_of`: two backends on one
    developer's machine differ by nothing else, and `127.0.0.1:8001` and
    `127.0.0.1:8011` are not the same subnet.
    """
    return urlparse(url).netloc.lower()


def find(backend_url: str) -> Environment | None:
    """Which environment hosts this backend; `None` when none of ours does.

    `None` is the ordinary answer for a self-hosted backend, and it is what makes
    the difference visible: for a host we know, this file already states the chain
    and the netuid, so `openroboto init` can write them; for one we do not, only
    that backend knows, so it has to be asked rather than assumed.
    """
    # A URL with no host at all matches nothing here on its own: `local` is the
    # only entry without a host and it stores `None`, never `""`.
    host = host_of(backend_url)
    return next((env for env in ENVIRONMENTS.values() if env.host == host), None)


def check_coherent(
    *,
    environment: str,
    network: str,
    netuid: int,
    control_json_url: str,
    backend_url: str,
    competition_source: str = "",
) -> list[str]:
    """Return every way this config contradicts itself; empty means it is consistent.

    Only reports mismatches against a **known** environment. A miner running their
    own backend has a host we do not recognise, and that is legitimate -- it is not
    this function's job to insist everyone uses ours. What it will not tolerate is
    naming an environment and then pointing somewhere else, because that is the
    shape of both money-losing mistakes described in the module docstring.

    `competition_source` is the backend that served the season in this workspace
    (`competition.source`, written by `openroboto init`). 🔴 **Without it the five
    self-describing fields can be perfectly consistent and still be wrong**, and
    they were: `init --backend-url <local backend>` used to take the season from
    that backend and write a mainnet workspace around it. Every field agreed with
    every other field -- mainnet, finney, 80, production URLs -- because the one
    fact that disagreed, *where the season came from*, was not among them. Empty
    means the workspace does not say (written before the key existed); that is not
    checked rather than assumed to be fine, which is why `init` writes it.
    """
    env = ENVIRONMENTS.get(environment)
    if env is None:
        known = ", ".join(sorted(ENVIRONMENTS))
        return [f"environment: unknown value {environment!r}, valid options: {known}"]

    problems: list[str] = []
    if (
        competition_source
        and backend_url
        and origin_of(competition_source) != origin_of(backend_url)
    ):
        problems.append(
            f"the competition in this workspace came from {competition_source}, "
            f"but backend.url is {backend_url}."
            f"\n     One backend named the season you trained for; a different one "
            f"is asked to confirm it in the second before you pay. They can agree "
            f"on `(track, seq)` and still be two different seasons -- both sides "
            f"seed the same tracks -- so the fee goes to whichever subnet "
            f"**this** file names."
            f"\n     → `openroboto init <directory> --backend-url "
            f"{competition_source}` writes a whole workspace that matches that "
            f"backend; or point backend.url at the one you meant to mine on."
        )
    if env.host is None:
        # Self-hosted backend: the chain is left unconstrained, but you
        # **must** say where the backend is.
        for label, url in (
            ("urls.control_json", control_json_url),
            ("backend.url", backend_url),
        ):
            if not url:
                problems.append(
                    f"environment=local requires {label} to be set explicitly -- "
                    f"left unset it keeps the built-in default, which is the "
                    f"**production** address. You would think you were testing "
                    f"locally while actually talking to the mainnet backend."
                )
            elif host_of(url) in {e.host for e in ENVIRONMENTS.values() if e.host}:
                problems.append(
                    f"environment=local, yet {label} points at a hosted environment "
                    f"({host_of(url)}) -- that is not one configuration, it is a "
                    f"contradiction."
                )
        return problems

    if env.netuid is not None and netuid and netuid != env.netuid:
        problems.append(
            f"environment={env.name} means netuid {env.netuid}, but the config "
            f"says {netuid}."
            f"\n     The two have to be changed together -- TAO burned on the "
            f"wrong subnet is not refunded."
        )
    if env.network is not None and network and network != env.network:
        problems.append(
            f"environment={env.name} means network {env.network!r}, "
            f"but the config says {network!r}"
        )

    # Only complain when the URL points at **another known environment**.
    # Pointing at a self-hosted backend is not an error.
    known_hosts = {e.host: e.name for e in ENVIRONMENTS.values()}
    urls = (
        ("urls.control_json", control_json_url),
        ("backend.url", backend_url),
    )
    for label, url in urls:
        other = known_hosts.get(host_of(url))
        if other is not None and other != env.name:
            problems.append(
                f"{label} points at the {other} environment "
                f"({host_of(url)}), while environment={env.name}."
                f"\n     The burn rate comes from control.json and status comes "
                f"from backend.url -- when they straddle two environments, you "
                f"burn on one subnet at another subnet's rate."
            )
    return problems
