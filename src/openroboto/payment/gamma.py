"""Spend a miner-owned Gamma deposit and verify the exact payment event.

A signed transaction is journaled before broadcast. An uncertain broadcast is
reconciled by its transaction hash; it is never replaced with a second spend.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from openroboto_protocol.gamma_payment import (
    GAMMA_UNITS_PER_GAMMA,
    GammaSpend,
    gamma_amount_units,
    validate_gamma_payment,
)

from openroboto.payment.burn import BurnReceipt

GAMMA_TX_LIFETIME_BLOCKS = 64
"""Bound the signed payment lifetime so uncertain broadcasts can be reconciled."""


class GammaPaymentError(Exception):
    """A Gamma payment stopped; the message distinguishes unpaid from unknown."""


class GammaPaymentNotSpent(GammaPaymentError):
    """The chain proves this direct spend failed or was skipped without payment."""


def check_gamma_deposit(
    subtensor: Any, *, netuid: int, hotkey: str, coldkey: str, amount_units: int
) -> int:
    """Return the balance after checking ownership, spender, and spend allowance."""
    current_block = subtensor.get_current_block()
    block_hash = subtensor.substrate.get_block_hash(current_block)
    deposit = subtensor.substrate.query(
        module="SubtensorModule",
        storage_function="GammaDeposits",
        params=[netuid, hotkey],
        block_hash=block_hash,
    ).value
    setup = (
        f"btcli gamma mint --netuid {netuid} --hotkey {hotkey} "
        f"--amount-tao <TAO_TO_TOP_UP> --spender {coldkey}"
    )
    if deposit is None:
        raise GammaPaymentError(
            f"No Gamma deposit for {hotkey} on subnet {netuid}. Nothing was paid.\n"
            f"  → fund your deposit with `{setup}`; review the quote before minting"
        )
    if deposit["coldkey"] != coldkey:
        raise GammaPaymentError(
            "This Gamma deposit belongs to another coldkey. Nothing was paid. "
            "Use the wallet that owns the submission hotkey."
        )
    if deposit["spender"] != coldkey:
        raise GammaPaymentError(
            "Your Gamma deposit does not authorize your own coldkey as spender. "
            "Nothing was paid. This CLI signs payments locally.\n"
            f"  → `btcli gamma set-spender --netuid {netuid} --hotkey {hotkey} "
            f"--spender {coldkey}`"
        )
    balance = int(deposit["balance"])
    if balance < amount_units:
        raise GammaPaymentError(
            f"Gamma balance {balance / GAMMA_UNITS_PER_GAMMA:g} is below the "
            f"submission fee {amount_units / GAMMA_UNITS_PER_GAMMA:g}. "
            f"Nothing was paid.\n  → top up with `{setup}`"
        )
    cap = deposit["daily_spend_cap"]
    if cap is not None:
        available, at_block = cap["bucket"]
        allowance = min(
            int(cap["cap"]),
            int(available)
            + int(cap["cap"]) * max(0, current_block - int(at_block)) // 7200,
        )
        if allowance < amount_units:
            raise GammaPaymentError(
                "The Gamma deposit's current daily allowance is below this fee. "
                "Nothing was paid. Wait for it to refill or review the cap:\n"
                f"  → `btcli gamma set-cap --netuid {netuid} --hotkey {hotkey} "
                "--daily-spend-cap-gamma <YOUR_DAILY_LIMIT>`"
            )
    return balance


def _spends(events: Sequence[Mapping[str, Any]]) -> list[GammaSpend]:
    spends = []
    for record in events:
        event = record["event"]
        if event["module_id"] != "SubtensorModule" or event["event_id"] != "GammaSpent":
            continue
        attrs = event["attributes"]
        reference = attrs["reference"]
        if not isinstance(reference, str):
            reference = "0x" + bytes(reference).hex()
        spends.append(
            GammaSpend(
                netuid=int(attrs["netuid"]),
                coldkey=str(attrs["coldkey"]),
                hotkey=str(attrs["hotkey"]),
                spender=str(attrs["spender"]),
                amount_units=int(attrs["amount"]),
                reference=reference,
            )
        )
    return spends


def _checked_receipt(
    receipt: Any,
    *,
    netuid: int,
    hotkey: str,
    coldkey: str,
    amount_units: int,
    reference: str,
    tx_hash: str,
    block_number: int,
) -> BurnReceipt:
    if not receipt.is_success:
        raise GammaPaymentNotSpent(
            f"Gamma transaction {tx_hash} failed: {receipt.error_message}. "
            "No Gamma payment was confirmed. Check the transaction before retrying."
        )
    events = receipt.triggered_events
    spends = _spends(events)
    for record in events:
        event = record["event"]
        if (
            event["module_id"] == "SubtensorModule"
            and event["event_id"] == "GammaSpendSkipped"
        ):
            attrs = event["attributes"]
            skipped_ref = attrs["reference"]
            if not isinstance(skipped_ref, str):
                skipped_ref = "0x" + bytes(skipped_ref).hex()
            if (
                int(attrs["netuid"]) == netuid
                and str(attrs["hotkey"]) == hotkey
                and skipped_ref == reference
                and not spends
            ):
                raise GammaPaymentNotSpent(
                    f"Gamma transaction {tx_hash} skipped this spend: "
                    f"{attrs['reason']}. No Gamma was paid. Fix the deposit "
                    "balance or allowance, then rerun submit."
                )
    try:
        validate_gamma_payment(
            spends,
            netuid=netuid,
            hotkey=hotkey,
            coldkey=coldkey,
            reference=reference,
            expected_units=amount_units,
        )
    except ValueError as exc:
        raise GammaPaymentError(
            f"Gamma transaction {tx_hash} has no matching payment: {exc}. "
            "A successful transaction can contain a skipped spend. "
            "No submission was announced; inspect the transaction before retrying."
        ) from exc
    return BurnReceipt(tx_hash=tx_hash, block_number=block_number)


def execute_gamma_spend(
    *,
    subtensor: Any,
    wallet: Any,
    netuid: int,
    hotkey: str,
    amount_gamma: str,
    reference: str,
    before_submit: Callable[[str, int], None],
) -> BurnReceipt:
    """Journal the signed hash before broadcast, then require its GammaSpent event."""
    amount_units = gamma_amount_units(amount_gamma)
    coldkey = str(wallet.coldkeypub.ss58_address)
    check_gamma_deposit(
        subtensor,
        netuid=netuid,
        hotkey=hotkey,
        coldkey=coldkey,
        amount_units=amount_units,
    )
    call = subtensor.substrate.compose_call(
        call_module="SubtensorModule",
        call_function="spend_gamma",
        call_params={"netuid": netuid, "spends": [[(hotkey, amount_units, reference)]]},
    )
    start_block = subtensor.get_current_block()
    extrinsic = subtensor.substrate.create_signed_extrinsic(
        call=call,
        keypair=wallet.coldkey,
        era={"period": GAMMA_TX_LIFETIME_BLOCKS, "current": start_block},
    )
    tx_hash = "0x" + extrinsic.extrinsic_hash.hex()
    try:
        before_submit(tx_hash, start_block)
    except OSError as exc:
        raise GammaPaymentError(
            f"Cannot save the signed Gamma payment before broadcast: {exc}. "
            "Nothing was sent or paid; fix workspace storage and retry submit."
        ) from exc
    try:
        receipt = subtensor.substrate.submit_extrinsic(
            extrinsic, wait_for_inclusion=True, wait_for_finalization=True
        )
        block_number = int(subtensor.substrate.get_block_number(receipt.block_hash))
        return _checked_receipt(
            receipt,
            netuid=netuid,
            hotkey=hotkey,
            coldkey=coldkey,
            amount_units=amount_units,
            reference=reference,
            tx_hash=tx_hash,
            block_number=block_number,
        )
    except GammaPaymentError:
        raise
    except Exception as exc:
        raise GammaPaymentError(
            f"Gamma transaction {tx_hash} has an unknown result: {exc}. "
            "Its signed hash is saved. Rerun `openroboto submit` to reconcile "
            "this payment; do not send another spend or delete its journal."
        ) from exc


def recover_gamma_spend(subtensor: Any, pending: Mapping[str, Any]) -> BurnReceipt:
    """Find the journaled transaction without signing or broadcasting anything."""
    tx_hash = str(pending["tx_hash"])
    start = int(pending["start_block"])
    death = start + GAMMA_TX_LIFETIME_BLOCKS
    try:
        finalized_hash = subtensor.substrate.get_chain_finalised_head()
        finalized = int(subtensor.substrate.get_block_number(finalized_hash))
        for block_number in range(start, min(finalized, death - 1) + 1):
            block = subtensor.substrate.get_block(block_number=block_number)
            if block is None:
                raise GammaPaymentError(
                    f"Cannot read block {block_number} while reconciling {tx_hash}. "
                    "Retry submit when the chain is available; no new payment was sent."
                )
            for extrinsic in block["extrinsics"]:
                if "0x" + extrinsic.extrinsic_hash.hex() != tx_hash:
                    continue
                block_hash = subtensor.substrate.get_block_hash(block_number)
                receipt = subtensor.substrate.retrieve_extrinsic_by_hash(
                    block_hash, tx_hash
                )
                return _checked_receipt(
                    receipt,
                    netuid=int(pending["netuid"]),
                    hotkey=str(pending["hotkey"]),
                    coldkey=str(pending["coldkey"]),
                    amount_units=gamma_amount_units(pending["amount_gamma"]),
                    reference=str(pending["reference"]),
                    tx_hash=tx_hash,
                    block_number=block_number,
                )
    except GammaPaymentError:
        raise
    except Exception as exc:
        raise GammaPaymentError(
            f"Cannot reconcile Gamma transaction {tx_hash}: {exc}. "
            "Retry submit when the chain is available; no new payment was sent."
        ) from exc
    if finalized >= death:
        raise GammaPaymentNotSpent(
            f"Gamma transaction {tx_hash} expired without inclusion. Every block "
            "in its mortal era is finalized and contains no such transaction; "
            "no Gamma was paid. Rerun submit to recheck and confirm a new payment."
        )
    raise GammaPaymentError(
        f"Gamma transaction {tx_hash} was not found in its payment window. "
        "No replacement payment was sent. Wait and rerun submit, or ask the "
        "operator to reconcile this transaction before authorizing a new payment."
    )
