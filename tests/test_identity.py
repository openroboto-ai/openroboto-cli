"""`openroboto identity` and the pre-payment binding gate in `submit`.

The acceptance condition for this feature is a chain of signatures, so the
tests check the bytes that go out rather than the wording on screen:

- `register` / `unbind` / `migrate` are signed by the contributor key;
- `bind` carries **two** signatures over the *same* message -- the contributor
  Ed25519 key and the hotkey's sr25519 key;
- `migrate` is signed by the **old** key, and the message names the new key and
  the new reward address;
- `submit`'s gate refuses to pay when the task track would refuse the author,
  and does not touch sim / real at all.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openroboto_protocol.commitment import Track

from openroboto import contributor as contributor_module
from openroboto.backend_api import (
    BackendError,
    Contributor,
    ContributorBinding,
    ContributorDetail,
)
from openroboto.cli import main
from openroboto.commands import identity as identity_command
from openroboto.commands import submit as submit_command
from openroboto.competition import Snapshot
from openroboto.config import Settings
from openroboto.contributor import (
    ContributorKey,
    generate_key,
    load_key,
    save_key,
)

NOW = datetime(2026, 10, 29, tzinfo=UTC)
SEED = bytes(range(32)).hex()
PUBLIC_KEY = "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
CONTRIBUTOR_ID = "07ec98a481181f8494f831703121d1e9041ba5e7bee012c2920cd735e3a65584"
REWARD = "5CcyqxXnJucaCnQQvvUg5EPzj1uoNAxACZvzArHw5aVDvgNH"
#: A valid ss58 address (sr25519, seed 0x01 * 32). The wallet fake returns it;
#: the backend checks the checksum, and a made-up string would not pass that.
HOTKEY = "5CcyqxXnJucaCnQQvvUg5EPzj1uoNAxACZvzArHw5aVDvgNH"


def _key() -> ContributorKey:
    return ContributorKey(public_key=PUBLIC_KEY, private_key=bytes.fromhex(SEED))


def _settings() -> Settings:
    return Settings(backend_url="https://api.example", hotkey="default")


def _contributor(**overrides: object) -> Contributor:
    fields: dict[str, object] = {
        "contributor_id": CONTRIBUTOR_ID,
        "public_key": PUBLIC_KEY,
        "reward_address": REWARD,
        "status": "active",
        "merged_into": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return Contributor(**fields)  # type: ignore[arg-type]


def _detail(**overrides: object) -> ContributorDetail:
    fields: dict[str, object] = {
        **_contributor().model_dump(),
        "bindings": [],
    }
    fields.update(overrides)
    return ContributorDetail(**fields)  # type: ignore[arg-type]


def _verify_with(key: ContributorKey, message: str, signature: str) -> None:
    private = Ed25519PrivateKey.from_private_bytes(key.private_key)
    private.public_key().verify(bytes.fromhex(signature), message.encode("utf-8"))


def _write_key(tmp_path: Path, name: str = "contributor.key") -> Path:
    path = tmp_path / name
    save_key(_key(), path)
    return path


class _FakeHotkey:
    def __init__(self) -> None:
        self.ss58_address = HOTKEY
        self.signed: list[bytes] = []

    def sign(self, message: bytes) -> bytes:
        self.signed.append(message)
        return b"\x11" * 64


class _FakeWallet:
    def __init__(self) -> None:
        self.hotkey = _FakeHotkey()


@pytest.fixture
def signing(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace the backend calls with recorders; keep signing real."""
    state: dict[str, object] = {"message": ""}

    def message(base_url: str, kind: str, **fields: str) -> str:
        state["kind"] = kind
        state["fields"] = fields
        text = f"openroboto:contributor:v1\nkind={kind}\n"
        state["message"] = text
        return text

    monkeypatch.setattr(identity_command, "fetch_contributor_message", message)
    monkeypatch.setattr(identity_command, "_settings", lambda args: _settings())
    return state


# ─── keygen ─────────────────────────────────────────────────────────────────


def test_keygen_writes_0600_and_never_prints_the_private_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "contributor.key"
    args = argparse.Namespace(key_file=str(path), force=False)

    assert identity_command.run_keygen(args) == 0
    output = capsys.readouterr().out
    assert str(path) in output
    key = load_key(path)
    assert key.contributor_id in output
    assert "0600" in output
    assert key.private_key.hex() not in output
    assert "Back this file up" in output


