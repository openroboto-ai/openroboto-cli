"""`openroboto identity` -- the persistent contributor identity behind a task.

The backend refuses a task submission from a hotkey that is not bound to a
contributor identity (`contributor_unbound`), **after the entry fee has been
paid and without refunding it**. This command is the whole repair path:

    openroboto identity keygen
    openroboto identity register --reward-address <your ss58>
    openroboto identity bind

Three properties are load-bearing:

- **the private key stays on this machine** (`~/.openroboto/contributor.key`,
  mode 0600) and is never printed, logged or uploaded;
- **the text that gets signed is fetched from the backend**
  (`GET /api/v1/contributors/message`), never re-assembled here;
- **`bind` carries two signatures** -- the contributor Ed25519 key and the
  hotkey's sr25519 key. The hotkey comes from the wallet named in
  `miner.yaml`; it is not generated here, because it is the identity that owns
  the on-chain submission.

`unbind` and `migrate` exist so a miner is not stuck when a hotkey is retired
or a signing key is lost (the latter only for the key that still works -- a
lost key goes through operator arbitration).
"""

from __future__ import annotations

import argparse
from dataclasses import replace

from openroboto.backend_api import (
    BackendError,
    bind_contributor_hotkey,
    fetch_contributor,
    fetch_contributor_message,
    migrate_contributor,
    miner_key,
    register_contributor,
    revoke_contributor_hotkey,
)
from openroboto.chain import hotkey_signature as sign_hotkey
from openroboto.chain import open_wallet
from openroboto.config import ConfigError, Settings
from openroboto.console import fail, say
from openroboto.contributor import (
    ContributorKey,
    generate_key,
    key_file,
    load_key,
    save_key,
)

#: Every subcommand that talks to the backend reads the same config, for the
#: backend URL -- and `bind` also for the wallet. Keeping the default identical
#: to every other command means `openroboto identity bind` works in the
#: workspace the miner already has.
DEFAULT_CONFIG = "miner.yaml"


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "identity", help="The contributor identity: key, registration, hotkeys"
    )
    inner = parser.add_subparsers(dest="identity_command", required=True)

    keygen = inner.add_parser(
        "keygen",
        help="Create the Ed25519 contributor key (~/.openroboto, mode 0600)",
    )
    _key_file_arg(keygen)
    keygen.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing key. **The old identity can then only be "
        "recovered through an admin-arbitrated migration**",
    )
    keygen.set_defaults(handler=run_keygen)

    show = inner.add_parser(
        "show", help="Print the local identity and its status on the backend"
    )
    _key_file_arg(show)
    show.add_argument("--config", default=DEFAULT_CONFIG)
    show.set_defaults(handler=run_show)

    register = inner.add_parser(
        "register", help="Register the identity; sign `register` with its key"
    )
    _key_file_arg(register)
    register.add_argument("--config", default=DEFAULT_CONFIG)
    register.add_argument(
        "--reward-address",
        required=True,
        help="SS58 address where this author's earnings are paid (usually your "
        "coldkey). Changing it later requires a signed migration",
    )
    register.set_defaults(handler=run_register)

    bind = inner.add_parser(
        "bind",
        help="Bind a hotkey: the contributor key AND the hotkey's sr25519 key sign",
    )
    _key_file_arg(bind)
    bind.add_argument("--config", default=DEFAULT_CONFIG)
    bind.add_argument(
        "--hotkey",
        default="",
        help="SS58 to bind; defaults to subnet.hotkey_ss58 in miner.yaml. It "
        "must be the hotkey in the wallet that config names -- only its private "
        "key can produce the second signature",
    )
    bind.set_defaults(handler=run_bind)

    unbind = inner.add_parser(
        "unbind", help="Revoke a hotkey binding; signed by the contributor key"
    )
    _key_file_arg(unbind)
    unbind.add_argument("--config", default=DEFAULT_CONFIG)
    unbind.add_argument(
        "--hotkey",
        default="",
        help="SS58 to unbind; defaults to subnet.hotkey_ss58 in miner.yaml",
    )
    unbind.set_defaults(handler=run_unbind)

    migrate = inner.add_parser(
        "migrate",
        help="Rotate the signing key; **the old key signs**, naming the new key "
        "and reward address",
    )
    _key_file_arg(migrate)
    migrate.add_argument("--config", default=DEFAULT_CONFIG)
    migrate.add_argument(
        "--new-key-file",
        required=True,
        help="Key file holding the new Ed25519 key (create it first with "
        "`openroboto identity keygen --key-file <path>`)",
    )
    migrate.add_argument(
        "--reward-address",
        required=True,
        help="SS58 address the migrated identity will be paid at",
    )
    migrate.set_defaults(handler=run_migrate)


def _key_file_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--key-file",
        default="",
        help="Contributor key file (default: ~/.openroboto/contributor.key). "
        "Keep it outside the workspace",
    )


# ─── keygen / show ──────────────────────────────────────────────────────────


