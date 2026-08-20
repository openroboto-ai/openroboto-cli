"""`openroboto validator run` -- the loop that puts emissions on chain.

This path had **zero coverage** while being the money exit: an external
validator reads weights from the backend and writes them to the chain. Every
failure here is silent from the outside -- the process keeps running, the logs
look ordinary, and the emissions either go nowhere or go to the wrong place.

The cases are grouped by what each one costs when it breaks:

- `_is_success` misreading the SDK's return shape -> the validator believes a
  successful write failed and re-sends every cycle. Three processes doing that
  at once is a real incident this repo already survived.
- Sending an extrinsic with no positive weights -> the fee is burned for
  nothing, every cycle, forever.
- The loop exiting on an infrastructure hiccup -> a long-running process that
  is not running. Nobody restarts what nobody knows stopped.
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

import pytest

from openroboto.chain.weights import set_weights_on_chain
from openroboto.commands import validator


class FakeSubtensor:
    """Records the set_weights calls instead of making them."""

    def __init__(self, result: Any = True) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def set_weights(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.result


class Timelocked:
    """The commit-reveal return shape: `success` plus `error=None`.

    Not an `is_success` in sight. Reading only that attribute is what made the
    old validator treat confirmed reveals as failures.
    """

    is_success = False
    success = True
    error = None


class Rejected:
    is_success = False
    success = False
    error = "not enough stake"
    status_message = "not enough stake"


# ─────────────────────────────────────────────────────────────────────────────
# Reading the SDK's answer
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        pytest.param(True, True, id="bare-bool-the-oldest-shape"),
        pytest.param(
            type("Standard", (), {"is_success": True})(), True, id="is_success"
        ),
        pytest.param(Timelocked(), True, id="timelocked-success-plus-error-none"),
        pytest.param(Rejected(), False, id="rejected"),
        pytest.param(False, False, id="falsy"),
        pytest.param(None, False, id="none"),
    ],
)
def test_every_sdk_return_shape_is_read_correctly(result: Any, expected: bool) -> None:
    """🔴 Three shapes mean success, and missing one is expensive in one direction.

    Under commit-reveal the SDK returns `success=True, error=None` with
    `is_success` absent or False. Checking only `is_success` reads a confirmed
    write as a failure, so the validator re-sends -- every cycle, forever, and
    with several validators at once. That is not hypothetical; it is why the
    compatibility branch exists.

    The other direction has to hold too: a genuine rejection must read as
    failure, or the validator reports success while the chain has nothing.
    """
    subtensor = FakeSubtensor(result)

    got = set_weights_on_chain(subtensor, object(), 80, {"a": 1.0}, ["a"])

    assert got is expected
    assert len(subtensor.calls) == 1, "the call itself should happen either way"


def test_no_positive_weights_sends_no_transaction() -> None:
    """No weights means no extrinsic -- sending one only burns the fee.

    It also has to be visible: emissions going nowhere this round is not a
    routine event, so it is logged as a warning rather than passed over.
    """
    subtensor = FakeSubtensor()

    got = set_weights_on_chain(subtensor, object(), 80, {}, ["a", "b"])

    assert got is False
    assert subtensor.calls == [], "an extrinsic was sent with nothing in it"


def test_the_uids_and_weights_reach_the_chain_call_unchanged() -> None:
    """What normalisation computed is what gets sent.

    Asserting the call arguments, not just the return value: a wrapper that
    dropped or reordered a list would still return True.
    """
    subtensor = FakeSubtensor()

    set_weights_on_chain(subtensor, object(), 80, {"a": 0.7, "b": 0.3}, ["a", "b"])

    (call,) = subtensor.calls
    assert call["netuid"] == 80
    assert call["uids"] == [0, 1]
    assert call["weights"] == [45874, 19660]


# ─────────────────────────────────────────────────────────────────────────────
# One cycle of the loop
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def one_cycle(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Run `validator run --once` with the chain and backend stubbed out.

    Returns a callable taking the pieces each case wants to vary, so the setup
    is not repeated six times.
    """

    def run(
        *,
        weights: dict[str, float] | None = None,
        hotkeys: list[str] | None = None,
        fetch_error: Exception | None = None,
        settings: Any = None,
    ) -> tuple[int, FakeSubtensor]:
        subtensor = FakeSubtensor()

        def fake_fetch_weights(base_url: str, public_key: str = "") -> Any:
            if fetch_error is not None:
                raise fetch_error
            return weights if weights is not None else {"a": 1.0}

        cfg = settings if settings is not None else _settings()
        monkeypatch.setattr(validator.Settings, "load", staticmethod(lambda path: cfg))
        monkeypatch.setattr(validator, "get_subtensor", lambda network: subtensor)
        monkeypatch.setattr(validator, "open_wallet", lambda s: object())
        monkeypatch.setattr(validator, "fetch_weights", fake_fetch_weights)
        monkeypatch.setattr(
            validator,
            "get_metagraph",
            lambda netuid, network, sub=None: type(
                "MG", (), {"hotkeys": hotkeys if hotkeys is not None else ["a"]}
            )(),
        )
        args = argparse.Namespace(config="validator.yaml", once=True)
        return validator.run(args), subtensor

    return run


def _settings() -> Any:
    from openroboto.config import Settings

    return Settings(
        environment="mainnet",
        network="finney",
        netuid=80,
        weight_interval_min=60,
        control_json_url="",
        backend_public_key="k",
    )


