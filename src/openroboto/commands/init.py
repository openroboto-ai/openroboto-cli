"""`openroboto init` -- create a workspace for one competition.

The goal is very concrete: **zero cloning for miners, start to finish**.
Getting `miner.example.yaml` and an editable strategy script used to require
`git clone`-ing the whole subnet repository; now one command after
`pip install openroboto` produces them. The templates are packed into the
wheel (`openroboto/templates/`).

What it produces is not a generic template any more. The subnet runs several
competitions at once and they do not accept the same thing, so `init` asks the
backend which ones are open, the miner picks one, and that season's whole spec
is written into `miner.yaml`. **Every later command reads that snapshot off
disk**: `build` / `train` / `check` never touch the network, and a competition
edited mid-round cannot silently change how a checkpoint is built.

Two rules hold this together:

- **the request comes first, and nothing is written until it returns.** Unpack
  first and a miner whose backend was unreachable is left with a workspace that
  has no competition in it -- and the next thing they do is `build`;
- **no fall back to a built-in competition.** A guessed season is a season the
  miner finds out about when they pay.

`init` is now the one command that needs the network. `build` / `train` /
`check` still do not, and the error message says so -- otherwise the first
miner behind a flaky link concludes the whole tool chain is online-only.
"""

from __future__ import annotations

import argparse
import difflib
import shutil
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from string import Template
from typing import Any

import yaml
from openroboto_protocol.schemas import Competition

from openroboto import adapters
from openroboto.backend_api import BackendError, fetch_competitions, fetch_netuid
from openroboto.competition import load_snapshot
from openroboto.config import ConfigError, Settings, environments
from openroboto.console import fail, hint, say

STRATEGIES = ("simple", "example")
"""simple = the minimal implementation that gets through the whole flow;
example = the annotated teaching version."""

DEFAULT_STRATEGY = "simple"

CONFIG_TEMPLATE = {"miner": "miner.yaml", "validator": "validator.yaml"}
README_TEMPLATE = {"miner": "README-miner.md", "validator": "README-validator.md"}