def run_keygen(args: argparse.Namespace) -> int:
    path = key_file(args.key_file)
    key = generate_key()
    saved = save_key(key, path, overwrite=args.force)

    say("🔑 contributor key created")
    say(f"   key file:       {saved} (mode 0600)")
    say(f"   public key:     {key.public_key}")
    say(f"   contributor id: {key.contributor_id}")
    say("")
    say(
        "⚠️  Back this file up now, somewhere other than this machine. It is "
        "the only proof of this identity."
    )
    say(
        "   If it is lost or deleted, this identity cannot sign anything again; "
        "its earnings can only be moved by an operator-arbitrated migration "
        "(`POST /api/v1/admin/contributors/<id>/migrate`)."
    )
    say("   Do not copy it into the workspace, a repository, or a chat window.")
    say("")
    say("   → next: `openroboto identity register --reward-address <your ss58>`")
    return 0


def run_show(args: argparse.Namespace) -> int:
    key = load_key(key_file(args.key_file))
    settings = _settings(args)

    say(f"contributor id: {key.contributor_id}")
    say(f"public key:     {key.public_key}")
    say(f"key file:       {key.path} (mode 0600)")
    say(f"backend:        {settings.backend_url}")

    detail = fetch_contributor(settings.backend_url, key.contributor_id)
    say(f"status:         {detail.status}")
    say(f"reward address: {detail.reward_address}")
    if detail.status == "migrated":
        say(
            f"⚠️  this identity was migrated to {detail.merged_into}. The new "
            f"identity controls the earnings; use the key that received it."
        )
        return 0

    # Bindings stay on the identity that created them. After a migration the
    # current id can have none while the inherited ones still authorise this
    # hotkey, and admission resolves them forward -- so this has to list them
    # too, or `show` would say "none" about a working setup.
    bound: list[tuple[str, str]] = [
        (key.contributor_id, binding.hotkey) for binding in detail.bindings
    ]
    for identity_id in key.migrated_from:
        try:
            inherited = fetch_contributor(settings.backend_url, identity_id)
        except BackendError:
            continue
        bound.extend((identity_id, b.hotkey) for b in inherited.bindings)

    if bound:
        say("bound hotkeys:")
        for identity_id, hotkey in bound:
            suffix = (
                ""
                if identity_id == key.contributor_id
                else (f" (inherited from {identity_id[:12]}...)")
            )
            say(f"  - {hotkey}{suffix}")
    else:
        say(
            "bound hotkeys:  none -- a task submission from any hotkey will be "
            "refused (`contributor_unbound`)."
        )
        say("   → `openroboto identity bind`")
    return 0


# ─── register / bind ────────────────────────────────────────────────────────


def run_register(args: argparse.Namespace) -> int:
    key = load_key(key_file(args.key_file))
    settings = _settings(args)

    _say_signing("register", key)
    text = fetch_contributor_message(
        settings.backend_url,
        "register",
        public_key=key.public_key,
        reward_address=args.reward_address,
    )
    contributor = register_contributor(
        settings.backend_url,
        public_key=key.public_key,
        reward_address=args.reward_address,
        signature=key.sign(text.encode("utf-8")),
        miner_key=_miner_key(settings),
    )

    # The server recomputes the id; a disagreement means this CLI and the
    # backend disagree about the derivation rule. Say so instead of silently
    # storing an id the next command cannot find.
    if contributor.contributor_id != key.contributor_id:
        fail(
            f"registered, but the backend derived contributor id "
            f"{contributor.contributor_id} while this CLI derives "
            f"{key.contributor_id} from the same public key -- the two sides "
            f"disagree about the identity rule.\n"
            f"   → upgrade the CLI (`pip install -U openroboto`) and run "
            f"`openroboto identity show` before binding anything"
        )
        return 1

    say(f"✅ registered contributor {contributor.contributor_id}")
    say(f"   reward address: {contributor.reward_address}")
    say(f"   status:         {contributor.status}")
    say("")
    say(
        "   → next: `openroboto identity bind` (binds the hotkey in miner.yaml; "
        "a task submitted from an unbound hotkey is refused after the fee)"
    )
    return 0


def run_bind(args: argparse.Namespace) -> int:
    key = load_key(key_file(args.key_file))
    settings = _settings(args)

    wallet = open_wallet(settings)
    wallet_hotkey = str(wallet.hotkey.ss58_address)
    hotkey = args.hotkey or settings.hotkey_ss58 or wallet_hotkey

    # 🔴 Only the wallet's own hotkey private key can produce the second
    # signature. Binding an address whose key is elsewhere would be refused by
    # the backend anyway, but with a 401 that does not say which of the two
    # addresses the miner meant.
    if wallet_hotkey != hotkey:
        fail(
            f"The wallet in {args.config} holds hotkey {wallet_hotkey}, but "
            f"{hotkey} was named for binding. Only that hotkey's own private "
            f"key can sign the bind message.\n"
            f"   → put {hotkey} in the wallet this config names "
            f"(`subnet.hotkey`), or run `openroboto identity bind` from the "
            f"workspace whose wallet holds it"
        )
        return 1

    _say_signing("bind", key)
    text = fetch_contributor_message(
        settings.backend_url,
        "bind",
        contributor_id=key.contributor_id,
        hotkey=hotkey,
    )
    message = text.encode("utf-8")
    binding = bind_contributor_hotkey(
        settings.backend_url,
        key.contributor_id,
        hotkey=hotkey,
        signature=key.sign(message),
        # Second signature over the **same bytes**: proof that the hotkey is
        # yours. Without it anyone could attach someone else's hotkey and
        # collect that author's earnings.
        hotkey_signature=sign_hotkey(wallet, message),
        miner_key=_miner_key(settings),
    )

    say(f"✅ hotkey {binding.hotkey} bound to contributor {key.contributor_id}")
    say(
        "   Task submissions from this hotkey now carry the identity; the "
        "backend will no longer refuse them as `contributor_unbound`."
    )
    return 0


