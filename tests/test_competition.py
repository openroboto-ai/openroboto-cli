"""The gate that stands between a miner and a fee they cannot get back.

Every case here is one way of paying for nothing: paying into a season that has
closed, paying a fee that changed after `init`, paying an address that has not
been published, or -- the one that costs the most and looks the most normal --
paying the season the miner *thinks* they are in while a new one has opened.

The two rules the rest of the file is built around:

1. **existence before equality.** `params.fee.coldkey` is `null` in the real
   track's row today. Compared first, `null == null` reads as "unchanged" and
   the transfer is then addressed to nobody;
2. **nothing may pass silently.** Not an unreachable backend, not a
   non-interactive shell, not an empty answer at the prompt.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml
from openroboto_protocol.schemas import Competition

from openroboto import competition as competition_module
from openroboto.commands.init import render_section
from openroboto.competition import (
    PrecheckFailed,
    fee_of,
    judge,
    load_snapshot,
    precheck,
    render_window,
)
from openroboto.config import ConfigError, Settings

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
COLDKEY = "5ERSxczp6BsY1gAW3i4KxB1xD8oqoh79tXaYEsyhNcjXd55L"
OTHER_COLDKEY = "5Feqsy76yEhcnhpqR6NnrwSqZ2VsL5X6sZDdiPBBrbUhUnGD"


def _competition(**overrides: Any) -> Competition:
    """One row as the endpoint serves it. The real track's first season."""
    row: dict[str, Any] = {
        "id": 3,
        "track": "real",
        "seq": 1,
        "label": "xArm 6 第一届",
        "adapter": "real_xarm6",
        "status": "active",
        "submit_closes_at": NOW + timedelta(days=3, hours=4),
        "base_repo": None,
        "base_revision": None,
        "params": {
            "fee": {"kind": "transfer", "amount_tao": 2.0, "coldkey": COLDKEY},
            "strategy_template": "simple",
            "format": {"profile": "lingbot", "cameras": ["camera_top"]},
        },
    }
    return Competition.model_validate(row | overrides)


def _snapshot(live: Competition) -> Any:
    """The snapshot `init` would have written for this row.

    Deliberately the round trip and not a hand-built dict: it is the same YAML
    the miner ends up with, so a key that survives the write but not the read
    fails here rather than in front of a miner about to pay.
    """
    settings = Settings.from_mapping(
        yaml.safe_load(render_section(live, "https://api.openroboto.ai"))
    )
    snapshot = load_snapshot(settings)
    assert snapshot is not None
    return snapshot


# ─── the snapshot ────────────────────────────────────────────


def test_a_config_from_before_competitions_has_no_snapshot() -> None:
    """`None`, not an empty snapshot: those configs take the old path whole."""
    assert load_snapshot(Settings()) is None


def test_the_empty_template_section_is_not_a_snapshot() -> None:
    """The shipped template carries the section with its values empty. Read as a
    snapshot it would send every fresh workspace looking for a season `/0`."""
    settings = Settings.from_mapping({"competition": {"adapter": "", "params": {}}})
    assert load_snapshot(settings) is None


def test_the_snapshot_survives_the_round_trip_through_miner_yaml() -> None:
    live = _competition()
    snapshot = _snapshot(live)
    assert snapshot.track == "real"
    assert snapshot.seq == 1
    assert snapshot.adapter == "real_xarm6"
    assert snapshot.label == "xArm 6 第一届"
    # `params` goes to disk verbatim -- a season adding a key must not need a
    # release of this package.
    assert snapshot.params == live.params


def test_the_base_model_is_read_from_the_top_level_columns() -> None:
    """Not from `params.base`. Written there they are simply absent when read
    back, and "the base model has not changed" then passes without comparing."""
    live = _competition(base_repo="robbyant/lingbot", base_revision="c0ffee")
    assert _snapshot(live).base == ("robbyant/lingbot", "c0ffee")


def test_an_unset_base_reads_as_none_not_as_an_empty_string() -> None:
    """`""` builds a URL that resolves; `None` cannot be mistaken for one."""
    assert _snapshot(_competition()).base == (None, None)


# ─── the fee ─────────────────────────────────────────────────


def test_the_fee_is_read_out_of_the_competition() -> None:
    fee = _snapshot(_competition()).fee()
    assert (fee.kind, fee.amount_tao, fee.coldkey) == ("transfer", 2.0, COLDKEY)


def test_a_missing_fee_block_refuses_rather_than_defaulting() -> None:
    with pytest.raises(ConfigError, match="entry fee is missing"):
        fee_of({}, where="miner.yaml")


