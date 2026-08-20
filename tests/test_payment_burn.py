"""`openroboto.payment.burn` -- the miner spends real TAO here.

This module had **28% coverage** while being the only place in the CLI that
moves money. Everything it gets wrong costs the miner the burn, with no refund:

- The TAO -> Rao conversion off by one unit -> the backend rejects the payment.
- The `limit` field wrong -> the extrinsic is refused, or accepts a price the
  miner did not agree to.
- `wait_for_inclusion` dropped -> `burn_block` is unknown, and `burn_block` is
  what the backend measures the commit window against.
- A block number guessed as 0 -> the submission is dead on arrival.

So the cases here assert the **call arguments and the failure modes**, not just
that a receipt comes back. A wrapper that returned a plausible `BurnReceipt`
while sending the wrong amount would pass a return-value-only test.
"""

from __future__ import annotations

from typing import Any

import pytest

from openroboto.payment.burn import (
    MAX_U64,
    BurnError,
    BurnReceipt,
    execute_stake_burn,
    resolve_burn_block,
    scan_recent_blocks_for_tx,
)

HOTKEY = "5Hot" + "0" * 44
OTHER_HOTKEY = "5Oth" + "0" * 44
TX = "0x" + "ab" * 32


class Receipt:
    """What `submit_extrinsic` hands back. Attributes vary by SDK version."""

    def __init__(
        self,
        *,
        is_success: bool = True,
        block_number: int | None = 4242,
        block_hash: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self.is_success = is_success
        self.block_number = block_number
        self.block_hash = block_hash
        self.error_message = error_message
        self.extrinsic_hash = TX


class FakeSubstrate:
    """Records what was composed and submitted instead of touching a chain."""

    def __init__(
        self,
        receipt: Receipt | None = None,
        head: int | None = None,
        blocks: dict[int, Any] | None = None,
    ) -> None:
        self.receipt = receipt if receipt is not None else Receipt()
        #: Head block height. `None` = `get_block()` answers nothing, which is
        #: what a node under load actually does.
        self.head = head
        self.blocks = blocks or {}
        self.call_params: dict[str, Any] = {}
        self.submit_kwargs: dict[str, Any] = {}
        self.signed_with: Any = None
        self.get_block_calls: list[int | None] = []

    def compose_call(
        self, call_module: str, call_function: str, call_params: dict[str, Any]
    ) -> str:
        self.call_module = call_module
        self.call_function = call_function
        self.call_params = call_params
        return "CALL"

    def create_signed_extrinsic(self, call: str, keypair: Any) -> str:
        self.signed_with = keypair
        return "EXTRINSIC"

    def submit_extrinsic(self, extrinsic: str, **kwargs: Any) -> Receipt:
        self.submit_kwargs = kwargs
        return self.receipt

    def get_block_number(self, block_hash: str) -> int:
        return 777

    def get_block(self, block_number: int | None = None) -> Any:
        self.get_block_calls.append(block_number)
        if block_number is None:
            return None if self.head is None else {"header": {"number": self.head}}
        return self.blocks.get(block_number)


class FakeSubtensor:
    def __init__(self, substrate: FakeSubstrate) -> None:
        self.substrate = substrate


class FakeWallet:
    coldkey = "COLDKEY"

    def __init__(self, hotkey_ss58: str = HOTKEY) -> None:
        self.hotkey = type("HK", (), {"ss58_address": hotkey_ss58})()


def _burn(substrate: FakeSubstrate, **kwargs: Any) -> BurnReceipt:
    return execute_stake_burn(
        FakeSubtensor(substrate), FakeWallet(), kwargs.pop("netuid", 80), **kwargs
    )


# ─────────────────────────────────────────────────────────────────────────────
# What goes into the extrinsic
# ─────────────────────────────────────────────────────────────────────────────


def test_tao_is_truncated_to_rao_never_rounded() -> None:
    """🔴 One Rao is the difference between paid and rejected, and there is no refund.

    `0.0157 TAO` is a value where the two disagree: `0.0157 * 1e9` is
    `15699999.999999998` in binary floating point, so `int()` gives 15699999
    and `round()` gives 15700000. The backend compares against the amount it
    computes the same way; a "cleanup" to `round()` or `Decimal` makes every
    such burn arrive one Rao short and be thrown away.

    The exact integer is asserted rather than the property, because the property
    ("close enough") is exactly what does not hold here.
    """
    substrate = FakeSubstrate()

    _burn(substrate, amount_tao=0.0157)

    assert substrate.call_params["amount"] == 15699999
    assert round(0.0157 * 1e9) == 15700000, "rounding really would differ"


def test_zero_limit_means_accept_the_market_price() -> None:
    """The default is max u64 -- not 0, and not omitted.

    Sending 0 is a limit order at zero: the chain refuses it. The miner asked to
    burn, so the intent is "at whatever the pool costs".
    """
    substrate = FakeSubstrate()

    _burn(substrate, amount_tao=0.1)

    assert substrate.call_params["limit"] == MAX_U64


def test_an_explicit_limit_is_passed_through_untouched() -> None:
    """A caller who names a price gets that price, not the max.

    Both branches matter: defaulting when one was given would spend more than
    the miner agreed to.
    """
    substrate = FakeSubstrate()

    _burn(substrate, amount_tao=0.1, limit_price_rao=123_456)

    assert substrate.call_params["limit"] == 123_456


def test_the_call_targets_the_subtensor_burn_extrinsic() -> None:
    """Module and function names are the contract with the chain; a typo in
    either is a runtime failure on the miner's machine, not ours."""
    substrate = FakeSubstrate()

    _burn(substrate, amount_tao=0.1, netuid=80)

    assert substrate.call_module == "SubtensorModule"
    assert substrate.call_function == "add_stake_burn"
    assert substrate.call_params["netuid"] == 80


def test_the_burn_is_signed_with_the_coldkey() -> None:
    """Stake operations are coldkey-signed. Signing with the hotkey would be
    refused by the chain -- and the hotkey is the one that lives on a hot
    machine, so the distinction is the whole point of having two."""
    substrate = FakeSubstrate()

    _burn(substrate, amount_tao=0.1)

    assert substrate.signed_with == "COLDKEY"


def test_the_stake_target_defaults_to_the_wallet_hotkey() -> None:
    substrate = FakeSubstrate()

    _burn(substrate, amount_tao=0.1)

    assert substrate.call_params["hotkey"] == HOTKEY


def test_an_explicit_hotkey_overrides_the_wallet() -> None:
    """`openroboto` supports burning on behalf of another hotkey; if the
    override were ignored the TAO would go to the wrong place and still be
    gone."""
    substrate = FakeSubstrate()

    _burn(substrate, amount_tao=0.1, hotkey_ss58=OTHER_HOTKEY)

    assert substrate.call_params["hotkey"] == OTHER_HOTKEY


def test_inclusion_is_awaited_and_finalization_is_not() -> None:
    """🔴 Both halves are deliberate.

    Without `wait_for_inclusion` there is no block number, and the block number
    is `bb` in the commitment -- the backend measures the burn-to-commit window
    (50 blocks) from it. Waiting for *finalization* instead would add a minute
    or more per submission for a guarantee nothing here needs.
    """
    substrate = FakeSubstrate()

    _burn(substrate, amount_tao=0.1)

    assert substrate.submit_kwargs == {
        "wait_for_inclusion": True,
        "wait_for_finalization": False,
    }


# ─────────────────────────────────────────────────────────────────────────────
# When it goes wrong
# ─────────────────────────────────────────────────────────────────────────────


def test_a_failed_extrinsic_raises_with_the_chain_message() -> None:
    """`BurnError` means the TAO has not moved, so the caller may retry.

    The chain's own message is carried through: "the burn failed" alone leaves
    the miner unable to tell "not enough balance" from "subnet does not exist".
    """
    substrate = FakeSubstrate(Receipt(is_success=False, error_message="Inability"))

    with pytest.raises(BurnError, match="Inability"):
        _burn(substrate, amount_tao=0.1)


def test_an_unresolvable_block_refuses_to_guess_and_says_do_not_burn_again() -> None:
    """🔴 The one case where the TAO **is** already gone.

    Returning `BurnReceipt(block_number=0)` would look like success and put a 0
    into the commitment, which the backend reads as a burn 5 million blocks
    stale -- rejected, not refunded. So it raises instead; and the message has
    to stop the miner from burning a second time, because the instinct on seeing
    an error is to retry.
    """
    substrate = FakeSubstrate(Receipt(block_number=None), head=None)

    with pytest.raises(BurnError) as excinfo:
        _burn(substrate, amount_tao=0.1)

    assert "do not burn again" in str(excinfo.value)
    assert TX[:16] in str(excinfo.value), "the miner needs the tx to check status"


def test_a_successful_burn_returns_both_commitment_fields() -> None:
    substrate = FakeSubstrate(Receipt(block_number=4242))

    receipt = _burn(substrate, amount_tao=0.1)

    assert receipt == BurnReceipt(tx_hash=TX, block_number=4242)


# ─────────────────────────────────────────────────────────────────────────────
# Finding the block -- three routes, tried in order
# ─────────────────────────────────────────────────────────────────────────────


def test_route_one_the_receipt_already_knows() -> None:
    substrate = FakeSubstrate()

    got = resolve_burn_block(FakeSubtensor(substrate), Receipt(block_number=99), TX)

    assert got == 99
    assert substrate.get_block_calls == [], "no lookup needed when it is right there"


def test_route_two_resolves_the_block_hash() -> None:
    """`wait_for_inclusion=True` does not guarantee `block_number` is set --
    which SDK version you are on decides. The hash is there either way."""
    substrate = FakeSubstrate()

    got = resolve_burn_block(
        FakeSubtensor(substrate), Receipt(block_number=None, block_hash="0xdead"), TX
    )

    assert got == 777


def test_route_two_failing_falls_through_to_the_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An SDK that raises on `get_block_number` must not end the burn.

    The TAO is already spent at this point, so every remaining route has to be
    tried before giving up.
    """
    substrate = FakeSubstrate(
        head=10,
        blocks={
            10: {"extrinsics": [{"extrinsic_hash": TX}]},
        },
    )
    monkeypatch.setattr(
        substrate, "get_block_number", lambda block_hash: 1 / 0, raising=True
    )

    got = resolve_burn_block(
        FakeSubtensor(substrate), Receipt(block_number=None, block_hash="0xdead"), TX
    )

    assert got == 10


# ─────────────────────────────────────────────────────────────────────────────
# Route three: scanning recent blocks
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scan backs off two seconds between attempts; the tests do not wait."""
    monkeypatch.setattr("openroboto.payment.burn.time.sleep", lambda _seconds: None)


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(TX, id="as-submitted"),
        pytest.param(TX.replace("0x", ""), id="bare-no-0x-prefix"),
    ],
)
def test_the_transaction_is_found_whichever_way_the_node_spells_it(
    stored: str,
) -> None:
    """🔴 Nodes disagree on the `0x` prefix, and a miss here is not a miss --
    it is a burn declared unresolvable and an exception raised over a string
    formatting difference."""
    substrate = FakeSubstrate(
        head=5,
        blocks={
            5: {"extrinsics": [{"extrinsic_hash": stored}]},
        },
    )

    assert scan_recent_blocks_for_tx(FakeSubtensor(substrate), TX) == 5


