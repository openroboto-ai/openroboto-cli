"""The contributor's own Ed25519 key: generate it, store it, sign with it.

A task author's earnings must follow the **person**, not the hotkey that
happened to submit a package. Hotkeys are replaceable -- rotated, sold,
deregistered -- so the backend derives a persistent `contributor_id` from an
Ed25519 public key instead, and every mutation (register / bind / unbind /
migrate) is proved by a signature from that key.

## The private key never leaves this machine, and never reaches a log

The key file lives under `~/.openroboto/` by default -- **outside the
workspace**, so it cannot be committed by accident and is not uploaded with a
model or a task package. It is created with mode `0600`; loading refuses a file
that is readable by anyone else rather than pretending it is safe.

No function here logs, prints or returns the private key. `ContributorKey` is
the only object holding it, and its `__repr__` is deliberately the default
dataclass one with the field excluded -- an exception traceback that includes
the object must not include the bytes.

## The message is fetched, never assembled here

Signatures cover a canonical text the **backend rebuilds** from `kind` plus a
fixed field order (`GET /api/v1/contributors/message`). Both sides assembling
their own copy is how "signature invalid" becomes the only error a miner ever
sees; the CLI therefore signs the exact bytes the backend returned, and this
module does not have a second spelling of that format.

## `contributor_id` is derived, not chosen

    contributor_id = sha256("openroboto:contributor:v1\\0" || public_key)

`derive_contributor_id()` is only used to *display* an id and to look the
identity up before a paid submission. The id of a new identity is still
recomputed by the server; a request body cannot pick its own primary key. When
`openroboto-protocol` publishes this rule, this function moves there and the
local copy goes away (red line #1: one implementation).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

logger = logging.getLogger(__name__)

#: Message domain, byte-identical to the backend's `domain/contributor.py`.
DOMAIN = "openroboto:contributor:v1"

#: Bumped only if the on-disk key file gains an incompatible field. The key
#: itself (32-byte seed) is what the backend cares about, not this number.
KEY_FILE_VERSION = 1

#: Where `keygen` writes when `--key-file` is not given. Under the home
#: directory, **not** under the workspace: a key that lands next to `miner.yaml`
#: is one `git add .` away from being published.
DEFAULT_KEY_DIR = Path.home() / ".openroboto"
DEFAULT_KEY_FILE = DEFAULT_KEY_DIR / "contributor.key"

#: A group- or world-readable private key is not a style issue. Loading stops
#: and asks for `chmod 600`.
_PRIVATE_MODES = 0o077


class ContributorKeyError(Exception):
    """The contributor key is missing, unreadable, or not safe to use.

    Written for the miner: which path, what was expected, how to fix it.
    """


@dataclass(frozen=True)
class ContributorKey:
    """One Ed25519 key pair, with the file it came from.

    `private_key` is the 32-byte seed and is excluded from `repr` so that an
    exception's locals cannot leak it into a log or a crash report.
    """

    public_key: str
    private_key: bytes = field(repr=False)
    path: Path | None = None
    #: Preserved when a key file is rewritten after a migration, so the file
    #: does not claim it was created a second ago.
    created_at: str = ""
    #: Identities this key inherited through `migrate`, oldest first.
    #:
    #: 🔴 **Load-bearing for the pre-payment gate.** A migration does not move
    #: hotkey bindings: they stay on the old identity, and admission resolves
    #: them to the new one along `merged_into`. Without this list, `submit`
    #: would look only at the new id, see no bindings, and refuse a submission
    #: the backend would accept -- after telling the miner to bind a hotkey
    #: the backend then reports as already bound elsewhere.
    migrated_from: tuple[str, ...] = ()

    @property
    def contributor_id(self) -> str:
        return derive_contributor_id(self.public_key)

    def sign(self, message: bytes) -> str:
        """Sign `message`, return the 64-byte signature as lowercase hex.

        The backend accepts exactly this: 128 hex characters, lowercase.
        """
        key = Ed25519PrivateKey.from_private_bytes(self.private_key)
        return key.sign(message).hex()

    def verify(self, message: bytes, signature_hex: str) -> bool:
        """Local self-check. The backend's verdict is still the only one that
        counts; this exists so a broken key file is caught before a request."""
        try:
            public = Ed25519PublicKey.from_public_bytes(bytes.fromhex(self.public_key))
            public.verify(bytes.fromhex(signature_hex), message)
        except (InvalidSignature, ValueError):
            return False
        return True

    def to_json(self) -> str:
        return (
            json.dumps(
                {
                    "version": KEY_FILE_VERSION,
                    "kind": "ed25519",
                    "public_key": self.public_key,
                    "private_key": self.private_key.hex(),
                    "created_at": self.created_at
                    or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "migrated_from": list(self.migrated_from),
                },
                indent=2,
            )
            + "\n"
        )


def key_file(value: str = "") -> Path:
    """`--key-file` or the default. `~` is expanded here, not by the shell."""
    return Path(value).expanduser() if value else DEFAULT_KEY_FILE


def generate_key() -> ContributorKey:
    """A fresh key pair. Nothing is written until `save()` is called."""
    private = Ed25519PrivateKey.generate()
    seed = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return ContributorKey(public_key=public.hex(), private_key=seed)


def save_key(key: ContributorKey, path: Path, *, overwrite: bool = False) -> Path:
    """Write the key with mode `0600`, atomically, refusing to clobber one.

    🔴 **Overwriting an existing key loses the identity it belongs to.** The
    old key is the only proof that identity has; without it the earnings can
    only be moved through an admin-arbitrated migration. So `keygen` needs
    `--force` for this, and the message says what is being given up.

    The temporary file is opened with mode `0600` **before** the private key is
    written into it, so there is no window in which the bytes exist on disk
    readable by another user.
    """
    path = path.expanduser()
    if path.exists() and not overwrite:
        raise ContributorKeyError(
            f"{path} already holds a contributor key. **Do not replace it** -- "
            f"that file is the only proof of the identity it belongs to, and "
            f"replacing it means the earnings can only be recovered through an "
            f"admin-arbitrated migration.\n"
            f"   → use the existing key (`openroboto identity show`), or pass "
            f"`--key-file <another path>` to create a second, separate identity\n"
            f"   → only if you really mean to abandon this identity: "
            f"`openroboto identity keygen --force`"
        )

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=".contributor-", suffix=".tmp"
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(key.to_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:  # pragma: no cover -- already renamed/removed
            pass
        raise

    return path


def load_key(path: Path | None = None) -> ContributorKey:
    """Read a key file. Fails loudly if it is missing, malformed, or exposed.

    🔴 **A group- or world-readable key is refused, not warned about.** A
    warning is read once and then scrolls away; the private key stays readable
    by every local user for as long as the file exists.
    """
    path = (path or DEFAULT_KEY_FILE).expanduser()
    if not path.exists():
        raise ContributorKeyError(
            f"No contributor key at {path}. Register a contributor identity "
            f"first -- it is what makes an authored task's earnings follow you "
            f"rather than the hotkey that submitted it.\n"
            f"   → `openroboto identity keygen` creates one (mode 0600) and "
            f"prints the backup warning\n"
            f"   → then `openroboto identity register --reward-address <ss58>`"
        )
    _refuse_public_permissions(path)

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContributorKeyError(
            f"{path} is not a readable contributor key file: {exc}\n"
            f"   → restore it from your backup (it is the only proof of this "
            f"identity), or `--key-file <path>` to use a different one"
        ) from exc

    if not isinstance(raw, dict):
        raise ContributorKeyError(f"{path} does not contain a key object")
    if raw.get("version") != KEY_FILE_VERSION:
        raise ContributorKeyError(
            f"{path} is version {raw.get('version')!r}, this CLI understands "
            f"{KEY_FILE_VERSION}. → upgrade the CLI (`pip install -U openroboto`)"
        )

    public_key = str(raw.get("public_key", ""))
    private_hex = str(raw.get("private_key", ""))
    try:
        seed = bytes.fromhex(private_hex)
    except ValueError as exc:
        raise ContributorKeyError(f"{path} has a malformed private key") from exc
    if len(seed) != 32 or len(public_key) != 64:
        raise ContributorKeyError(
            f"{path} is not an Ed25519 key (expected a 32-byte seed and a "
            f"64-hex public key)"
        )

    migrated_from = raw.get("migrated_from", [])
    if not isinstance(migrated_from, list) or not all(
        isinstance(item, str) for item in migrated_from
    ):
        raise ContributorKeyError(
            f"{path} has a malformed `migrated_from` list; restore it from backup"
        )

    key = ContributorKey(
        public_key=public_key,
        private_key=seed,
        path=path,
        created_at=str(raw.get("created_at", "")),
        migrated_from=tuple(migrated_from),
    )
    # The two halves must describe the same key. A mismatched pair would sign
    # with a key the backend does not know, and every request would answer
    # "signature invalid" -- which looks like a server problem, not a file
    # problem.
    derived = _public_key_of(seed)
    if derived != public_key:
        raise ContributorKeyError(
            f"{path} is inconsistent: its private key belongs to public key "
            f"{derived}, but the file says {public_key}.\n"
            f"   → restore the file from backup; do not edit either half by hand"
        )
    return key


def derive_contributor_id(public_key_hex: str) -> str:
    """`sha256("openroboto:contributor:v1\\0" || public_key)`, lowercase hex.

    Used for display and for the pre-payment lookup. **The server still
    recomputes it on register** -- this value is never accepted as input.
    """
    try:
        raw = bytes.fromhex(public_key_hex)
    except ValueError as exc:
        raise ContributorKeyError(f"public key is not hex: {public_key_hex!r}") from exc
    if len(raw) != 32:
        raise ContributorKeyError(f"public key must be 32 bytes, got {len(raw)}")
    return hashlib.sha256(DOMAIN.encode("utf-8") + b"\x00" + raw).hexdigest()


def key_file_note(key: ContributorKey) -> str:
    """The one line every command that touched the key prints: where it is."""
    return f"key file: {key.path or DEFAULT_KEY_FILE}"


def _public_key_of(seed: bytes) -> str:
    private = Ed25519PrivateKey.from_private_bytes(seed)
    return (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )


def _refuse_public_permissions(path: Path) -> None:
    if os.name != "posix":  # pragma: no cover -- miners are on Linux/macOS
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:  # pragma: no cover -- exists() just answered
        raise ContributorKeyError(f"Cannot stat {path}: {exc}") from exc
    if mode & _PRIVATE_MODES:
        raise ContributorKeyError(
            f"{path} is mode {mode:04o}; a contributor private key must be "
            f"readable by you alone.\n"
            f"   → `chmod 600 {path}` and run the command again"
        )
    logger.debug("contributor key loaded from %s", path)