def test_a_missing_amount_refuses_and_names_no_number() -> None:
    with pytest.raises(ConfigError) as raised:
        fee_of({"fee": {"kind": "burn"}}, where="miner.yaml")
    assert "None" in str(raised.value)
    assert "TAO" not in str(raised.value)


def test_a_payment_kind_this_client_does_not_know_refuses() -> None:
    """`free_period` is a payment *status*, not a way to pay. Accepted here it
    would look like a handled case forever, while the backend never sends it."""
    with pytest.raises(ConfigError, match="pip install -U"):
        fee_of({"fee": {"kind": "free_period", "amount_tao": 1}}, where="x")


def test_a_burn_needs_no_address() -> None:
    fee = fee_of({"fee": {"kind": "burn", "amount_tao": 0.1}}, where="x")
    assert fee.coldkey is None


# ─── the submission window ───────────────────────────────────


def test_the_window_says_how_much_is_left() -> None:
    line = render_window(_competition(), NOW)
    assert "3 days" in line
    assert "4 hours" in line
    assert "2026-09-10" not in line or "closes" in line


def test_a_closed_window_says_so_without_a_negative_number() -> None:
    line = render_window(_competition(submit_closes_at=NOW - timedelta(days=1)), NOW)
    assert line.startswith("closed")
    assert "-" not in line.replace("2026-08-24", "")


def test_no_closing_date_is_not_a_deadline_and_not_a_date() -> None:
    """`None` means this boundary is not checked. Inventing a date for it tells
    a miner to hurry for a reason that does not exist."""
    line = render_window(_competition(submit_closes_at=None), NOW)
    assert "no closing date" in line
    assert "2026" not in line


def test_a_window_that_has_not_opened_says_when_it_does() -> None:
    line = render_window(_competition(submit_opens_at=NOW + timedelta(hours=5)), NOW)
    assert "not open yet" in line
    assert "5 hours" in line


# ─── the verdict ─────────────────────────────────────────────


def test_an_unchanged_season_passes_and_carries_the_live_id() -> None:
    live = _competition(id=17)
    verdict = judge(_snapshot(_competition()), [live], NOW)
    # 🔴 The id that goes on chain is the one just resolved, not the one that
    # was sitting in miner.yaml: `id` is local to one database.
    assert verdict.cid == 17
    assert verdict.amount_tao == 2.0
    assert verdict.kind == "transfer"


def test_a_season_the_backend_no_longer_lists_is_refused() -> None:
    other = _competition(id=9, track="sim", seq=2, label="LingBot-VLA 2.0")
    with pytest.raises(PrecheckFailed) as raised:
        judge(_snapshot(_competition()), [other], NOW)
    assert "init --refresh" in str(raised.value)
    assert "LingBot-VLA 2.0" in str(raised.value)


def test_an_archived_season_names_the_one_that_is_open() -> None:
    """The two hardest situations to tell apart on a terminal: the season you
    picked ended, and a new one opened. Both labels have to be in the message."""
    mine = _competition(status="archived")
    opened = _competition(id=9, track="real", seq=2, label="xArm 6 第二届")
    with pytest.raises(PrecheckFailed) as raised:
        judge(_snapshot(_competition()), [mine, opened], NOW)
    assert "xArm 6 第一届" not in str(raised.value)  # named by track/seq
    assert "real/1" in str(raised.value)
    assert "xArm 6 第二届" in str(raised.value)


def test_a_closed_window_refuses_and_points_at_the_next_season() -> None:
    mine = _competition(submit_closes_at=NOW - timedelta(hours=1))
    opened = _competition(id=9, seq=2, label="xArm 6 第二届")
    with pytest.raises(PrecheckFailed) as raised:
        judge(_snapshot(_competition()), [mine, opened], NOW)
    assert "closed" in str(raised.value)
    assert "xArm 6 第二届" in str(raised.value)


def test_a_window_that_has_not_opened_refuses() -> None:
    live = _competition(submit_opens_at=NOW + timedelta(days=1))
    with pytest.raises(PrecheckFailed, match="not open"):
        judge(_snapshot(_competition()), [live], NOW)


def test_a_season_with_no_boundaries_at_all_passes() -> None:
    """Two `null` instants are a configuration, not missing data."""
    live = _competition(submit_opens_at=None, submit_closes_at=None)
    verdict = judge(_snapshot(live), [live], NOW)
    assert "no closing date" in verdict.window


# ─── the address, which is the whole point ───────────────────


