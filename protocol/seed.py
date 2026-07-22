"""Public and reproducible OpenRoboto evaluation-seed derivation.

The seed binds three public values: the block hash containing a miner's
commitment, the subnet round number, and randomness from the drand quicknet
beacon. It does not select or expose held-out evaluation data.
"""

from __future__ import annotations

import hashlib

DRAND_API = "https://api.drand.sh"
DRAND_CHAIN_HASH = "8990e7a9aaed2ffed73dbd7092123d6f289930540d7651336225dc172e51b2ce"


def derive_seed(block_hash: str, round_num: int, drand_random: str) -> int:
    """Return the protocol's uint32 seed for the three public inputs."""
    seed_input = f"{block_hash}:{round_num}:{drand_random}".encode("utf-8")
    digest = hashlib.sha256(seed_input).digest()
    return int.from_bytes(digest[-4:], byteorder="big")


def drand_round_url(drand_round: int | str = "latest") -> str:
    """Return the public drand URL for a numbered round or ``latest``."""
    if drand_round != "latest" and (not isinstance(drand_round, int) or drand_round <= 0):
        raise ValueError("drand_round must be a positive integer or 'latest'")
    return f"{DRAND_API}/{DRAND_CHAIN_HASH}/public/{drand_round}"


def verify_seed(
    expected_seed: int,
    block_hash: str,
    round_num: int,
    drand_random: str,
) -> bool:
    """Check a published seed against the deterministic public formula."""
    return expected_seed == derive_seed(block_hash, round_num, drand_random)