def run_unbind(args: argparse.Namespace) -> int:
    key = load_key(key_file(args.key_file))
    settings = _settings(args)

    hotkey = args.hotkey or settings.hotkey_ss58
    if not hotkey:
        raise ConfigError(
            "Don't know which hotkey to unbind -- pass `--hotkey <SS58>`, or set "
            "subnet.hotkey_ss58 in miner.yaml"
        )

    _say_signing("unbind", key)
    text = fetch_contributor_message(
        settings.backend_url,
        "unbind",
        contributor_id=key.contributor_id,
        hotkey=hotkey,
    )
    detail = revoke_contributor_hotkey(
        settings.backend_url,
        key.contributor_id,
        hotkey=hotkey,
        signature=key.sign(text.encode("utf-8")),
        miner_key=_miner_key(settings),
    )

    say(f"✅ hotkey {hotkey} unbound from contributor {key.contributor_id}")
    if detail.bindings:
        say("   still bound: " + ", ".join(b.hotkey for b in detail.bindings))
    else:
        say(
            "   no hotkeys are bound now -- task submissions will be refused "
            "as `contributor_unbound` until one is bound again"
        )
    say(
        "   Historical tasks keep this identity: unbinding only affects future "
        "submissions."
    )
    return 0


def run_migrate(args: argparse.Namespace) -> int:
    old = load_key(key_file(args.key_file))
    new = load_key(key_file(args.new_key_file))
    settings = _settings(args)

    if old.public_key == new.public_key:
        fail(
            f"--new-key-file points at the same key as --key-file "
            f"({old.public_key}). A migration has to change the signing key.\n"
            f"   → `openroboto identity keygen --key-file <new path>` first"
        )
        return 1

    _say_signing("migrate", old)
    text = fetch_contributor_message(
        settings.backend_url,
        "migrate",
        contributor_id=old.contributor_id,
        new_public_key=new.public_key,
        new_reward_address=args.reward_address,
    )
    contributor = migrate_contributor(
        settings.backend_url,
        old.contributor_id,
        new_public_key=new.public_key,
        new_reward_address=args.reward_address,
        # 🔴 Signed by the **old** key. The message names the new public key
        # and the new reward address, so this one signature authorises both.
        signature=old.sign(text.encode("utf-8")),
        miner_key=_miner_key(settings),
    )

    # Record the direction of the migration in the new key file. The pre-payment
    # gate in `submit` uses it to check the bindings that stayed on the old
    # identity, instead of refusing a submission the backend would accept.
    updated = replace(new, migrated_from=(*new.migrated_from, old.contributor_id))
    try:
        save_key(updated, new.path or key_file(args.new_key_file), overwrite=True)
    except OSError as exc:  # pragma: no cover -- a read-only key directory
        say(
            f"⚠️  migrated, but could not update {new.path} with the old id "
            f"({exc}). `openroboto submit` will still work; it will just check "
            f"only the new identity's bindings."
        )

    say(f"✅ migrated {old.contributor_id} → {contributor.contributor_id}")
    say(f"   new reward address: {contributor.reward_address}")
    say(f"   new key file:       {new.path}")
    say(
        "   Historical tasks remain on the old id and are aggregated along the "
        "migration chain."
    )
    say(
        "   Existing hotkey bindings stay on the old identity and admission "
        "resolves them along the same chain -- they do not need to be moved."
    )
    say(
        f"   → from now on use `--key-file {new.path}` (or replace the default "
        f"file with it); the CLI records the old id in that file for the "
        f"pre-payment check"
    )
    return 0


# ─── shared ─────────────────────────────────────────────────────────────────


def _miner_key(settings: Settings) -> str:
    """Backwards-compatible name; the implementation is `backend_api.miner_key`."""
    return miner_key(settings)


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.load(args.config)
    if not settings.backend_url:
        raise ConfigError(
            f"{args.config} does not name a backend (`backend.url`), so the "
            f"identity request has nowhere to go.\n"
            f"   → `openroboto init --refresh`, or set backend.url"
        )
    return settings


def _say_signing(action: str, key: ContributorKey) -> None:
    """Name the action and the identity before the network call. Never the key
    bytes -- this line is allowed in a log."""
    say(f"✍️  signing `{action}` with contributor {key.contributor_id}")
