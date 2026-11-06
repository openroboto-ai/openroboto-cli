"""The contributor key: derivation, storage permissions, signing.

The three things that cost a miner money if they are wrong:

- the `contributor_id` derivation must equal the backend's, or the CLI looks up
  an identity that does not exist and reports "not registered" for one that is;
- the key file must be `0600`, or the private key is readable by every local
  user for as long as it exists;
- signing must produce lowercase 128-character hex, the only shape the backend
  accepts.

The signatures are verified here with `cryptography` directly, not through this
module's own helper, so a bug in `verify()` cannot make `sign()` look right.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openroboto.contributor import (
    DEFAULT_KEY_FILE,
    ContributorKey,
    ContributorKeyError,
    derive_contributor_id,
    generate_key,
    key_file,
    load_key,
    save_key,
)

#: seed = bytes(range(32)), generated once and pinned. The expected public key
#: and id below come from the backend's rule:
#: sha256("openroboto:contributor:v1\\0" || public_key).
SEED = bytes(range(32)).hex()
PUBLIC_KEY = "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
CONTRIBUTOR_ID = "07ec98a481181f8494f831703121d1e9041ba5e7bee012c2920cd735e3a65584"


def _key() -> ContributorKey:
    return ContributorKey(public_key=PUBLIC_KEY, private_key=bytes.fromhex(SEED))


def _verify(public_key_hex: str, message: bytes, signature_hex: str) -> None:
    """Independent check -- cryptography, not `ContributorKey.verify`."""
    private = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(SEED))
    expected_public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    assert expected_public.hex() == public_key_hex
    private.public_key().verify(bytes.fromhex(signature_hex), message)


# ─── derivation ─────────────────────────────────────────────────────────────


def test_contributor_id_matches_the_backend_rule() -> None:
    assert derive_contributor_id(PUBLIC_KEY) == CONTRIBUTOR_ID
    assert _key().contributor_id == CONTRIBUTOR_ID


def test_derivation_rejects_non_hex_and_wrong_length() -> None:
    with pytest.raises(ContributorKeyError):
        derive_contributor_id("not-hex")
    with pytest.raises(ContributorKeyError):
        derive_contributor_id("ab" * 31)


# ─── signing ────────────────────────────────────────────────────────────────


def test_signature_is_lowercase_hex_and_verifies() -> None:
    signature = _key().sign(b"openroboto:contributor:v1\nkind=register\n")
    assert len(signature) == 128
    assert signature == signature.lower()
    _verify(PUBLIC_KEY, b"openroboto:contributor:v1\nkind=register\n", signature)


def test_verify_rejects_a_different_message() -> None:
    key = _key()
    signature = key.sign(b"bind")
    assert key.verify(b"bind", signature)
    assert not key.verify(b"unbind", signature)


# ─── storage ────────────────────────────────────────────────────────────────


def test_key_file_is_created_private(tmp_path: Path) -> None:
    path = tmp_path / "keys" / "contributor.key"
    saved = save_key(_key(), path)

    assert saved == path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # The directory is created private too: a 0700 dir keeps another user from
    # even listing the file name.
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_save_refuses_to_clobber_an_identity(tmp_path: Path) -> None:
    path = tmp_path / "contributor.key"
    save_key(_key(), path)
    with pytest.raises(ContributorKeyError) as excinfo:
        save_key(generate_key(), path)
    assert "--force" in str(excinfo.value)
    assert "admin-arbitrated" in str(excinfo.value).lower() or (
        "admin" in str(excinfo.value).lower()
    )


def test_save_with_overwrite_replaces_the_key(tmp_path: Path) -> None:
    path = tmp_path / "contributor.key"
    save_key(_key(), path)
    replacement = generate_key()
    save_key(replacement, path, overwrite=True)
    assert load_key(path).public_key == replacement.public_key


def test_load_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "contributor.key"
    save_key(_key(), path)
    loaded = load_key(path)
    assert loaded.public_key == PUBLIC_KEY
    assert loaded.private_key == bytes.fromhex(SEED)
    assert loaded.path == path


def test_load_refuses_a_group_readable_key(tmp_path: Path) -> None:
    path = tmp_path / "contributor.key"
    save_key(_key(), path)
    os.chmod(path, 0o644)
    with pytest.raises(ContributorKeyError) as excinfo:
        load_key(path)
    assert "chmod 600" in str(excinfo.value)


def test_load_reports_a_missing_key_with_the_next_commands(
    tmp_path: Path,
) -> None:
    with pytest.raises(ContributorKeyError) as excinfo:
        load_key(tmp_path / "nope.key")
    message = str(excinfo.value)
    assert "identity keygen" in message
    assert "identity register" in message


def test_load_rejects_a_mismatched_pair(tmp_path: Path) -> None:
    path = tmp_path / "contributor.key"
    payload = {
        "version": 1,
        "kind": "ed25519",
        "public_key": "ab" * 32,
        "private_key": SEED,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(ContributorKeyError) as excinfo:
        load_key(path)
    assert "inconsistent" in str(excinfo.value)


def test_migrated_from_and_created_at_survive_a_round_trip(
    tmp_path: Path,
) -> None:
    path = tmp_path / "contributor.key"
    key = ContributorKey(
        public_key=PUBLIC_KEY,
        private_key=bytes.fromhex(SEED),
        created_at="2026-10-29T00:00:00Z",
        migrated_from=(CONTRIBUTOR_ID,),
    )
    save_key(key, path)
    loaded = load_key(path)
    assert loaded.created_at == "2026-10-29T00:00:00Z"
    assert loaded.migrated_from == (CONTRIBUTOR_ID,)


def test_private_key_never_appears_in_repr() -> None:
    assert SEED not in repr(_key())


def test_key_file_defaults_outside_the_workspace() -> None:
    assert ".openroboto" in str(DEFAULT_KEY_FILE)
    assert DEFAULT_KEY_FILE.name == "contributor.key"
    assert key_file("~/custom.key") == Path.home() / "custom.key"