def test_keygen_does_not_replace_an_existing_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_key(tmp_path)
    args = argparse.Namespace(key_file=str(path), force=False)
    with pytest.raises(Exception) as excinfo:
        identity_command.run_keygen(args)
    assert "already holds a contributor key" in str(excinfo.value)


# ─── register ───────────────────────────────────────────────────────────────


def test_register_signs_the_backend_message_and_sends_no_id(
    tmp_path: Path, signing: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, str] = {}

    def register(base_url: str, **fields: str) -> Contributor:
        captured.update(fields)
        captured["base_url"] = base_url
        return _contributor()

    monkeypatch.setattr(identity_command, "register_contributor", register)
    args = argparse.Namespace(
        key_file=str(_write_key(tmp_path)),
        config="miner.yaml",
        reward_address=REWARD,
    )

    assert identity_command.run_register(args) == 0
    assert signing["kind"] == "register"
    assert signing["fields"] == {"public_key": PUBLIC_KEY, "reward_address": REWARD}
    # The id is derived by the server; it must not be in the request body.
    assert "contributor_id" not in captured
    _verify_with(_key(), str(signing["message"]), captured["signature"])


def test_register_refuses_an_id_the_server_derived_differently(
    tmp_path: Path,
    signing: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        identity_command,
        "register_contributor",
        lambda *a, **k: _contributor(contributor_id="ab" * 32),
    )
    args = argparse.Namespace(
        key_file=str(_write_key(tmp_path)),
        config="miner.yaml",
        reward_address=REWARD,
    )
    assert identity_command.run_register(args) == 1
    assert "disagree about the identity rule" in capsys.readouterr().err


# ─── bind ───────────────────────────────────────────────────────────────────