SECTION = "competition"
#: The columns copied out of the competition row into `miner.yaml`, in the order
#: they are written. `id` is in there to be displayed and to be sent on chain
#: after it has been re-resolved; it is **not** what the durable lookup keys on
#: -- see `competition.Snapshot`.
SECTION_KEYS = (
    "id",
    "track",
    "seq",
    "label",
    "adapter",
    "status",
    "submit_opens_at",
    "submit_closes_at",
    "eval_starts_at",
    "eval_ends_at",
    "champion_announced_at",
    #: 🔴 Top-level columns, **not** keys inside `params`. Written into
    #: `params.base` they are simply not there when read back, and "the base
    #: model has not changed" then passes by never having been compared.
    "base_repo",
    "base_revision",
    #: Which rule book judges this season's checkpoint, which files feed the
    #: fingerprint, which loader the evaluator uses. `adapter` no longer answers
    #: that -- its `_openpi` / `_lingbot` suffixes are historical and must not be
    #: read as the base model (protocol 0.8.0).
    #:
    #: 🔴 `None` here means **not decided yet, refuse** -- the opposite of the
    #: `None` on the five instants above, which means "this boundary is not
    #: checked". `real/1` is `None` today. Defaulting it to `openpi` would judge a
    #: submission somebody already paid for by rules nobody chose for that season.
    "base_model_family",
)


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "init", help="Create a ready-to-use workspace (no clone required)"
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="target directory, defaults to the current one",
    )
    parser.add_argument(
        "-s",
        "--strategy",
        choices=STRATEGIES,
        default="",
        help="which strategy script to unpack; defaults to the one the "
        "competition asks for",
    )
    parser.add_argument(
        "--validator",
        action="store_true",
        help="write validator.yaml (for external validators) instead of a "
        "strategy script",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="rewrite only the competition section of an existing miner.yaml, "
        "leaving every other line untouched",
    )
    parser.add_argument(
        "--backend-url",
        default="",
        help="where to ask for the competition list; defaults to backend.url in "
        "the target's miner.yaml, or to production",
    )
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    if args.validator:
        # External validators watch the whole subnet, so there is no competition
        # to pick. **Not one request is sent on this path.**
        return _write_validator(Path(args.directory), args.force)

    target = Path(args.directory)
    try:
        backend_url = _backend_url(args, target)
        rows = _competitions(backend_url)
        # 🔴 `--refresh` re-reads **the season this workspace already mines**.
        #    It must not ask.
        #
        #    Asking looks harmless because `_pick` takes the only row without a
        #    prompt when one season is open -- which is how this went unnoticed.
        #    With several open, a mistyped digit silently repoints the workspace
        #    at a different season while the training hyperparameters, the
        #    strategy script and whatever is already trained in `tmp/` stay
        #    exactly where they were. The contract of this flag is "rewrite the
        #    competition section, leave every other line untouched"; switching
        #    seasons is the one thing that cannot honour it.
        live = _refresh_target(target, rows) if args.refresh else _pick(rows)
        adapters.resolve(live.adapter)
        strategy = _strategy(args.strategy, live)
        if args.refresh:
            return _refresh(target / CONFIG_TEMPLATE["miner"], live, backend_url)
        subnet = _subnet_of(backend_url)
    except (BackendError, ConfigError) as exc:
        fail(str(exc))
        return 1

    # Everything is built in memory before anything reaches the disk: a failure
    # above this line leaves the target directory exactly as it was found.
    contents = {
        CONFIG_TEMPLATE["miner"]: write_section(
            _config_text(subnet), live, subnet.backend_url
        ),
        "train_strategy.py": _template(f"{strategy}/train_strategy.py"),
        "README.md": _template(README_TEMPLATE["miner"]),
        # Inside the package it is called `gitignore` (no leading dot): dotfiles
        # are easily swallowed by all sorts of default exclusion rules during
        # packaging and distribution, and when they are swallowed **nothing
        # raises an error** -- it shows up as the file simply not being in the
        # miner's workspace, and the next `git add .` commits their wallet
        # password. The dot is added only when it lands on disk.
        ".gitignore": _template("gitignore"),
    }

    target.mkdir(parents=True, exist_ok=True)
    for name, content in contents.items():
        say(f"✅ {_write(target / name, content, args.force)}")

    say("")
    say(f"Competition: {live.label} ({live.track}/{live.seq} · cid={live.id})")
    say(
        f"Subnet:      netuid {subnet.netuid} on {subnet.network or '(not set)'} "
        f"({subnet.environment} · {subnet.backend_url})"
    )
    if not subnet.network:
        # The backend states its netuid and nothing states its chain, so this is
        # the one field `init` cannot fill in. Left silent it defaults to
        # nothing, `require_for_chain()` refuses at `burn`, and the miner has to
        # work out which of two identically-numbered subnets they are on then.
        hint(
            f"⚠️  {subnet.backend_url} says it watches netuid {subnet.netuid}, but a "
            f"netuid does not say which chain it is on.\n"
            f"    Set subnet.network in miner.yaml (`test` for a testnet backend, "
            f"`finney` for a mainnet one) — every on-chain command refuses until "
            f"you do."
        )
    say("Next steps (the workspace README.md has the full walkthrough):")
    say(
        "  1. Fill in huggingface.token / username and subnet.hotkey_ss58 in miner.yaml"
    )
    say("  2. `openroboto doctor` — catch environment problems before you pay")
    say("  3. `openroboto build` → `openroboto train` →")
    say("     `openroboto check` → `openroboto submit`")
    say("")
    say("⚠️  miner.yaml will hold your wallet password and HF token; .gitignore")
    say("    already excludes it. Do not take it off the ignore list.")
    return 0


def _backend_url(args: argparse.Namespace, target: Path) -> str:
    """`--backend-url` > the target's own `miner.yaml` > production.

    The middle tier is what makes `--refresh` work for anyone not on production:
    a miner pointed at the dev subnet has that address in the config already,
    and re-asking them for it on every refresh is one more chance to refresh a
    dev workspace against production's competition list.
    """
    if args.backend_url:
        return str(args.backend_url)
    config = target / CONFIG_TEMPLATE["miner"]
    settings = Settings.load(str(config)) if config.is_file() else Settings()
    if not settings.backend_url:
        raise ConfigError(
            f"{config} sets no backend.url (`environment: local` supplies none), "
            f"so there is nowhere to ask which competitions are open.\n"
            f"  → set backend.url in that file, or pass `--backend-url <address>`"
        )
    return settings.backend_url


