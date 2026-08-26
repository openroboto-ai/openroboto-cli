"""`openroboto.payment.transfer` -- the real track's entry fee leaves here.

Same stakes as `test_payment_burn.py` and the same shape of test: assert the
**call arguments and the failure modes**, because a wrapper that returned a
plausible receipt while sending the wrong amount, or to the wrong address, would
pass a return-value-only test and cost a miner 2 TAO with nothing to show.

What the backend does with what this sends is checked on its side
(`judge_transfer`), against a real testnet transaction. What is checked here is
that the extrinsic this composes is the one that check was written for.
"""

from __future__ import annotations

from typing import Any

import pytest

from openroboto.payment.burn import BurnReceipt
from openroboto.payment.transfer import TransferError, execute_transfer
from tests.test_payment_burn import FakeSubstrate, FakeSubtensor, FakeWallet, Receipt

DEST = "5Feqsy76Do37q6NaKkGnu4191g2gog85rLvNdx3iVHLKugtD"


def _transfer(substrate: FakeSubstrate, **kwargs: Any) -> BurnReceipt:
    kwargs.setdefault("dest_coldkey", DEST)
    kwargs.setdefault("amount_tao", 2.0)
    return execute_transfer(FakeSubtensor(substrate), FakeWallet(), **kwargs)


def test_it_composes_transfer_keep_alive_with_the_dest_and_rao_amount() -> None:
    """🔴 The three fields the backend reads back out of the block.

    `judge_transfer` compares the destination character by character and the
    amount in Rao. `transfer_keep_alive` rather than `transfer_allow_death`:
    the latter reaps a coldkey that drops below the existential deposit, so a
    miner paying their last 2 TAO would lose the remainder too.
    """
    substrate = FakeSubstrate()
    _transfer(substrate, amount_tao=2.0)

    assert substrate.call_module == "Balances"
    assert substrate.call_function == "transfer_keep_alive"
    assert substrate.call_params == {"dest": DEST, "value": 2_000_000_000}


def test_tao_is_truncated_to_rao_never_rounded() -> None:
    """The same conversion as the burn, and it has to stay the same expression.

    `0.0157 * 1e9` is `15699999.999999998` in binary floating point: `int()`
    gives 15699999, `round()` gives 15700000. The backend compares with `>=`
    against its own `int(amount_tao * 1e9)`, so the two must agree exactly --
    a "cleanup" here rejects a payment that is not refunded.
    """
    substrate = FakeSubstrate()
    _transfer(substrate, amount_tao=0.0157)
    assert substrate.call_params["value"] == 15_699_999


def test_the_coldkey_signs_not_the_hotkey() -> None:
    """🔴 `fee_payer_not_owner` is a rejection with the TAO already gone.

    The backend asks the chain who owns the announced hotkey and compares that
    against the extrinsic's signer. Signing with anything but the coldkey makes
    every payment from a correctly configured workspace fail that comparison.
    """
    substrate = FakeSubstrate()
    _transfer(substrate)
    assert substrate.signed_with == "COLDKEY"


def test_inclusion_is_awaited_and_finalization_is_not() -> None:
    """`bb` goes on chain, so the block has to be known before this returns --
    but waiting for finalization would hold the miner for a minute per submission
    while the backend only ever looks the transaction up by block."""
    substrate = FakeSubstrate()
    _transfer(substrate)
    assert substrate.submit_kwargs == {
        "wait_for_inclusion": True,
        "wait_for_finalization": False,
    }


def test_a_failed_extrinsic_raises_and_nothing_is_recorded() -> None:
    """The TAO has not moved, so the caller may retry -- which is only true if
    this raises instead of returning a receipt with a zero block."""
    substrate = FakeSubstrate(Receipt(is_success=False, error_message="Inability"))
    with pytest.raises(TransferError, match="Inability"):
        _transfer(substrate)


def test_an_unknown_block_raises_and_says_not_to_pay_again() -> None:
    """🔴 The one case where the money **has** moved.

    Returning a receipt with `block_number=0` would put a 0 on chain as `bb` and
    the backend would never find the transaction: `payment_tx_not_found`, final,
    unrefunded. The message has to stop the miner from paying a second time.
    """
    substrate = FakeSubstrate(Receipt(block_number=None, block_hash=None), head=None)
    with pytest.raises(TransferError, match="do not pay again"):
        _transfer(substrate)


def test_the_block_is_resolved_from_the_hash_when_the_receipt_omits_it() -> None:
    """`wait_for_finalization=False` leaves `block_number` unset on some SDK
    versions -- measured on testnet 313, not hypothetical."""
    substrate = FakeSubstrate(Receipt(block_number=None, block_hash="0x" + "c" * 64))
    assert _transfer(substrate).block_number == 777