def test_an_unpublished_address_refuses_even_though_both_sides_are_null() -> None:
    """🔴 The state of the real track's row **today**, not a corner case.

    Checked for equality first, `null == null` passes as "unchanged" and two
    TAO leave for an address nobody holds the key to.
    """
    live = _competition(
        params={"fee": {"kind": "transfer", "amount_tao": 2.0, "coldkey": None}}
    )
    with pytest.raises(PrecheckFailed) as raised:
        judge(_snapshot(live), [live], NOW)
    assert "has not published its collection address" in str(raised.value)


def test_a_burn_is_not_stopped_by_the_missing_address() -> None:
    """Burning needs no address. Without this branch the simulation seasons are
    blocked by the gate written for the real one."""
    live = _competition(
        track="sim",
        seq=2,
        adapter="sim_lingbot",
        params={"fee": {"kind": "burn", "amount_tao": 0.1, "coldkey": None}},
    )
    assert judge(_snapshot(live), [live], NOW).kind == "burn"


def test_a_changed_address_shows_both_values() -> None:
    changed = _competition(
        params={
            "fee": {"kind": "transfer", "amount_tao": 2.0, "coldkey": OTHER_COLDKEY}
        }
    )
    with pytest.raises(PrecheckFailed) as raised:
        judge(_snapshot(_competition()), [changed], NOW)
    assert COLDKEY in str(raised.value)
    assert OTHER_COLDKEY in str(raised.value)


def test_the_address_is_checked_before_the_amount() -> None:
    """Ordered by what it costs to get wrong: the wrong address loses the whole
    fee, the wrong amount loses it too but is at least visible on chain."""
    changed = _competition(
        params={
            "fee": {"kind": "transfer", "amount_tao": 9.0, "coldkey": OTHER_COLDKEY}
        }
    )
    with pytest.raises(PrecheckFailed) as raised:
        judge(_snapshot(_competition()), [changed], NOW)
    assert "address has changed" in str(raised.value)


def test_a_changed_amount_shows_both_numbers() -> None:
    changed = _competition(
        params={"fee": {"kind": "transfer", "amount_tao": 9.0, "coldkey": COLDKEY}}
    )
    with pytest.raises(PrecheckFailed) as raised:
        judge(_snapshot(_competition()), [changed], NOW)
    assert "2.0 TAO" in str(raised.value)
    assert "9.0 TAO" in str(raised.value)


def test_a_changed_base_model_says_to_train_again() -> None:
    trained_on = _competition(base_repo="robbyant/lingbot", base_revision="c0ffee")
    now_serving = _competition(base_repo="robbyant/lingbot", base_revision="deadbee")
    with pytest.raises(PrecheckFailed) as raised:
        judge(_snapshot(trained_on), [now_serving], NOW)
    assert "MIGRATION" in str(raised.value)
    assert "c0ffee" in str(raised.value)


# ─── the whole gate, including the prompt ────────────────────


def _backend(monkeypatch: pytest.MonkeyPatch, rows: list[Competition]) -> list[str]:
    calls: list[str] = []

    def _fetch(base_url: str, **kwargs: Any) -> Any:
        calls.append(base_url)
        return type("_Envelope", (), {"data": rows})()

    monkeypatch.setattr(competition_module, "fetch_competitions", _fetch)
    return calls


def _answer(monkeypatch: pytest.MonkeyPatch, reply: str, tty: bool = True) -> None:
    monkeypatch.setattr(competition_module.sys.stdin, "isatty", lambda: tty)
    monkeypatch.setattr("builtins.input", lambda *a: reply)


