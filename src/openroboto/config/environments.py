"""Which subnet, and which backend, this config is talking to.

Three settings have to agree with each other: `subnet.network`, `subnet.netuid`
and `backend.url`. They are three independent switches for one decision, and
changing only some of them costs the miner money -- submit to testnet while
`openroboto status` asks production about it, and nothing is ever found, with no
error message anywhere that explains why.

So `environment` is one name for the whole decision, and `check_coherent()`
refuses to go on chain when the pieces disagree.

There is a fourth half-state, and it is the one those fields cannot see: **the
season came from one backend and the money leaves on another's chain.** Nothing
in the config contradicts anything else -- the contradiction is between the file
and an event that happened while it was being written. That fact is
`competition.source`, written by `openroboto init`, and `check_coherent()` takes
it as an argument for exactly this reason.

`urls.control_json` is a **validator** setting (`validator.yaml`); it is checked
here when it is set, because a validator pointed at another environment's
control.json reads a key that answers 401 and then sets no weights at all.

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
        """Where this environment publishes `public_key`. Read by external
        validators only; no miner command opens it."""
        return f"https://{self.host}/control.json" if self.host else ""


MAINNET = Environment(
    name="mainnet",
    network="finney",
    netuid=80,
    host="api.openroboto.ai",
)
"""Real emissions, real TAO. The entry fee is the season's own `params.fee`,
and it is not refundable."""

DEV = Environment(
    name="dev",
    network="test",
    netuid=313,
    host="api-dev.openroboto.ai",
)
"""Testnet. TAO comes from the faucet, so a wrong burn costs nothing.

The backend at `api-dev.openroboto.ai` runs `NETWORK=test` / `NETUID=313`, so a
payment verified here exercises the same path it does on mainnet."""

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
        # **must** say where the backend is. `urls.control_json` is optional --
        # only a validator sets it, and a validator that leaves it out simply
        # never picks up a rotated key.
        for label, url, required in (
            ("urls.control_json", control_json_url, False),
            ("backend.url", backend_url, True),
        ):
            if not url:
                if required:
                    problems.append(
                        f"environment=local requires {label} to be set explicitly "
                        f"-- left unset it keeps the built-in default, which is "
                        f"the **production** address. You would think you were "
                        f"testing locally while actually talking to the mainnet "
                        f"backend."
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
                f"\n     Straddling two environments means the seasons, the "
                f"status and the key come from different subnets."
            )
    return problems
