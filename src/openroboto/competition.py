"""The competition this workspace mines: the snapshot in `miner.yaml`, and the
check run against the backend right before money moves.

Two halves, deliberately separated:

- **the snapshot** -- `openroboto init` writes the competition's whole spec into
  `miner.yaml` and every later command reads it from there, offline. Training
  does not need the network, and a competition that changed under a miner
  mid-round must not silently change how their checkpoint is built;
- **the check** -- `judge()` compares that snapshot against what the backend
  serves *now*, and it is the last gate before a fee is paid.

## Why the durable key is `(track, seq)` and not `id`

`schemas.Competition` says it in full: `id` is an identity column, local to one
database. A reseed or a restore hands out different numbers for the same
seasons, and a config file still holding the old one points at another
competition without anything looking wrong. So `miner.yaml` keys on
`(track, seq)` -- what `ux_competitions_track_seq` makes unique -- and the `id`
that goes on chain is the one **resolved at submit time**, from the row the
backend just served.

## Why the fee is never read from anywhere else

`params.fee` belongs to one season. `control.json`'s `payment` block and
`Settings.burn_rate_tao` are subnet-wide, and comparing a season's fee against
a subnet-wide number is not a comparison at all. Once a config has a
competition section, this module is the only source of the amount and the
address.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, NoReturn

from openroboto_protocol.commitment import Track
from openroboto_protocol.schemas import Competition

from openroboto.backend_api import BackendError, fetch_competitions
from openroboto.config import ConfigError, Settings
from openroboto.console import fail, say

#: The two ways a season can be paid for. `kind` is competition data, read from
#: `params.fee.kind` -- it is **not** derived from the adapter or the track.
#: A second table saying "the real track transfers" would be a second thing to
#: keep in step with the data, and they would disagree on the season that breaks
#: the pattern.
#:
#: ⚠️ `free_period` is **not** one of these. It is one of the `payment_status`
#: words, meaning "this payment counts as settled", not a way to pay.
BURN: Final = "burn"
TRANSFER: Final = "transfer"
FEE_KINDS: Final = (BURN, TRANSFER)

REFRESH_HINT: Final = "  → `openroboto init --refresh` rewrites it from the backend"

#: The **shape** of an SS58 address (substrate prefix 42), character for
#: character the backend's `app/domain/fee.py::_SS58_RE`: base58 alphabet minus
#: `0OIl`, first character always `5`, length exactly 48.
#:
#: Copied rather than imported because the protocol package publishes no such
#: pattern, and this side needs it *before* the money moves while that side only
#: reaches it while filing the payment. The two must stay identical: a season the
#: backend refuses to parse cannot take a submission, so an address this accepts
#: and that one rejects is a fee paid into a season that will never file it.
#:
#: ⚠️ Shape, not validity -- checking the checksum needs base58 and a key of the
#: SDK's, and this module is pure. What it catches is a placeholder or an address
#: that lost a character on the way into the season's config, which is exactly
#: how 2 TAO reaches a stranger irreversibly.
_SS58_RE: Final = re.compile(r"\A5[1-9A-HJ-NP-Za-km-z]{47}\Z")

#: Printed in full, never truncated: a miner who wants to check the address
#: against the announcement has to be able to read all of it.
_TIME_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"


class PrecheckFailed(Exception):
    """The pre-payment check did not pass, so nothing was paid.

    The reason and the next step have **already been printed** by the time this
    is raised; the command layer only has to return 1
    (`commands/submit.py` is written against exactly that).

    🔴 **Raise it through `_refuse()`, never directly.** The contract above is a
    promise made to the caller, and a bare `raise` breaks it silently: the
    command layer returns 1 and prints nothing at all, on the one path where the
    miner most needs a sentence. That happened — an unreachable backend exited 1
    with an empty stderr, discarding the diagnosis the message carried.

    The two are one action, so they live in one function rather than in every
    author's memory.
    """


def _refuse(message: str) -> NoReturn:
    """Say why, then refuse. **The only sanctioned way to fail a precheck.**"""
    fail(message)
    raise PrecheckFailed(message)


@dataclass(frozen=True)
class Fee:
    """One season's entry fee, as read from that season's `params.fee`."""

    kind: str
    amount_tao: float
    #: 🔴 `None` is a real value and it means **do not pay**: the real track's
    #: collection address has not been published yet. Whoever reads it fails
    #: closed. It is legitimately `None` for `kind == "burn"`, which needs no
    #: address at all -- which is why the gate lives in `judge()`, where the
    #: kind is known, and not in the parser below.
    coldkey: str | None


@dataclass(frozen=True)
class Snapshot:
    """The `competition:` section of `miner.yaml`, verbatim.

    `raw` is kept as-is rather than mapped onto fields: `params` changes every
    season, and a mapping here would mean a CLI release before a season could
    add a key.
    """

    raw: Mapping[str, Any]

    @property
    def track(self) -> str:
        return str(self.raw.get("track", ""))

    @property
    def seq(self) -> int:
        return int(self.raw.get("seq", 0) or 0)

    @property
    def label(self) -> str:
        return str(self.raw.get("label", "")) or self.name

    @property
    def name(self) -> str:
        """`real/1` -- the durable key, and how a season is named to a human."""
        return f"{self.track}/{self.seq}"

    @property
    def adapter(self) -> str:
        return str(self.raw.get("adapter", ""))

    @property
    def status(self) -> str:
        """`draft` | `active` | `archived`, frozen at the moment `init` ran.

        control.json only ever had one word here (`active`), so a workspace
        reading this instead of that file gains two: `archived` is a season that
        has finished, `draft` one whose spec is not published yet. Neither is
        something to train against.

        🔴 This is the **snapshot's** copy, so it answers "which season did I
        sign up for", not "is that season still open". The live answer is bought
        at `submit`, from the row the backend serves in the second before the
        fee is paid (`judge()`), and nothing here replaces it.
        """
        return str(self.raw.get("status", ""))

    @property
    def training(self) -> Mapping[str, Any]:
        """`params.training` — what to train **in**, **on** and **from**.

        `image` (the container), `dataset` (`{train, val}`) and `checkpoint`
        (the base weights training starts from).

        🔴 `checkpoint` is not `base_repo`. `base_repo` is the **baseline** the
        leaderboard's `delta_vs_base` is measured against; this is where the
        miner's own run begins. They were visibly different for π0.5 --
        `openroboto-ai/pi05-libero-pytorch` against
        `gs://openpi-assets/checkpoints/pi05_base` -- and one field cannot mean
        both without being wrong for one of them.
        """
        value = self.params.get("training")
        return value if isinstance(value, Mapping) else {}

    @property
    def base_model_family(self) -> str:
        """Which base model this season runs on, or `""` when the key is absent.

        `""` covers two cases that behave the same here and are told apart by
        `adapters.base_model_family()`: a `miner.yaml` written before the key
        existed, and a backend row whose season has not decided yet (`null`).
        Both mean "this file does not say", and neither may be guessed past --
        the second one especially, because a `null` there is the backend
        *refusing* to name a base model, not omitting one.
        """
        return str(self.raw.get("base_model_family") or "")

    @property
    def params(self) -> Mapping[str, Any]:
        value = self.raw.get("params")
        return value if isinstance(value, Mapping) else {}

    @property
    def base(self) -> tuple[str | None, str | None]:
        """`(base_repo, base_revision)`.

        🔴 Top-level columns of the competition row, **not** keys inside
        `params`. Written into `params.base` they are simply not there when read
        back, and "the base model did not change" then passes by never having
        been compared.
        """
        return _text(self.raw.get("base_repo")), _text(self.raw.get("base_revision"))

    def fee(self) -> Fee:
        return fee_of(self.params, where="miner.yaml")


def load_snapshot(settings: Settings) -> Snapshot | None:
    """The competition section, or `None` for a config written before there was
    more than one competition.

    `None` is not an error: those configs keep working exactly as they did, on
    the π0.5 simulation path (`adapters.DEFAULT_ADAPTER`).

    The mark of a real snapshot is the durable key `(track, seq)`, not the
    presence of the section: the template ships the section with its values
    empty, and treating that as a snapshot would send every fresh workspace into
    the pre-payment check looking for a season named `/0`.
    """
    section = settings.competition
    if not section.get("track") or not section.get("seq"):
        return None
    return Snapshot(section)


def fee_of(params: Mapping[str, Any], *, where: str) -> Fee:
    """Read `params.fee`. Every missing piece raises -- **no defaults**.

    A `.get(name, fallback)` here is the fail-open version of the whole module:
    the fallback is what gets paid on the day the key is misspelled.
    """
    fee = params.get("fee")
    if not isinstance(fee, Mapping):
        raise ConfigError(
            f"This competition's entry fee is missing from {where} "
            f"(`competition.params.fee`), so there is no amount to pay.\n"
            + REFRESH_HINT
        )
    kind = str(fee.get("kind", ""))
    if kind not in FEE_KINDS:
        raise ConfigError(
            f"This competition's payment kind in {where} is `{kind or '(empty)'}`, "
            f"which this client does not know (it knows: {', '.join(FEE_KINDS)}).\n"
            f"  → pip install -U openroboto"
        )
    amount = fee.get("amount_tao")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        raise ConfigError(
            f"This competition's entry fee amount in {where} is "
            f"`{amount!r}`, not a number.\n" + REFRESH_HINT
        )
    return Fee(kind=kind, amount_tao=float(amount), coldkey=_text(fee.get("coldkey")))


def render_window(live: Competition, now: datetime) -> str:
    """One line about the submission window.

    🔴 `None` on either instant means **that boundary is not checked** -- not
    "unknown", not "closed". A season with no deadline takes submissions
    indefinitely, and inventing a date for it is how a miner is told to hurry
    for no reason.
    """
    opens, closes = live.submit_opens_at, live.submit_closes_at
    if opens is not None and opens > now:
        return f"opens {_stamp(opens)} (not open yet, in {_span(opens - now)})"
    if closes is None:
        return "no closing date set for this competition"
    if closes <= now:
        return f"closed {_stamp(closes)}"
    return f"{_span(closes - now)} left (closes {_stamp(closes)})"


@dataclass(frozen=True)
class Verdict:
    """What the backend says about this season, at the moment of paying."""

    live: Competition
    fee: Fee
    window: str

    @property
    def cid(self) -> int:
        """The competition id **resolved just now** -- this is what goes on
        chain, never the number that was sitting in `miner.yaml`."""
        return self.live.id

    @property
    def amount_tao(self) -> float:
        return self.fee.amount_tao

    @property
    def kind(self) -> str:
        return self.fee.kind


def judge(
    snapshot: Snapshot, live_rows: Sequence[Competition], now: datetime
) -> Verdict:
    """Compare the snapshot against the backend's current rows. Pure.

    The order is by **what it costs to get it wrong**, and one step of it is not
    negotiable: the address is checked for *existence* before it is checked for
    *equality*. Reversed, a `None` in the config equals the `None` the backend
    serves today, the season passes as "unchanged", and the transfer is then
    addressed to nobody.
    """
    live = _find(snapshot, live_rows)

    if live.status != "active":
        raise PrecheckFailed(
            f"{_name(live)} is `{live.status}`, so it is not taking submissions.\n"
            f"{_active_now(live_rows)}\n" + REFRESH_HINT
        )

    window = render_window(live, now)
    if live.submit_opens_at is not None and live.submit_opens_at > now:
        raise PrecheckFailed(
            f"{_name(live)} is not open for submissions yet: {window}.\n"
            f"  → nothing to fix, wait for the opening time above"
        )
    if live.submit_closes_at is not None and live.submit_closes_at <= now:
        raise PrecheckFailed(
            f"{_name(live)} {window} -- submissions are no longer accepted.\n"
            f"{_active_now(live_rows, exclude=live)}\n" + REFRESH_HINT
        )

    live_fee = fee_of(live.params, where=f"the backend's {_name(live)}")

    # 🔴 One direction only: **the real track has to be a transfer**. This is not
    # a track → kind table (the module header says why there must not be one); it
    # is this side's copy of the backend's own rule, `app/domain/fee.py::parse_fee`
    # refusing any `kind` but `transfer` when it prices a real-track submission.
    #
    # The comparison the season check makes -- snapshot against live row -- cannot
    # see this one at all: `miner.yaml` was copied from the same row at `init`, so
    # a season misconfigured as `burn` matches itself and the check passes. What
    # follows is `add_stake_burn`, which has **no recipient**: the fee is
    # destroyed, nobody is paid, the submission still counts as unpaid, and there
    # is nothing to refund because there is nobody who received it.
    #
    # The other direction (`sim` ⇒ `burn`) is deliberately **not** checked. It is
    # not a rule anywhere -- no backend function refuses a simulation season that
    # collects by transfer -- so writing it here would be inventing one, and the
    # first season that breaks the pattern would be refused by the CLI while the
    # backend was happy to take it.
    if live.track == Track.REAL and live_fee.kind != TRANSFER:
        raise PrecheckFailed(
            f"{_name(live)} is a real-track season but its entry fee is "
            f"configured as `{live_fee.kind}`, which the backend does not accept "
            f"for the real track.\n"
            f"  Refusing to pay: a burn is destroyed rather than sent, so paying "
            f"this way would spend {live_fee.amount_tao} TAO that no one "
            f"receives, irreversibly, and leave the submission unpaid anyway.\n"
            f"  Nothing was paid. This is a mistake in the season's own "
            f"configuration, not in your workspace -- report it to the subnet "
            f"operators; `openroboto init --refresh` cannot fix it."
        )

    # 🔴 Existence before equality. See the docstring.
    if live_fee.kind == TRANSFER and live_fee.coldkey is None:
        raise PrecheckFailed(
            f"{_name(live)} has not published its collection address yet "
            f"(`params.fee.coldkey` is null), so there is nowhere to send "
            f"{live_fee.amount_tao} TAO.\n"
            f"  Refusing to pay: sending it anywhere else -- a burn address, the "
            f"simulation track's behaviour, any default -- spends the fee before "
            f"the submission is even made.\n"
            f"  → wait for the address to be announced, then "
            f"`openroboto init --refresh`"
        )

    if live_fee.kind == TRANSFER and not _SS58_RE.match(live_fee.coldkey or ""):
        raise PrecheckFailed(
            f"{_name(live)}'s collection address is not an SS58 address:\n"
            f"  {live_fee.coldkey!r}\n"
            f"  Refusing to pay: a transfer is addressed by that string alone, so "
            f"sending {live_fee.amount_tao} TAO at a placeholder -- or at an "
            f"address that lost a character somewhere -- puts it where nobody "
            f"holds the key, irreversibly.\n"
            f"  Nothing was paid. Report it to the subnet operators; this is the "
            f"season's own configuration, and `openroboto init --refresh` copies "
            f"it rather than fixing it."
        )

    mine = snapshot.fee()
    if live_fee.coldkey != mine.coldkey:
        raise PrecheckFailed(
            f"{_name(live)}'s collection address has changed since you ran "
            f"`init`.\n"
            f"  miner.yaml: {mine.coldkey}\n"
            f"  backend:    {live_fee.coldkey}\n" + REFRESH_HINT
        )
    if live_fee.amount_tao != mine.amount_tao or live_fee.kind != mine.kind:
        raise PrecheckFailed(
            f"{_name(live)}'s entry fee has changed since you ran `init`.\n"
            f"  miner.yaml: {mine.amount_tao} TAO ({mine.kind})\n"
            f"  backend:    {live_fee.amount_tao} TAO ({live_fee.kind})\n"
            + REFRESH_HINT
        )

    # 🔴 **A changed `base_repo` does not block the payment** (a gate removed on
    # 2026-09-01).
    #
    # This used to compare `(base_repo, base_revision)`, on the stated grounds
    # that "a checkpoint you trained on the old base will be judged against the
    # new one, so the fee buys an evaluation of the wrong model". **That is not
    # true**, and this file's own `Competition.training` docstring says why:
    # `base_repo` is the **leaderboard's `delta_vs_base` reference**, while the
    # miner's training starting point is `params.training` (on the π0.5 season
    # the two were plainly different addresses). Changing the baseline only
    # changes what the Δ column compares against, not how this checkpoint is
    # judged.
    #
    # It bit for real on 2026-09-01: operations pointed LingBot's baseline at the
    # repository that carries the evaluation results (a display-only reference),
    # and **every miner who had already run `init` could no longer pay** -- with
    # an error telling them to retrain, which was a falsehood that costs a round.
    #
    # ⚠️ **The thing that really should be gated has not gone away**: a changed
    # *training starting point* does invalidate a training run. But that value
    # lives in `params.training` (`base_weights` / `checkpoint`), not in these
    # two columns. It is deliberately not added here on the way past: a gate that
    # refuses payment has to be thought through on its own, because refusing
    # wrongly costs a miner a whole round of training -- and the lesson of this
    # incident is precisely a gate watching the wrong field. Tracked in
    # `08-31-competition-is-the-only-source`.

    return Verdict(live=live, fee=live_fee, window=window)


def resolve_competition(
    settings: Settings, snapshot: Snapshot, now: datetime
) -> Verdict:
    """Ask the backend which season this is, and judge it. **Prints no verdict
    and asks nothing** -- see `confirm_payment` for the other half.

    Being unable to reach the backend is a **refusal**, not a warning: without
    it there is no way to say which season the money is going to, and money
    moving on an assumption is the failure this whole module exists to prevent.

    🔴 **The two halves are separate because gates run between them.** The
    layout gate has to judge the repository by *this season's* rule book, and
    the rule book is on the live row -- `miner.yaml` holds the copy taken at
    `init`, and a season that has since changed its base model is judged here by
    the old rules and by admission by the new ones, after the fee. It therefore
    cannot run before this. It equally must not run after the prompt: asking
    someone to confirm a payment that the next gate is about to refuse teaches
    them to answer the prompt without reading it.
    """
    try:
        live_rows = fetch_competitions(settings.backend_url).data
    except BackendError as exc:
        _refuse(
            f"Cannot reach the backend, so there is no way to confirm which "
            f"competition this fee would pay for. Nothing was paid.\n  {exc}"
        )

    try:
        return judge(snapshot, live_rows, now)
    except (PrecheckFailed, ConfigError) as exc:
        # A malformed snapshot lands here too (`ConfigError` out of `fee_of`).
        # It is the same event as far as the miner is concerned -- the fee was
        # not paid -- and it must not escape as a different exception type that
        # some caller might one day treat as "carry on".
        fail(f"Competition check failed; **nothing was paid**.\n   {exc}")
        raise PrecheckFailed(str(exc)) from exc


def confirm_payment(verdict: Verdict) -> None:
    """Say who is being paid and ask for a yes. Raises `PrecheckFailed`.

    🔴 **The last thing that happens before the money moves.** Every gate that
    can still refuse this submission has already run by the time this is called;
    what is on screen when the question is asked is the whole of what the miner
    is agreeing to.
    """
    _announce(verdict)
    if not _confirmed():
        # `_confirmed()` already explained the not-a-tty and closed-stdin cases.
        # A plain "n" is the one route here that has said nothing yet, and on a
        # path that spends money the miner is owed the confirmation that it did
        # not.
        _refuse("Cancelled at the confirmation prompt; nothing was paid.")


def _announce(verdict: Verdict) -> None:
    """Say who is being paid, for what, and how long is left.

    🔴 "check passed" on its own is not enough. A miner's last `init` may have
    picked a season that has since ended while a new one opened, and on their
    terminal those two situations look identical -- these lines are what makes
    them look different, printed while the money can still be kept.
    """
    live = verdict.live
    say("✅ competition check passed")
    say(f"   Submitting to: {live.label} ({_name(live)} · cid={live.id})")
    say(f"   Submissions:   {verdict.window}")
    destination = (
        "burned on the subnet (no address)"
        if verdict.kind == BURN
        else f"transfer to {verdict.fee.coldkey}"
    )
    say(f"   Entry fee:     {verdict.amount_tao} TAO -- {destination}")
    say(
        "   ⚠️  Passing this check does not guarantee the payment is accepted: "
        "the competition can still change in the seconds that follow."
    )
    say("")


def _confirmed() -> bool:
    """Ask before spending. Anything but an explicit yes is a no.

    Not a tty (CI, a wrapper script, a cron job) → **no**. There is no silent
    yes on a path that spends money; a script that wants to pay has to call the
    single-step commands and own that decision.
    """
    if not sys.stdin.isatty():
        fail(
            "Not running on a terminal, so the payment cannot be confirmed. "
            "Nothing was paid.\n"
            "   → run `openroboto submit` from a terminal"
        )
        return False
    try:
        return input("Pay now? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        # Input closed while the question was on screen -- a terminal that went
        # away, a wrapper that redirected stdin after opening a tty. Unanswered
        # is unanswered; letting this out as a traceback would be a stack trace
        # where the miner is owed a sentence saying their TAO is still theirs.
        fail("No answer given, so nothing was paid.")
        return False


def _find(snapshot: Snapshot, live_rows: Sequence[Competition]) -> Competition:
    """Locate the season by `(track, seq)`. See the module docstring."""
    for row in live_rows:
        if row.track == snapshot.track and row.seq == snapshot.seq:
            return row
    raise PrecheckFailed(
        f"The backend does not list {snapshot.name} ({snapshot.label}) among the "
        f"competitions taking submissions.\n"
        f"{_active_now(live_rows)}\n" + REFRESH_HINT
    )


def _active_now(rows: Sequence[Competition], exclude: Competition | None = None) -> str:
    """Name the seasons that *are* open, so the miner sees both sides at once."""
    skip = exclude.id if exclude is not None else None
    open_now = [row for row in rows if row.status == "active" and row.id != skip]
    if not open_now:
        return "  No competition is taking submissions right now."
    listed = ", ".join(f"{row.label} ({_name(row)})" for row in open_now)
    return f"  Taking submissions right now: {listed}"


def _name(live: Competition) -> str:
    return f"{live.track}/{live.seq}"


def _stamp(moment: datetime) -> str:
    return moment.strftime(_TIME_FORMAT)


def _span(delta: Any) -> str:
    """`3 days 4 hours`. Coarse on purpose -- minutes are noise at this scale,
    and a deadline is never that precise in practice."""
    hours, seconds = divmod(int(max(delta.total_seconds(), 0)), 3600)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days} days {hours} hours"
    if hours:
        return f"{hours} hours {seconds // 60} minutes"
    return f"{seconds // 60} minutes"


def _text(value: Any) -> str | None:
    """`None` and `""` both mean "not set", and they must not be told apart
    later: an empty repo name builds a URL that resolves."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