def test_bind_signs_the_same_message_with_both_keys(
    tmp_path: Path, signing: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet = _FakeWallet()
    monkeypatch.setattr(identity_command, "open_wallet", lambda settings: wallet)
    captured: dict[str, str] = {}

    def bind(base_url: str, contributor_id: str, **fields: str) -> ContributorBinding:
        captured.update(fields)
        assert contributor_id == CONTRIBUTOR_ID
        return ContributorBinding(hotkey=HOTKEY, created_at=NOW)

    monkeypatch.setattr(identity_command, "bind_contributor_hotkey", bind)
    args = argparse.Namespace(
        key_file=str(_write_key(tmp_path)),
        config="miner.yaml",
        hotkey="",
    )

    assert identity_command.run_bind(args) == 0
    message = str(signing["message"])
    _verify_with(_key(), message, captured["signature"])
    # The hotkey's sr25519 signature covers the same bytes, as 128 hex chars.
    assert wallet.hotkey.signed == [message.encode("utf-8")]
    assert captured["hotkey_signature"] == "11" * 64
    assert captured["hotkey"] == HOTKEY


def test_bind_refuses_a_hotkey_whose_private_key_is_not_in_the_wallet(
    tmp_path: Path,
    signing: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wallet = _FakeWallet()
    monkeypatch.setattr(identity_command, "open_wallet", lambda settings: wallet)
    called = False

    def bind(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(identity_command, "bind_contributor_hotkey", bind)
    args = argparse.Namespace(
        key_file=str(_write_key(tmp_path)),
        config="miner.yaml",
        hotkey="5" + "1" * 47,
    )

    assert identity_command.run_bind(args) == 1
    assert not called
    assert "Only that hotkey's own private key" in capsys.readouterr().err


# ─── unbind ─────────────────────────────────────────────────────────────────


def test_unbind_is_signed_by_the_contributor_key(
    tmp_path: Path, signing: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, str] = {}

    def revoke(base_url: str, contributor_id: str, **fields: str) -> ContributorDetail:
        captured.update(fields)
        return _detail(bindings=[])

    monkeypatch.setattr(identity_command, "revoke_contributor_hotkey", revoke)
    args = argparse.Namespace(
        key_file=str(_write_key(tmp_path)), config="miner.yaml", hotkey=HOTKEY
    )

    assert identity_command.run_unbind(args) == 0
    assert signing["kind"] == "unbind"
    assert captured["hotkey"] == HOTKEY
    _verify_with(_key(), str(signing["message"]), captured["signature"])


# ─── migrate ────────────────────────────────────────────────────────────────


def test_migrate_is_signed_by_the_old_key_and_names_the_new_one(
    tmp_path: Path, signing: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    old_path = _write_key(tmp_path)
    new = generate_key()
    new_path = tmp_path / "new.key"
    save_key(new, new_path)
    captured: dict[str, str] = {}

    def migrate(base_url: str, contributor_id: str, **fields: str) -> Contributor:
        captured.update(fields)
        return _contributor(
            contributor_id=new.contributor_id,
            public_key=new.public_key,
        )

    monkeypatch.setattr(identity_command, "migrate_contributor", migrate)
    args = argparse.Namespace(
        key_file=str(old_path),
        config="miner.yaml",
        new_key_file=str(new_path),
        reward_address=HOTKEY,
    )

    assert identity_command.run_migrate(args) == 0
    assert signing["kind"] == "migrate"
    assert captured["new_public_key"] == new.public_key
    assert captured["new_reward_address"] == HOTKEY
    # 🔴 Signed by the OLD key: it is the only one the backend trusts to
    # authorise the new key.
    _verify_with(_key(), str(signing["message"]), captured["signature"])
    # The new key file now remembers where it came from; `submit` needs that to
    # find bindings that stayed on the old identity.
    assert load_key(new_path).migrated_from == (CONTRIBUTOR_ID,)


def test_migrate_refuses_the_same_key_twice(
    tmp_path: Path, signing: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_key(tmp_path)
    args = argparse.Namespace(
        key_file=str(path),
        config="miner.yaml",
        new_key_file=str(path),
        reward_address=HOTKEY,
    )
    assert identity_command.run_migrate(args) == 1
    assert "has to change the signing key" in capsys.readouterr().err


# ─── submit's pre-payment gate ──────────────────────────────────────────────


#: The task track exists only in the protocol build that carries it. On an
#: older install these gate tests are skipped rather than failing: the code
#: under them returns True for every track in that case (see `_TASK_TRACK` in
#: `commands/submit.py`).
TASK = getattr(Track, "TASK", None)


def _live(track: Track | None) -> SimpleNamespace:
    if track is None:
        pytest.skip("installed openroboto-protocol has no task track")
    return SimpleNamespace(track=track, label="task/1")


def _state() -> dict[str, str]:
    return {"hotkey_ss58": HOTKEY}


def _patch_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(submit_command, "load_key", lambda path=None: _key())


def test_submit_selects_bound_key_instead_of_unregistered_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Real key selection; scene validation, upload and payment are simulated."""
    default_path = _write_key(tmp_path)
    selected_path = tmp_path / "author.key"
    selected = generate_key()
    save_key(selected, selected_path)
    original_keys = {p: p.read_bytes() for p in (default_path, selected_path)}
    monkeypatch.setattr(contributor_module, "DEFAULT_KEY_FILE", default_path)
    snapshot = Snapshot(
        {"id": 77, "track": "task", "seq": 1, "label": "task/1", "params": {}}
    )
    live = SimpleNamespace(track="task", label="task/1", params={})
    verdict = SimpleNamespace(live=live, kind="burn", cid=77)
    uploaded = {"hf_repo_id": "author/scene", "hf_commit": "a" * 40}
    state = {**_state(), **uploaded, "hf_url": "https://example.invalid/scene"}
    calls: list[str] = []
    identities: list[str] = []
    monkeypatch.setattr(submit_command.Settings, "load", lambda *a: _settings())
    monkeypatch.setattr(submit_command, "load_snapshot", lambda *a: snapshot)
    monkeypatch.setattr(submit_command, "load_state", lambda *a: state)
    monkeypatch.setattr(submit_command, "save_state", lambda *a: None)
    monkeypatch.setattr(submit_command, "_prepare_scene_submission", lambda *a: True)
    monkeypatch.setattr(submit_command, "resolve_competition", lambda *a: verdict)

    def upload(*args: object, **kwargs: object) -> None:
        assert kwargs["reuse_existing"] is True
        assert all(state[k] == v for k, v in uploaded.items())
        calls.append("reuse-upload")

    def fetch(base_url: str, contributor_id: str) -> ContributorDetail:
        assert base_url == _settings().backend_url
        identities.append(contributor_id)
        if contributor_id == CONTRIBUTOR_ID:
            raise BackendError("not registered", code="CONTRIBUTOR_NOT_FOUND")
        assert contributor_id == selected.contributor_id
        return _detail(
            contributor_id=selected.contributor_id,
            public_key=selected.public_key,
            bindings=[ContributorBinding(hotkey=HOTKEY, created_at=NOW)],
        )

    monkeypatch.setattr(submit_command, "perform_upload", upload)
    monkeypatch.setattr(submit_command, "fetch_contributor", fetch)
    for method in (
        "layout_is_payable",
        "task_package_upload_is_unchanged",
        "slot_is_free",
        "evaluator_can_read",
    ):
        monkeypatch.setattr(submit_command, method, lambda *a: True)
    monkeypatch.setattr(
        submit_command, "confirm_payment", lambda *a: calls.append("confirm")
    )
    monkeypatch.setattr(
        submit_command, "perform_burn", lambda *a, **k: calls.append("pay") or True
    )
    monkeypatch.setattr(
        submit_command, "perform_announce", lambda *a: calls.append("announce") or True
    )
    argv = [
        "--log-dir",
        str(tmp_path / "logs"),
        "submit",
        "--config",
        str(tmp_path / "miner.yaml"),
        "--scene-package",
        str(tmp_path / "scene"),
    ]
    assert main(argv) == 1
    assert "not registered" in capsys.readouterr().err
    assert calls == ["reuse-upload"]
    assert main([*argv, "--key-file", str(selected_path)]) == 0
    assert identities == [CONTRIBUTOR_ID, selected.contributor_id]
    assert calls == ["reuse-upload", "reuse-upload", "confirm", "pay", "announce"]
    assert all(state[k] == v for k, v in uploaded.items())
    assert {p: p.read_bytes() for p in original_keys} == original_keys


def test_the_gate_ignores_sim_and_real(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fetch(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(submit_command, "fetch_contributor", fetch)
    for track in (Track.SIM, Track.REAL):
        assert submit_command.contributor_is_bound(_settings(), _state(), _live(track))
    assert not called


def test_the_gate_passes_a_bound_hotkey(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_key(monkeypatch)
    monkeypatch.setattr(
        submit_command,
        "fetch_contributor",
        lambda base_url, contributor_id: _detail(
            bindings=[ContributorBinding(hotkey=HOTKEY, created_at=NOW)]
        ),
    )
    assert submit_command.contributor_is_bound(_settings(), _state(), _live(TASK))
    assert capsys.readouterr().err == ""


def test_the_gate_refuses_an_unbound_hotkey_before_paying(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_key(monkeypatch)
    monkeypatch.setattr(
        submit_command, "fetch_contributor", lambda base_url, contributor_id: _detail()
    )
    assert not submit_command.contributor_is_bound(_settings(), _state(), _live(TASK))
    error = capsys.readouterr().err
    assert "identity bind" in error
    assert "Nothing was paid" in error


def test_the_gate_says_how_to_register_an_unknown_identity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_key(monkeypatch)

    def fetch(*args: object, **kwargs: object) -> None:
        raise BackendError("not found", code="CONTRIBUTOR_NOT_FOUND")

    monkeypatch.setattr(submit_command, "fetch_contributor", fetch)
    assert not submit_command.contributor_is_bound(_settings(), _state(), _live(TASK))
    error = capsys.readouterr().err
    assert "identity register" in error
    assert "identity bind" in error


def test_the_gate_refuses_when_the_backend_cannot_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_key(monkeypatch)

    def fetch(*args: object, **kwargs: object) -> None:
        raise BackendError("down", retryable=True)

    monkeypatch.setattr(submit_command, "fetch_contributor", fetch)
    assert not submit_command.contributor_is_bound(_settings(), _state(), _live(TASK))
    assert "nothing was paid" in capsys.readouterr().err.lower()


def test_the_gate_follows_a_migration_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_key(monkeypatch)
    new_id = "cd" * 32

    def fetch(base_url: str, contributor_id: str) -> ContributorDetail:
        if contributor_id == CONTRIBUTOR_ID:
            return _detail(
                status="migrated",
                merged_into=new_id,
                bindings=[ContributorBinding(hotkey=HOTKEY, created_at=NOW)],
            )
        assert contributor_id == new_id
        return _detail(contributor_id=new_id)

    monkeypatch.setattr(submit_command, "fetch_contributor", fetch)
    assert submit_command.contributor_is_bound(_settings(), _state(), _live(TASK))


def test_the_gate_checks_bindings_an_identity_inherited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A migration does not move bindings. The new key must look at the ids it
    inherited, or rotating a key would make `submit` refuse a valid hotkey."""
    inherited = ContributorKey(
        public_key=PUBLIC_KEY,
        private_key=bytes.fromhex(SEED),
        migrated_from=(CONTRIBUTOR_ID,),
    )
    monkeypatch.setattr(submit_command, "load_key", lambda path=None: inherited)
    monkeypatch.setattr(
        submit_command,
        "fetch_contributor",
        lambda base_url, contributor_id: _detail(
            bindings=[ContributorBinding(hotkey=HOTKEY, created_at=NOW)]
        ),
    )
    assert submit_command.contributor_is_bound(_settings(), _state(), _live(TASK))


def test_the_gate_refuses_a_migration_chain_that_ends_inactive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_key(monkeypatch)
    new_id = "cd" * 32

    def fetch(base_url: str, contributor_id: str) -> ContributorDetail:
        if contributor_id == CONTRIBUTOR_ID:
            return _detail(
                status="migrated",
                merged_into=new_id,
                bindings=[ContributorBinding(hotkey=HOTKEY, created_at=NOW)],
            )
        return _detail(contributor_id=new_id, status="migrated", merged_into=None)

    monkeypatch.setattr(submit_command, "fetch_contributor", fetch)
    assert not submit_command.contributor_is_bound(_settings(), _state(), _live(TASK))
    assert "migrated" in capsys.readouterr().err.lower()


# ─── show / rate-limit key ──────────────────────────────────────────────────


def test_show_lists_bindings_inherited_through_a_migration(
    tmp_path: Path,
    signing: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """After a migration the current id can have no bindings while the old one
    still authorises the hotkey. Saying "none" there would send the miner to
    `bind`, which answers `HOTKEY_ALREADY_BOUND`."""
    old_id = "ab" * 32
    key = ContributorKey(
        public_key=PUBLIC_KEY,
        private_key=bytes.fromhex(SEED),
        migrated_from=(old_id,),
    )
    path = tmp_path / "contributor.key"
    save_key(key, path)

    def fetch(base_url: str, contributor_id: str) -> ContributorDetail:
        if contributor_id == CONTRIBUTOR_ID:
            return _detail(bindings=[])
        assert contributor_id == old_id
        return _detail(
            contributor_id=old_id,
            bindings=[ContributorBinding(hotkey=HOTKEY, created_at=NOW)],
        )

    monkeypatch.setattr(identity_command, "fetch_contributor", fetch)
    args = argparse.Namespace(key_file=str(path), config="miner.yaml")

    assert identity_command.run_show(args) == 0
    output = capsys.readouterr().out
    assert HOTKEY in output
    assert "inherited from" in output
    assert "identity bind" not in output


def test_show_says_none_when_nothing_is_bound(
    tmp_path: Path,
    signing: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        identity_command, "fetch_contributor", lambda *a: _detail(bindings=[])
    )
    args = argparse.Namespace(key_file=str(_write_key(tmp_path)), config="miner.yaml")
    assert identity_command.run_show(args) == 0
    output = capsys.readouterr().out
    assert "bound hotkeys:  none" in output
    assert "identity bind" in output


def test_the_rate_limit_key_comes_from_config_or_control_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openroboto import backend_api
    from openroboto.config import ControlFetch, ControlFetchError

    settings = _settings()
    settings.backend_public_key = "from-config"
    assert identity_command._miner_key(settings) == "from-config"

    settings.backend_public_key = ""
    settings.control_json_url = "https://api.example/control.json"
    monkeypatch.setattr(
        backend_api,
        "fetch_control",
        lambda url: ControlFetch({"public_key": "from-control"}, ""),
    )
    assert identity_command._miner_key(settings) == "from-control"

    def boom(url: str) -> None:
        raise ControlFetchError("down")

    monkeypatch.setattr(backend_api, "fetch_control", boom)
    assert identity_command._miner_key(settings) == ""

    settings.control_json_url = ""
    assert identity_command._miner_key(settings) == ""