def test_passing_says_which_season_how_long_and_how_much(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 "check passed" on its own is not enough, and this is the assertion
    that says so: five facts, and the sentence that it is not a guarantee."""
    live = _competition()
    _backend(monkeypatch, [live])
    _answer(monkeypatch, "y")

    verdict = precheck(Settings(), _snapshot(live), NOW)

    printed = capsys.readouterr().out
    assert "xArm 6 第一届" in printed  # which season
    assert "cid=3" in printed  # and its id
    assert "3 days 4 hours" in printed  # how long is left
    assert "2.0 TAO" in printed  # how much
    assert COLDKEY in printed  # to whom, in full
    assert "does not guarantee" in printed  # and that it is not a promise
    assert verdict.cid == 3


def test_an_unreachable_backend_refuses_to_pay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opposite of `doctor`, deliberately: `doctor` costs nothing, this
    spends. Fail-closed is the default on the paying path."""

    def _fetch(base_url: str, **kwargs: Any) -> Any:
        raise competition_module.BackendError("connection refused", retryable=True)

    monkeypatch.setattr(competition_module, "fetch_competitions", _fetch)

    with pytest.raises(PrecheckFailed) as raised:
        precheck(Settings(), _snapshot(_competition()), NOW)
    assert "Nothing was paid" in str(raised.value)


def test_a_non_interactive_shell_is_a_no(monkeypatch: pytest.MonkeyPatch) -> None:
    """A script, CI, cron: nobody is there to agree, so nobody agreed."""
    live = _competition()
    _backend(monkeypatch, [live])
    _answer(monkeypatch, "y", tty=False)

    with pytest.raises(PrecheckFailed):
        precheck(Settings(), _snapshot(live), NOW)


@pytest.mark.parametrize("reply", ["", "n", "no", "Y no wait"])
def test_anything_but_yes_is_a_no(monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
    live = _competition()
    _backend(monkeypatch, [live])
    _answer(monkeypatch, reply)

    with pytest.raises(PrecheckFailed):
        precheck(Settings(), _snapshot(live), NOW)


def test_a_terminal_that_goes_away_mid_question_is_not_a_yes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured during the end-to-end run: stdin closing at the prompt used to
    surface as an `EOFError` traceback, which is a stack trace where the miner
    is owed one sentence saying their TAO is still theirs."""
    live = _competition()
    _backend(monkeypatch, [live])
    monkeypatch.setattr(competition_module.sys.stdin, "isatty", lambda: True)

    def _closed(*args: Any) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _closed)

    with pytest.raises(PrecheckFailed):
        precheck(Settings(), _snapshot(live), NOW)


def test_a_malformed_snapshot_cannot_escape_as_a_different_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `ConfigError` out of the fee parser is the same event to a miner --
    nothing was paid -- and must not leave by a door some caller treats as
    recoverable."""
    live = _competition()
    _backend(monkeypatch, [live])
    settings = Settings.from_mapping(
        {"competition": {"track": "real", "seq": 1, "params": {}}}
    )
    snapshot = load_snapshot(settings)
    assert snapshot is not None

    with pytest.raises(PrecheckFailed):
        precheck(settings, snapshot, NOW)


def test_the_fee_never_comes_from_the_subnet_wide_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`control.json`'s `payment` block is one number for the whole subnet.
    Comparing a season's fee against it is not a comparison."""
    live = _competition()
    _backend(monkeypatch, [live])
    _answer(monkeypatch, "y")

    settings = Settings()
    settings.burn_rate_tao = 0.1
    assert precheck(settings, _snapshot(live), NOW).amount_tao == 2.0


def test_an_unreachable_backend_says_so_instead_of_exiting_silently(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 Refusing to pay must say why. Exit code 1 alone is not a message.

    `PrecheckFailed` promises its caller that the reason is already on screen,
    and `commands/submit.py` is written against that promise: it catches and
    returns 1 without printing anything itself. A bare `raise` therefore
    produces exit 1 with an empty stderr -- on the money path, and while
    discarding the diagnosis the backend sent back.

    Measured before the fix: `openroboto submit` against a stopped backend
    exited 1 and wrote zero bytes to stderr. The miner had nothing to act on.
    """
    from openroboto.backend_api import BackendError

    def _boom(base_url: str, **kwargs: Any) -> Any:
        raise BackendError("connection refused", retryable=True)

    monkeypatch.setattr(competition_module, "fetch_competitions", _boom)
    live = _competition()

    with pytest.raises(PrecheckFailed):
        precheck(Settings(), _snapshot(live), NOW)

    err = capsys.readouterr().err
    assert err.strip(), "refused to pay and said nothing at all"
    assert "Nothing was paid" in err
    # The backend's own words survive: without them the miner cannot tell a
    # stopped service from a wrong URL.
    assert "connection refused" in err


def test_declining_at_the_prompt_confirms_that_nothing_was_paid(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Typing `n` is the one refusal route that used to print nothing.

    The two other ways `_confirmed()` returns False -- not a terminal, stdin
    closed -- each say so themselves. A plain `n` did not, and the miner knows
    they declined without knowing whether the decline landed before or after
    the transfer. On a path that spends money, that sentence is owed.
    """
    live = _competition()
    _backend(monkeypatch, [live])
    _answer(monkeypatch, "n")

    with pytest.raises(PrecheckFailed):
        precheck(Settings(), _snapshot(live), NOW)

    assert "nothing was paid" in capsys.readouterr().err.lower()