@dataclass(frozen=True)
class Subnet:
    """Which chain a workspace talks to -- **decided by the backend that served
    its competition**, never by the shipped template.

    That distinction is the whole point of this type. `init` used to write a
    static mainnet block around whatever season it had just fetched, so
    `--backend-url <a testnet backend>` produced a workspace whose
    `competition:` came from testnet and whose `environment` / `netuid` /
    `backend.url` said mainnet. Every field agreed with every other one; the
    only thing that disagreed was reality, and the first command to notice
    would have been the one that pays.
    """

    environment: str
    #: `""` = the backend did not say, and nothing else may answer for it. The
    #: config carries the empty value through, and `require_for_chain()` refuses.
    network: str
    netuid: int
    #: Normalised: what goes into `backend.url` and into `competition.source`,
    #: so the two are compared without a trailing slash telling them apart.
    backend_url: str


def _subnet_of(backend_url: str) -> Subnet:
    """Resolve the backend address into the chain settings to write down.

    A host this client knows (`api.openroboto.ai`, `api-dev.openroboto.ai`) is
    described by `config/environments.py`, which is also what `check_coherent()`
    enforces later -- taking the values from anywhere else would be two sources
    for one fact.

    Any other host is somebody's own backend, so **it is asked** which subnet it
    watches instead of being assumed onto ours. Unreachable there is a refusal,
    exactly as it is for the competition list: `init` writes nothing rather than
    a workspace whose netuid is a guess.
    """
    env = environments.find(backend_url)
    if env is not None:
        return Subnet(
            environment=env.name,
            network=env.network or "",
            netuid=env.netuid or 0,
            backend_url=env.backend_url,
        )
    return Subnet(
        environment=environments.LOCAL.name,
        # Not derived from the netuid: netuid 313 exists on both chains, and the
        # one that costs money is the one nobody chose deliberately.
        network="",
        netuid=fetch_netuid(backend_url),
        backend_url=backend_url.rstrip("/"),
    )


def _config_text(subnet: Subnet) -> str:
    """The `miner.yaml` template with this subnet's values filled in.

    `string.Template` and not a YAML round trip, for the same reason
    `write_section` does text surgery: every comment in that file is there to
    stop a specific expensive mistake, and `safe_dump` deletes all of them.
    """
    return Template(_template(CONFIG_TEMPLATE["miner"])).substitute(
        environment=subnet.environment,
        # `""` rather than an empty value: YAML reads a bare `network:` back as
        # null, and "the backend did not say" has to survive the round trip as
        # something `Settings` can hold.
        network=subnet.network or '""',
        netuid=subnet.netuid,
        control_json=f"{subnet.backend_url}/control.json",
        backend_url=subnet.backend_url,
    )


def _competitions(backend_url: str) -> list[Competition]:
    """Ask which competitions are open. Unreachable = stop, never a default."""
    rows = list(fetch_competitions(backend_url).data)
    if not rows:
        raise BackendError(
            f"{backend_url} lists no competition taking submissions right now.\n"
            f"  → nothing to fix on your side; watch for the next one to open"
        )
    return rows


def _refresh_target(target: Path, rows: list[Competition]) -> Competition:
    """The season this workspace already mines, as the backend serves it today.

    🔴 **Locates, never asks.** See the note at the call site for why asking is
    the dangerous option here.

    Refusing is the only move when the season is no longer listed: a workspace
    points at one season, and `--refresh` cannot repoint it without breaking
    its own contract. `openroboto init <dir>` is how a miner moves to another
    season, and it says so.
    """
    settings = Settings.load(str(target / CONFIG_TEMPLATE["miner"]))
    snapshot = load_snapshot(settings)
    if snapshot is None:
        raise ConfigError(
            f"{target / CONFIG_TEMPLATE['miner']} has no competition section, "
            f"so there is nothing to refresh.\n"
            f"  → `openroboto init {target}` writes a workspace from scratch"
        )
    for row in rows:
        if row.track == snapshot.track and row.seq == snapshot.seq:
            return row
    raise ConfigError(
        f"The backend no longer lists {snapshot.name} ({snapshot.label}) among "
        f"the competitions taking submissions, so nothing was rewritten.\n"
        f"  → to mine a different season, run `openroboto init <directory>`; "
        f"--refresh only updates the one this workspace already has"
    )


