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

⚠️ As of 2026-08-19 the deployed dev backend is still configured for
`network: finney` / `netuid: 80` -- it watches mainnet, and is a sandbox in name
only. This entry describes where dev is going, not where it is: it becomes true
when the rebuilt backend is deployed there pointed at 313. Until then,
`environment: dev` will fail `check_coherent()` against a mainnet netuid, which
is the correct outcome -- it is exactly the combination that burns mainnet TAO at
the dev fee.
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


def check_coherent(
    *,
    environment: str,
    network: str,
    netuid: int,
    control_json_url: str,
    backend_url: str,
) -> list[str]:
    """Return every way this config contradicts itself; empty means it is consistent.

    Only reports mismatches against a **known** environment. A miner running their
    own backend has a host we do not recognise, and that is legitimate -- it is not
    this function's job to insist everyone uses ours. What it will not tolerate is
    naming an environment and then pointing somewhere else, because that is the
    shape of both money-losing mistakes described in the module docstring.
    """
    env = ENVIRONMENTS.get(environment)
    if env is None:
        known = ", ".join(sorted(ENVIRONMENTS))
        return [f"environment: unknown value {environment!r}, valid options: {known}"]

    problems: list[str] = []
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