def test_the_scan_walks_backwards_and_stops_at_the_depth_limit() -> None:
    """Newest first: a burn submitted seconds ago is at the head, not 15 blocks
    back, and each block costs a round trip. The block just outside the window
    is left unfound rather than scanned forever."""
    substrate = FakeSubstrate(
        head=100,
        blocks={
            **{n: {"extrinsics": []} for n in range(86, 101)},
            85: {"extrinsics": [{"extrinsic_hash": TX}]},
        },
    )

    assert scan_recent_blocks_for_tx(FakeSubtensor(substrate), TX, max_retries=1) == 0
    assert substrate.get_block_calls[1:] == list(range(100, 85, -1))


def test_a_block_that_comes_back_empty_is_skipped_not_fatal() -> None:
    """Pruned or not-yet-available blocks are ordinary; the scan continues."""
    substrate = FakeSubstrate(
        head=3,
        blocks={
            3: None,
            2: {"extrinsics": [{"extrinsic_hash": TX}]},
        },
    )

    assert scan_recent_blocks_for_tx(FakeSubtensor(substrate), TX) == 2


def test_the_scan_retries_because_a_fresh_transaction_is_still_propagating() -> None:
    """The first look can legitimately come up empty -- the burn was submitted
    a moment ago. Giving up on attempt one would raise `BurnError` on a burn
    that is perfectly fine, and the miner would be told not to retry a payment
    that in fact succeeded."""
    attempts = {"n": 0}
    substrate = FakeSubstrate()

    def flaky_get_block(block_number: int | None = None) -> Any:
        if block_number is None:
            attempts["n"] += 1
            return {"header": {"number": 1}}
        if attempts["n"] < 3:
            return {"extrinsics": []}
        return {"extrinsics": [{"extrinsic_hash": TX}]}

    substrate.get_block = flaky_get_block  # type: ignore[method-assign]

    assert scan_recent_blocks_for_tx(FakeSubtensor(substrate), TX) == 1
    assert attempts["n"] == 3


