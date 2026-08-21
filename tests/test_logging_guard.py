"""bittensor must not be able to silence this package's logs.

Split out rather than folded into an existing file because it patches a method
on `logging.Logger`, and the fixture that puts it back has to be unmissable.
"""

from __future__ import annotations

import logging

import pytest

from openroboto import logging as or_logging


@pytest.fixture(autouse=True)
def restore_setlevel() -> object:
    """Put `Logger.setLevel` back, whatever the test did to it.

    The guard patches a class method and records that it did. Leaking either
    the patch or the flag into the rest of the suite would make unrelated
    tests depend on execution order.
    """
    original = logging.Logger.setLevel
    installed = or_logging._level_guard_installed
    yield
    logging.Logger.setLevel = original  # type: ignore[method-assign]
    or_logging._level_guard_installed = installed


def _bittensor_would_do_this() -> None:
    """What `LoggingMachine.before_enable_default` does: walk every logger that
    exists and raise the ones it does not own to CRITICAL."""
    for name in list(logging.root.manager.loggerDict):
        if not name.startswith("bittensor"):
            logging.getLogger(name).setLevel(logging.CRITICAL)


def test_our_loggers_survive_bittensors_sweep(tmp_path: object) -> None:
    """🔴 Measured on 2026-08-21 with openroboto==0.1.0a1: constructing a
    Subtensor took `openroboto.chain.weights` from INFO (20) to CRITICAL (50).

    For `validator run` that removes everything: which uids are being written,
    whether the extrinsic landed, and the reason when it refuses to send. A
    validator doing nothing and saying nothing is indistinguishable from one
    that is fine.
    """
    or_logging.setup_logger("openroboto.testguard", log_dir=str(tmp_path))

    _bittensor_would_do_this()

    assert logging.getLogger("openroboto.testguard").level <= logging.WARNING


def test_bittensors_own_loggers_are_left_alone(tmp_path: object) -> None:
    """The guard pushes back on the overreach, not on bittensor configuring
    itself. Silencing its own logger is its business."""
    or_logging.setup_logger("openroboto.testguard2", log_dir=str(tmp_path))

    logging.getLogger("bittensor.something").setLevel(logging.CRITICAL)

    assert logging.getLogger("bittensor.something").level == logging.CRITICAL


def test_lowering_our_level_still_works(tmp_path: object) -> None:
    """`--log-level DEBUG` has to keep working -- the guard blocks raising into
    silence, not any change at all."""
    or_logging.setup_logger("openroboto.testguard3", log_dir=str(tmp_path))

    logging.getLogger("openroboto.testguard3").setLevel(logging.DEBUG)

    assert logging.getLogger("openroboto.testguard3").level == logging.DEBUG