def test_one_cycle_sets_weights_and_exits_zero(one_cycle: Any) -> None:
    """The happy path, end to end: fetch, normalise, send, return 0.

    `--once` exists for cron and for this test; without it the loop sleeps
    forever and nothing about the money path can be checked.
    """
    code, subtensor = one_cycle()

    assert code == 0
    assert len(subtensor.calls) == 1


def test_a_backend_hiccup_does_not_kill_the_process(
    one_cycle: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """🔴 Infrastructure failure is not a reason for a long-running process to exit.

    A validator that exited on one bad response would stay down until somebody
    noticed -- and what they would notice is emissions being wrong, days later.
    So the cycle is skipped and logged, and the process stays up.
    """
    from openroboto.backend_api import BackendError

    with caplog.at_level(logging.WARNING):
        code, subtensor = one_cycle(fetch_error=BackendError("502 from the backend"))

    assert code == 0, "the process exited on an infrastructure failure"
    assert subtensor.calls == []
    assert "skipping this cycle" in caplog.text


def test_an_unexpected_error_does_not_kill_it_either(
    one_cycle: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The catch-all matters more than the specific handler.

    Whatever the SDK throws next -- a new exception type, a bug in a
    dependency -- has to leave the loop running. An unhandled type here means
    the validator stops on the first surprise.
    """
    with caplog.at_level(logging.ERROR):
        code, _ = one_cycle(fetch_error=RuntimeError("something nobody predicted"))

    assert code == 0
    assert "loop error" in caplog.text


def test_an_empty_weight_table_skips_the_round_loudly(
    one_cycle: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """🔴 The backend returning nothing means no miner is paid this round.

    Production has been here: the response shape changed, every lookup missed,
    and `set_weights` was never called. No exception, no non-2xx -- just no
    emissions. It must not be silent.
    """
    with caplog.at_level(logging.WARNING):
        code, subtensor = one_cycle(weights={})

    assert code == 0
    assert subtensor.calls == [], "an empty table must not become an extrinsic"
    assert "no weights" in caplog.text


def test_hotkeys_missing_from_the_metagraph_are_dropped(one_cycle: Any) -> None:
    """A miner who deregistered is not on the metagraph and gets nothing; the
    rest split the whole allocation.

    Silent by nature -- the caller sees a valid weight table either way -- so it
    is pinned here rather than assumed.
    """
    _, subtensor = one_cycle(weights={"a": 1.0, "gone": 3.0}, hotkeys=["a", "b"])

    (call,) = subtensor.calls
    assert call["uids"] == [0]
    assert call["weights"] == [65535]


def test_a_rotated_public_key_is_picked_up_without_a_restart(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """control.json rotates `public_key` each round, and the loop follows it.

    Without this the key rotation is an outage: every fetch answers 401, the
    validator sets no weights, and the only way back is somebody restarting a
    process that looks perfectly healthy.

    The assertion is on the key actually used for the fetch, not on the log
    line -- logging the rotation while still sending the old key is exactly the
    bug this guards against.
    """
    seen_keys: list[str] = []

    def fake_fetch_weights(base_url: str, public_key: str = "") -> Any:
        seen_keys.append(public_key)
        return {"a": 1.0}

    cfg = _settings()
    cfg.control_json_url = "https://example.invalid/control.json"
    monkeypatch.setattr(validator.Settings, "load", staticmethod(lambda path: cfg))
    monkeypatch.setattr(validator, "get_subtensor", lambda network: FakeSubtensor())
    monkeypatch.setattr(validator, "open_wallet", lambda s: object())
    monkeypatch.setattr(validator, "fetch_weights", fake_fetch_weights)
    monkeypatch.setattr(
        validator,
        "get_metagraph",
        lambda netuid, network, sub=None: type("MG", (), {"hotkeys": ["a"]})(),
    )
    monkeypatch.setattr(validator, "apply_control", lambda settings, control: None)
    monkeypatch.setattr(
        validator,
        "fetch_control",
        lambda url, etag: type(
            "Fetched", (), {"etag": "e1", "control": {"public_key": "rotated-key"}}
        )(),
    )

    with caplog.at_level(logging.INFO):
        validator.run(argparse.Namespace(config="validator.yaml", once=True))

    assert seen_keys == ["rotated-key"], "the loop kept using the stale key"
    assert "public_key in control.json has been updated" in caplog.text


def test_without_once_the_loop_keeps_going(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long-running process has to actually keep running.

    `--once` is for cron and for the cases above; the deployed shape is the
    endless loop. If it ever returned after one cycle, the validator would stop
    setting weights while looking exactly like a healthy process that happens to
    be quiet -- and quiet is what a working validator looks like too.

    `time.sleep` is the only way out here, so it doubles as the probe: reaching
    it proves a second cycle was coming.
    """
    cycles = 0

    def stop_after_two(_seconds: float) -> None:
        nonlocal cycles
        cycles += 1
        if cycles >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(validator.Settings, "load", staticmethod(lambda p: _settings()))
    monkeypatch.setattr(validator, "get_subtensor", lambda network: FakeSubtensor())
    monkeypatch.setattr(validator, "open_wallet", lambda s: object())
    monkeypatch.setattr(validator, "fetch_weights", lambda url, key="": {"a": 1.0})
    monkeypatch.setattr(
        validator,
        "get_metagraph",
        lambda netuid, network, sub=None: type("MG", (), {"hotkeys": ["a"]})(),
    )
    monkeypatch.setattr(validator.time, "sleep", stop_after_two)

    with pytest.raises(KeyboardInterrupt):
        validator.run(argparse.Namespace(config="validator.yaml", once=False))

    assert cycles == 2, "the loop stopped on its own instead of continuing"