def test_chain_jitter_during_the_scan_is_survived() -> None:
    """An RPC error on one attempt is not an answer. Letting it out would turn a
    node hiccup into "your burn is unresolvable"."""
    attempts = {"n": 0}
    substrate = FakeSubstrate()

    def sometimes_broken(block_number: int | None = None) -> Any:
        if block_number is None:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("connection reset")
            return {"header": {"number": 1}}
        return {"extrinsics": [{"extrinsic_hash": TX}]}

    substrate.get_block = sometimes_broken  # type: ignore[method-assign]

    assert scan_recent_blocks_for_tx(FakeSubtensor(substrate), TX) == 1


def test_giving_up_returns_zero_and_the_caller_turns_that_into_an_error() -> None:
    """🔴 Zero is this function's "not found", **not** a block number.

    It is only safe because `execute_stake_burn` checks it and raises. If a
    future caller passed it straight into a commitment, that submission would be
    rejected -- so the pairing is asserted here rather than left to a reader.
    """
    substrate = FakeSubstrate(head=None)

    assert scan_recent_blocks_for_tx(FakeSubtensor(substrate), TX, max_retries=2) == 0

    with pytest.raises(BurnError, match="do not burn again"):
        _burn(
            FakeSubstrate(Receipt(block_number=None), head=None),
            amount_tao=0.1,
        )