def _pick(rows: list[Competition]) -> Competition:
    """One competition → take it. Several → choose by number.

    Asking which of one is not a choice, it is a keystroke. Numbers rather than
    names because a name is a thing to mistype, and mistyping this one picks the
    wrong season. The order is the backend's `(track, seq)`.
    """
    if len(rows) == 1:
        return rows[0]

    say("Competitions taking submissions:")
    for number, row in enumerate(rows, start=1):
        say(f"  {number}. {row.label} ({row.track}/{row.seq} · cid={row.id})")
    say("")
    while True:
        try:
            answer = input(f"Which one? [1-{len(rows)}] ").strip()
        except EOFError as exc:
            # No terminal to ask (CI, a wrapper, `< /dev/null`). Unanswered is
            # unanswered -- but a stack trace here reads like a broken client,
            # and the miner cannot tell it apart from one.
            raise ConfigError(
                f"{len(rows)} competitions are open and nothing answered which "
                f"one to set up, so no workspace was written.\n"
                f"  → run this from a terminal, or pipe the number in: "
                f"`echo 1 | openroboto init …`"
            ) from exc
        if answer.isdigit() and 1 <= int(answer) <= len(rows):
            return rows[int(answer) - 1]
        hint(f"Type a number between 1 and {len(rows)}.")


def _strategy(explicit: str, live: Competition) -> str:
    """`-s` wins; otherwise the competition names its own starting script.

    A name this client does not ship is refused rather than quietly replaced
    with the default: handing a miner the π0.5 starting script for a competition
    that asked for another one means they find out at `check`, after training.
    """
    if explicit:
        return explicit
    wanted = str(live.params.get("strategy_template") or DEFAULT_STRATEGY)
    if wanted not in STRATEGIES:
        raise ConfigError(
            f"{live.label} asks for the `{wanted}` strategy template, which this "
            f"client does not ship (it has: {', '.join(STRATEGIES)}).\n"
            f"  → pip install -U openroboto\n"
            f"  → or pass `-s {DEFAULT_STRATEGY}` to start from the generic one"
        )
    return wanted


def render_section(live: Competition, source: str) -> str:
    """The `competition:` block for `miner.yaml`, as YAML text.

    `params` goes in **verbatim**. It is this season's spec (fee, base model,
    image, camera list) and it changes every season; picking fields out of it
    here would mean a CLI release before a season could add a key.

    `source` is the backend this row came from. It is not a column of the row --
    no backend can tell you which backend you were talking to -- and it is
    written down because it is the one fact `check_coherent()` cannot derive
    from the rest of the file: `(track, seq)` matches across environments (both
    sides seed the same tracks), so a season fetched from one backend sits
    unremarkably in a config that pays on another's chain.
    """
    row = live.model_dump(mode="json")
    section: dict[str, Any] = {key: row[key] for key in SECTION_KEYS}
    section["source"] = source
    section["params"] = row["params"]
    return yaml.safe_dump({SECTION: section}, sort_keys=False, allow_unicode=True)


