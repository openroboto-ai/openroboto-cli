"""The local `normalize_weights` copy -- and a todo that expires by itself.

This is the last conversion before emissions land on chain: `{hotkey: share}`
into `(uid, u16)`. The backend's chain-writer and every validator running this
CLI each compute it, and if the two ever disagree the chain averages the
difference away -- no error, no alert, nothing to trace it back with.

openroboto-protocol 0.4.0 holds the shared copy (`openroboto_protocol.weights`)
but is not published yet, so the pin is still 0.3.0 and this repo keeps its own.

**Why this is a test and not a comment.** The last todo of this exact shape --
`burn_block_window`, "a three-line change once protocol 0.3.0 is published" --
sat unread while 0.3.0 went out the next day, leaving a known red-line
violation open for another day. So this fails the moment the protocol package
gains the module, and says what to do. It expires on its own instead of waiting
to be remembered.

⚠️ No `skipif` here: AGENTS.md §4 bans skips in this suite, and CI audits for
them. A todo that switches a test off is the failure mode this file exists to
avoid, so it branches in the open instead.
"""

from __future__ import annotations

import importlib.util

import pytest

from openroboto.chain import weights as local_weights

PROTOCOL_HAS_WEIGHTS = (
    importlib.util.find_spec("openroboto_protocol.weights") is not None
)


def test_weight_normalisation_is_the_protocol_copy_once_one_exists() -> None:
    """While the copy exists, pin what it produces. When it stops needing to
    exist, fail and say so.

    Three counter-intuitive details, and changing any one on one side only makes
    the two u16 tables differ: `weight > 0` is strictly greater, the share is
    computed before scaling, and `int()` truncates.

    The input is on-chain snapshot 122: the burn address holds 0.9, and
    `0.9 * 65535` is exactly `58981.5`. Truncation gives 58981, rounding gives
    58982 -- `round()` would rewrite a value already settled on chain.

    🔴 When this fails because the protocol package ships `weights`, that is a
    todo coming due, not a regression. Three things to do:

    1. pin `openroboto-protocol` to the version carrying `weights`;
    2. delete `normalize_weights` / `NormalizedWeights` from
       `src/openroboto/chain/weights.py`, importing both from
       `openroboto_protocol.weights`;
    3. delete this file.

    Red line #1 exists because the package's only claim is that both sides can
    be *shown* to install the same thing. While a copy remains that claim is
    literally false -- the two agree today because nobody has touched them, not
    because anything holds them together.
    """
    if PROTOCOL_HAS_WEIGHTS:
        pytest.fail(
            "openroboto_protocol.weights is available -- drop the local copy and "
            "import it instead (steps in this test's docstring)"
        )

    result = local_weights.normalize_weights(
        {"burn": 0.9, "a": 0.07, "b": 0.02, "c": 0.01}, ["burn", "a", "b", "c"]
    )

    assert result.uids == [0, 1, 2, 3]
    assert result.weights == [58981, 4587, 1310, 655]
    assert sum(result.weights) == 65533, "the shortfall is what the chain accepts"