def write_section(config_text: str, live: Competition, source: str) -> str:
    """Replace the `competition:` block; leave every other byte alone.

    Text surgery rather than a YAML round trip, and that is the whole point:
    `yaml.safe_load` + `yaml.safe_dump` washes out every comment and reformats
    every value the miner hand-edited, and `--refresh` quietly overwriting
    hand-edited fields is the worst accident this command can cause. The cost is
    that a section moved somewhere unexpected has to be fixed by hand -- a cost
    that is **visible**, because it is refused out loud instead of guessed at.
    """
    lines = config_text.splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if line.startswith(f"{SECTION}:")), None
    )
    if start is None:
        raise ConfigError(
            f"Cannot find the `{SECTION}:` section in this config, so there is "
            f"nothing to rewrite -- and this will not guess where it belongs.\n"
            f"  → put a top-level line `{SECTION}:` back (no indentation) and run "
            f"this again, or run `openroboto init <new-directory>` for a fresh "
            f"workspace"
        )
    # The section ends at the first line that is not indented. A blank line ends
    # it too, which keeps the blank line separating it from the next section.
    end = start + 1
    while end < len(lines) and lines[end][:1] in (" ", "\t"):
        end += 1
    return "".join(lines[:start]) + render_section(live, source) + "".join(lines[end:])


def _refresh(config_path: Path, live: Competition, source: str) -> int:
    """Rewrite one section of a config the miner already owns.

    The backup and the diff are not a nicety: overwriting `miner.yaml` is the
    only part of this command that cannot be undone, so `miner.yaml.bak` **is**
    the rollback path.

    🔴 **This command rewrites the competition section and nothing else** -- that
    is its contract, and it is also why the season and the config have to be
    checked against each other *here*. A refresh from a different backend than
    the file talks to cannot be repaired by writing: the chain settings around
    the new season are the old ones, by design. So it is refused instead.
    """
    try:
        before = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(
            f"Cannot read {config_path}: {exc}\n"
            f"  → --refresh updates an existing config; run "
            f"`openroboto init <directory>` to create one"
        )
        return 1

    try:
        conflicts = _conflicts(config_path, source)
        after = write_section(before, live, source)
    except ConfigError as exc:
        fail(str(exc))
        return 1

    if conflicts:
        fail(
            "This config does not match the backend the competition came from, "
            "so nothing was rewritten:\n  - " + "\n  - ".join(conflicts)
        )
        return 1

    if before == after:
        say(f"✅ {config_path} already matches {live.label} — nothing to change")
        return 0

    for line in difflib.unified_diff(
        before.splitlines(), after.splitlines(), "before", "after", lineterm="", n=1
    ):
        say(line)
    say("")

    backup = config_path.with_suffix(config_path.suffix + ".bak")
    shutil.copyfile(config_path, backup)
    config_path.write_text(after, encoding="utf-8")
    say(f"✅ {config_path} — competition section updated ({live.label})")
    say(f"   the previous version is kept at {backup}")
    return 0


def _conflicts(config_path: Path, source: str) -> list[str]:
    """How this config disagrees with the backend the season came from.

    The same rule the chain commands run (`Settings.require_for_chain()`), given
    the same extra fact -- one implementation, so a refresh cannot pass a check
    that `burn` would then fail, or the other way round.
    """
    settings = Settings.load(str(config_path))
    return environments.check_coherent(
        environment=settings.environment,
        network=settings.network,
        netuid=settings.netuid,
        control_json_url=settings.control_json_url,
        backend_url=settings.backend_url,
        competition_source=source,
    )


def _write_validator(target: Path, force: bool) -> int:
    target.mkdir(parents=True, exist_ok=True)
    contents = {
        CONFIG_TEMPLATE["validator"]: _template(CONFIG_TEMPLATE["validator"]),
        "README.md": _template(README_TEMPLATE["validator"]),
        ".gitignore": _template("gitignore"),
    }
    for name, content in contents.items():
        say(f"✅ {_write(target / name, content, force)}")
    say("")
    say(
        "Next: fill in backend.public_key in validator.yaml, "
        "then run `openroboto validator run`"
    )
    return 0


def _template(relative: str) -> str:
    """Read a template packed inside the package."""
    return (files("openroboto") / "templates" / relative).read_text(encoding="utf-8")


def _write(path: Path, content: str, force: bool) -> Path:
    """Write the file; skip when it already exists and --force was not given
    -- we must not silently overwrite a config the miner has filled in."""
    if path.exists() and not force:
        hint(f"⏭️  Already exists, skipped: {path} (pass --force to overwrite)")
        return path
    path.write_text(content, encoding="utf-8")
    return path
