"""Command layer: init / check / build / status / round_state / preflight.

All pure local logic: no network, no GPU, no chain.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from openroboto_protocol.model_format import (
    CheckpointKind,
    FormatIssue,
    FormatIssueCode,
    FormatReport,
)
from openroboto_protocol.schemas import (
    Competition,
    ListEnvelope,
    Reason,
    ScanRejection,
    SubmissionHistoryItem,
)

from openroboto.backend_api import BackendError, RosterEntry
from openroboto.commands import build as build_command
from openroboto.commands import check as check_command
from openroboto.commands import doctor as doctor_command
from openroboto.commands import init as init_command
from openroboto.commands import status as status_command
from openroboto.commands import train as train_command
from openroboto.config import ConfigError, Settings
from openroboto.huggingface import build_repo_id, commit_sha_from_url
from openroboto.preflight import (
    check_announce_ready,
    check_burn_window,
    payload_size,
)
from openroboto.round_state import (
    StateError,
    is_step_done,
    load_state,
    resolve_output_dir,
    resolve_round,
    save_state,
)
from openroboto.training import container
from openroboto.training.container import DEFAULT_IMAGE, runner_image
from openroboto.training.round import TrainParams

BIG_ENOUGH = 11 * 1024 * 1024  # the protocol requires >= 10 MB per repo; below that it
# is judged "only a pointer was uploaded"


# ─── init ────────────────────────────────────────────────────
#
# `init` is the one command that needs the network, so every case here fakes
# the competition list. The two that matter most are the ones about **not**
# writing: an unreachable backend must leave the target directory empty, and
# `--refresh` must not touch a single line outside the competition section.

COMPETITION_ROW: dict[str, Any] = {
    "id": 3,
    "track": "real",
    "seq": 1,
    "label": "xArm 6 第一届",
    "adapter": "real_xarm6",
    "status": "active",
    "submit_closes_at": "2026-09-10T00:00:00Z",
    "base_repo": None,
    "base_revision": None,
    "params": {
        "fee": {"kind": "transfer", "amount_tao": 2.0, "coldkey": None},
        "training": {"image": "lingbot-runner:1.2"},
    },
}


def _row(**overrides: Any) -> Competition:
    return Competition.model_validate(COMPETITION_ROW | overrides)


def _serving(
    monkeypatch: pytest.MonkeyPatch, *rows: Competition, netuid: int = 313
) -> list[str]:
    """Answer `init`'s requests. Returns the base URLs the competition list was
    asked for, so "sent no request at all" is an assertion and not an assumption.

    `netuid` is what a backend this client does not host answers when asked which
    subnet it watches. The default `--backend-url` below is exactly that kind of
    address, so every case here goes down the self-hosted path unless it says
    otherwise.
    """
    called: list[str] = []

    def _fetch(base_url: str, **kwargs: Any) -> Any:
        called.append(base_url)
        return SimpleNamespace(data=list(rows))

    monkeypatch.setattr(init_command, "fetch_competitions", _fetch)
    monkeypatch.setattr(init_command, "fetch_netuid", lambda base_url: netuid)
    return called


def _init_args(directory: Path, **overrides: Any) -> argparse.Namespace:
    args = argparse.Namespace(
        directory=str(directory),
        strategy="",
        validator=False,
        refresh=False,
        backend_url="http://backend.test",
        force=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_init_releases_config_and_strategy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Miners clone nothing: the templates ship in the wheel and init unpacks them."""
    _serving(monkeypatch, _row())
    assert init_command.run(_init_args(tmp_path / "my-miner")) == 0

    target = tmp_path / "my-miner"
    assert (target / "miner.yaml").is_file()
    assert "def train(" in (target / "train_strategy.py").read_text(encoding="utf-8")
    # the template must be readable back by our own parser
    assert Settings.load(str(target / "miner.yaml")).netuid == 313


def test_the_workspace_points_where_the_backend_url_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The one that was broken: **ask a testnet backend, get a testnet
    workspace.**

    `init` used to write a static mainnet block around whatever season it had
    just fetched. Asking a local backend on netuid 313 produced a `competition:`
    from that backend inside `environment: mainnet` / `netuid: 80` / production
    URLs, with no `backend:` section at all -- so `submit` would confirm the
    season against **production**, match it by `(track, seq)` (both sides seed
    the same tracks), and burn mainnet TAO for a season nobody there had heard
    of. Nothing in the file was inconsistent with anything else in it.
    """
    _serving(monkeypatch, _row(), netuid=313)
    args = _init_args(tmp_path / "w", backend_url="http://127.0.0.1:8011")
    assert init_command.run(args) == 0

    written = Settings.load(str(tmp_path / "w" / "miner.yaml"))
    assert written.netuid == 313
    assert written.environment == "local"
    assert written.network != "finney"
    assert written.backend_url == "http://127.0.0.1:8011"
    assert written.control_json_url == "http://127.0.0.1:8011/control.json"
    # and the season carries where it came from, which is the fact no field
    # above can contradict on its own.
    assert written.competition_source == "http://127.0.0.1:8011"


def test_the_default_still_writes_the_mainnet_workspace_it_always_did(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 No `--backend-url` is the miner's normal path, and it must be the
    workspace they have always got: mainnet, finney, 80, production URLs.

    It also asks no probe. A hosted environment states its own netuid in
    `config/environments.py` -- which is the same table `check_coherent()`
    enforces -- so asking the backend instead would be a second source for one
    fact, and one more request between a miner and their first command.
    """
    _serving(monkeypatch, _row())

    def _no_probe(base_url: str) -> int:
        raise AssertionError("mainnet states its own netuid; do not go and ask")

    monkeypatch.setattr(init_command, "fetch_netuid", _no_probe)

    assert init_command.run(_init_args(tmp_path / "w", backend_url="")) == 0

    written = Settings.load(str(tmp_path / "w" / "miner.yaml"))
    assert (written.environment, written.network, written.netuid) == (
        "mainnet",
        "finney",
        80,
    )
    assert written.backend_url == "https://api.openroboto.ai"
    assert written.control_json_url == "https://api.openroboto.ai/control.json"
    # coherent end to end, including where the season came from
    written.require_for_chain()


def test_a_backend_that_will_not_say_which_subnet_it_watches_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same rule as the competition list: a netuid is not something to
    guess, so not knowing it stops the command instead of picking one."""
    _serving(monkeypatch, _row())

    def _unreachable(base_url: str) -> int:
        raise BackendError("connection refused", retryable=True)

    monkeypatch.setattr(init_command, "fetch_netuid", _unreachable)

    target = tmp_path / "w"
    assert init_command.run(_init_args(target)) == 1
    assert not target.exists() or list(target.iterdir()) == []


def test_init_takes_the_only_competition_without_asking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Asking which of one is not a choice, it is a keystroke."""
    _serving(monkeypatch, _row())

    def _no_input(*args: Any) -> str:
        raise AssertionError("init asked a question with only one possible answer")

    monkeypatch.setattr("builtins.input", _no_input)

    assert init_command.run(_init_args(tmp_path / "w")) == 0
    assert "cid=3" in capsys.readouterr().out


def test_init_writes_the_competition_the_miner_picked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _serving(
        monkeypatch,
        _row(id=1, track="sim", seq=2, label="LingBot-VLA 2.0", adapter="sim_lingbot"),
        _row(),
    )
    monkeypatch.setattr("builtins.input", lambda *a: "2")

    assert init_command.run(_init_args(tmp_path / "w")) == 0

    listing = capsys.readouterr().out
    assert "1. LingBot-VLA 2.0" in listing
    assert "2. xArm 6 第一届" in listing
    written = Settings.load(str(tmp_path / "w" / "miner.yaml"))
    assert written.competition["id"] == 3
    assert written.competition_adapter == "real_xarm6"


def test_section_keys_track_the_protocol_contract() -> None:
    """🔴 **The alarm on a deliberately blocked line.**

    `base_model_family` is missing from `SECTION_KEYS` only because the pinned
    protocol package (0.7.0) has no such field: `Contract` keeps pydantic's
    `extra=ignore`, so `Competition` drops it on the way in and `model_dump()`
    would not have the key. Adding it today breaks `openroboto init` outright.

    So the two are tied together here instead of in a comment nobody re-reads:
    the moment the pin moves to 0.8.0 this fails, and the fix is one line in
    `commands/init.py`. Until then a workspace simply has no such key, and
    `adapters.base_model_family()` resolves the two sim adapters and refuses for
    the real track -- which is the honest answer while xArm 6's base model is
    `null` in the database anyway.

    The general property (and the reason this is not just a version assertion):
    every column `init` copies has to exist on the model it copies from, or the
    command dies on a `KeyError` at the one moment a miner cannot recover from
    it -- their first ever command.
    """
    fields = set(Competition.model_fields)
    assert set(init_command.SECTION_KEYS) <= fields, (
        f"init copies columns the protocol package does not publish: "
        f"{sorted(set(init_command.SECTION_KEYS) - fields)}"
    )
    if "base_model_family" in fields:
        assert "base_model_family" in init_command.SECTION_KEYS, (
            "the protocol pin moved: add `base_model_family` to SECTION_KEYS, "
            "or miner.yaml will not say which base model the season runs on and "
            "every real-track workspace stays refused"
        )


def test_init_keeps_the_competition_parameters_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A season adding a key to `params` must not need a release of this
    package, so nothing is picked out of it on the way to disk."""
    _serving(monkeypatch, _row())
    assert init_command.run(_init_args(tmp_path / "w")) == 0

    written = Settings.load(str(tmp_path / "w" / "miner.yaml"))
    assert written.competition_params == COMPETITION_ROW["params"]
    # 🔴 including the `null` collection address: filled in with anything, the
    # fail-closed gate before payment never fires.
    assert written.competition_params["fee"]["coldkey"] is None


def test_init_writes_nothing_at_all_when_the_backend_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 Half a workspace is worse than none: the next thing a miner does is
    `build`, and a config with no competition in it builds the wrong image and
    is judged by the wrong rules."""

    def _fetch(base_url: str, **kwargs: Any) -> Any:
        raise BackendError("connection refused", retryable=True)

    monkeypatch.setattr(init_command, "fetch_competitions", _fetch)

    target = tmp_path / "w"
    assert init_command.run(_init_args(target)) == 1
    assert not target.exists() or list(target.iterdir()) == []


def test_init_does_not_invent_a_competition_when_none_are_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serving(monkeypatch)
    target = tmp_path / "w"
    assert init_command.run(_init_args(target)) == 1
    assert not target.exists() or list(target.iterdir()) == []


def test_init_refuses_a_competition_this_client_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writing the workspace anyway would leave the miner one `pip install`
    short of a config that every later command refuses."""
    _serving(monkeypatch, _row(adapter="real_xarm7"))
    target = tmp_path / "w"
    assert init_command.run(_init_args(target)) == 1
    assert not target.exists() or list(target.iterdir()) == []


def test_init_unpacks_the_strategy_the_competition_asks_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    params = COMPETITION_ROW["params"] | {"strategy_template": "example"}
    _serving(monkeypatch, _row(params=params))
    assert init_command.run(_init_args(tmp_path / "w")) == 0

    unpacked = (tmp_path / "w" / "train_strategy.py").read_text(encoding="utf-8")
    assert "def train(" in unpacked
    # the annotated teaching version, not the minimal one
    assert unpacked != (
        files("openroboto") / "templates" / "simple" / "train_strategy.py"
    ).read_text(encoding="utf-8")


def test_an_explicit_strategy_beats_the_competitions_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    params = COMPETITION_ROW["params"] | {"strategy_template": "example"}
    _serving(monkeypatch, _row(params=params))
    assert init_command.run(_init_args(tmp_path / "w", strategy="simple")) == 0

    assert (tmp_path / "w" / "train_strategy.py").read_text(encoding="utf-8") == (
        files("openroboto") / "templates" / "simple" / "train_strategy.py"
    ).read_text(encoding="utf-8")


def test_a_strategy_template_this_client_does_not_ship_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Handing over the generic script instead would be found out at `check`,
    after the training run."""
    params = COMPETITION_ROW["params"] | {"strategy_template": "lingbot_v2"}
    _serving(monkeypatch, _row(params=params))
    target = tmp_path / "w"
    assert init_command.run(_init_args(target)) == 1
    assert not target.exists() or list(target.iterdir()) == []


def test_init_does_not_overwrite_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serving(monkeypatch, _row())
    config = tmp_path / "miner.yaml"
    config.write_text("subnet:\n  netuid: 80\n", encoding="utf-8")

    args = _init_args(tmp_path)
    init_command.run(args)
    assert config.read_text(encoding="utf-8") == "subnet:\n  netuid: 80\n"

    args.force = True
    init_command.run(args)
    assert "OpenRoboto" in config.read_text(encoding="utf-8")


def test_init_validator_sends_no_request_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """External validators watch the whole subnet; there is no competition to
    pick, and asking for one would break them the day the endpoint is down."""
    called = _serving(monkeypatch, _row())
    init_command.run(_init_args(tmp_path, validator=True))

    assert called == []
    assert (tmp_path / "validator.yaml").is_file()
    assert not (tmp_path / "train_strategy.py").exists()


def test_init_gitignores_the_file_holding_the_wallet_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.gitignore` must block the config file -- this is a security item, not a
    convenience item.

    `miner.yaml` holds `subnet.wallet_password` and `huggingface.token`, and it is
    entirely reasonable for a miner to version their own `train_strategy.py`. Without
    this line, the very first `git add .` commits the wallet password, and **there is
    no warning of any kind**.
    """
    _serving(monkeypatch, _row())
    for validator in (False, True):
        target = tmp_path / ("val" if validator else "miner")
        assert init_command.run(_init_args(target, validator=validator)) == 0

        ignored = (target / ".gitignore").read_text(encoding="utf-8")
        assert "miner.yaml" in ignored
        assert "validator.yaml" in ignored
        # the strategy script, conversely, **must** stay committable -- it is the
        # miner's own work and deserves a history.
        assert "train_strategy.py" not in ignored


def test_init_writes_a_workspace_readme_that_names_the_next_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workspace ships its own manual. A miner should not have to open a web page
    to find out which command comes next."""
    _serving(monkeypatch, _row())
    assert init_command.run(_init_args(tmp_path / "w")) == 0

    readme = (tmp_path / "w" / "README.md").read_text(encoding="utf-8")
    for command in ("openroboto doctor", "openroboto train", "openroboto check"):
        assert command in readme, f"README does not mention {command}"
    # skipping check costs non-refundable TAO, so this sentence must be there
    assert "not refunded" in readme


# ─── init --refresh ──────────────────────────────────────────


def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace the miner has since filled in and commented."""
    _serving(monkeypatch, _row())
    target = tmp_path / "w"
    assert init_command.run(_init_args(target)) == 0

    config = target / "miner.yaml"
    edited = config.read_text(encoding="utf-8").replace(
        'token: ""', 'token: "hf_secret"  # my write token'
    )
    config.write_text(edited, encoding="utf-8")
    return target


def test_refresh_updates_the_competition_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 Overwriting a hand-edited value is the worst accident this command can
    cause -- and unlike a crash, nobody finds out."""
    target = _workspace(tmp_path, monkeypatch)
    _serving(monkeypatch, _row(label="xArm 6 第一届（延长）", status="active"))

    assert init_command.run(_init_args(target, refresh=True)) == 0

    after = (target / "miner.yaml").read_text(encoding="utf-8")
    assert 'token: "hf_secret"  # my write token' in after
    assert "延长" in after
    # every comment of the shipped template is still there, byte for byte
    assert "# ─── Bittensor subnet ─" in after


def test_refresh_never_asks_which_season(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 `--refresh` re-reads the season this workspace already mines.

    Asking looked harmless because `_pick` takes the only row without a prompt
    when one season is open, which is how this survived until four were. With
    several open, a mistyped digit silently repoints the workspace at a
    different season while the hyperparameters, the strategy script and
    anything already trained in `tmp/` stay exactly where they were -- and this
    flag's whole contract is "leave every other line untouched".

    `input` is replaced with something that fails loudly: a prompt here is the
    defect, so it must not be answerable.
    """
    target = _workspace(tmp_path, monkeypatch)

    def _no_prompt(_: str = "") -> str:
        raise AssertionError("--refresh asked which competition to use")

    monkeypatch.setattr("builtins.input", _no_prompt)
    # Four open, and the workspace's own is not the first in the list.
    _serving(
        monkeypatch,
        _row(id=7, track="sim", seq=1, label="π0.5", adapter="sim_openpi"),
        _row(id=8, track="sim", seq=2, label="LingBot", adapter="sim_lingbot"),
        _row(id=9, track="real", seq=2, label="xArm 6 第二届"),
        _row(label="xArm 6 第一届（延长）"),
    )

    assert init_command.run(_init_args(target, refresh=True)) == 0

    # ⚠️ Assert on the section, not on the whole file: the shipped template's
    #    prose mentions LingBot by name, so a substring search over the file
    #    would pass for the wrong reason.
    section = yaml.safe_load((target / "miner.yaml").read_text(encoding="utf-8"))[
        "competition"
    ]
    assert (section["track"], section["seq"]) == ("real", 1)
    assert section["label"] == "xArm 6 第一届（延长）"


def test_refresh_refuses_when_the_season_is_no_longer_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refusing is the only move: a workspace points at one season, and this
    flag cannot repoint it without breaking its own contract. The message has
    to name the other command, or the miner is stuck holding a workspace that
    no command will touch."""
    target = _workspace(tmp_path, monkeypatch)
    before = (target / "miner.yaml").read_text(encoding="utf-8")
    _serving(monkeypatch, _row(id=9, track="real", seq=2, label="xArm 6 第二届"))
    capsys.readouterr()  # drop what `_workspace` printed; only the refusal matters

    assert init_command.run(_init_args(target, refresh=True)) == 1

    assert (target / "miner.yaml").read_text(encoding="utf-8") == before
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "openroboto init" in out
    assert "xArm 6 第一届" in out, "the refusal has to name the season it looked for"


def test_refresh_keeps_the_previous_version_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backup **is** the rollback path: rewriting the config is the only
    part of this command that cannot be undone."""
    target = _workspace(tmp_path, monkeypatch)
    before = (target / "miner.yaml").read_text(encoding="utf-8")
    _serving(monkeypatch, _row(label="renamed"))

    assert init_command.run(_init_args(target, refresh=True)) == 0

    assert (target / "miner.yaml.bak").read_text(encoding="utf-8") == before


def test_refresh_prints_what_it_is_about_to_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _workspace(tmp_path, monkeypatch)
    _serving(monkeypatch, _row(label="renamed"))

    init_command.run(_init_args(target, refresh=True))

    printed = capsys.readouterr().out
    assert "-  label: xArm 6 第一届" in printed
    assert "+  label: renamed" in printed


def test_refresh_refuses_rather_than_guessing_where_the_section_went(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guessing means rewriting a line that is not the one it meant."""
    target = _workspace(tmp_path, monkeypatch)
    config = target / "miner.yaml"
    config.write_text("subnet:\n  netuid: 80\n", encoding="utf-8")
    digest = hashlib.md5(config.read_bytes()).hexdigest()
    _serving(monkeypatch, _row())

    assert init_command.run(_init_args(target, refresh=True)) == 1
    assert hashlib.md5(config.read_bytes()).hexdigest() == digest
    assert not (target / "miner.yaml.bak").exists()


def test_a_refresh_from_another_backend_is_refused_rather_than_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The one case writing cannot fix.

    `--refresh` rewrites the competition section **and nothing else** -- that is
    its contract and the reason miners trust it with a hand-edited file. So a
    season fetched from another backend would land inside this workspace's
    existing chain settings, which is precisely the shape that pays one
    subnet's fee for another subnet's season. It is refused instead.
    """
    target = _workspace(tmp_path, monkeypatch)
    _serving(monkeypatch, _row(label="renamed"))
    before = (target / "miner.yaml").read_text(encoding="utf-8")

    args = _init_args(target, refresh=True, backend_url="http://127.0.0.1:8011")
    assert init_command.run(args) == 1

    assert (target / "miner.yaml").read_text(encoding="utf-8") == before
    assert not (target / "miner.yaml.bak").exists()


def test_the_picker_on_a_closed_stdin_says_so_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A traceback out of `input()` reads like a broken client, and the miner
    running this in CI has no way to tell that it is not one."""
    _serving(monkeypatch, _row(), _row(id=1, track="sim", seq=2))

    def _eof(*args: Any) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)

    target = tmp_path / "w"
    assert init_command.run(_init_args(target)) == 1
    assert not target.exists() or list(target.iterdir()) == []


def test_refresh_on_a_workspace_that_does_not_exist_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serving(monkeypatch, _row())
    assert init_command.run(_init_args(tmp_path / "nowhere", refresh=True)) == 1


# ─── check ───────────────────────────────────────────────────


def _make_file(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    os.truncate(path, size)  # sparse file; does not actually write 11 MB


def test_check_rejects_a_bare_lora_adapter(tmp_path: Path) -> None:
    """This path is the "burned the TAO, then found out an adapter was uploaded" run."""
    _make_file(tmp_path / "adapter_model.safetensors", BIG_ENOUGH)
    (tmp_path / "adapter_config.json").write_text("{}", encoding="utf-8")

    report = check_command.check_directory(tmp_path)
    assert not report.ok
    assert any(issue.code == "bare_lora_adapter" for issue in report.errors)


def test_check_accepts_a_complete_pytorch_checkpoint(tmp_path: Path) -> None:
    _make_file(tmp_path / "model.safetensors", BIG_ENOUGH)
    _make_file(tmp_path / "assets/physical-intelligence/libero/norm_stats.json", 1024)

    report = check_command.check_directory(tmp_path)
    assert report.ok
    assert report.kind == "pytorch"


def test_check_reports_missing_norm_stats(tmp_path: Path) -> None:
    _make_file(tmp_path / "model.safetensors", BIG_ENOUGH)
    report = check_command.check_directory(tmp_path)
    assert not report.ok
    assert any(issue.code == "missing_norm_stats" for issue in report.errors)


def test_check_exit_code_follows_the_report(tmp_path: Path) -> None:
    _make_file(tmp_path / "model.safetensors", BIG_ENOUGH)
    _make_file(tmp_path / "assets/physical-intelligence/libero/norm_stats.json", 1024)
    ok_report = check_command.check_directory(tmp_path)
    assert check_command.report_result(tmp_path, ok_report) == 0


def test_check_exit_code_is_nonzero_when_the_report_fails(tmp_path: Path) -> None:
    """The half that costs money. Miners chain these as `check && submit`, so a
    rejected checkpoint that still exits 0 sends them straight to `burn` -- and
    the burn behind a rejected submission is not refunded."""
    _make_file(tmp_path / "adapter_model.safetensors", BIG_ENOUGH)
    (tmp_path / "adapter_config.json").write_text("{}", encoding="utf-8")

    bad_report = check_command.check_directory(tmp_path)
    assert not bad_report.ok
    assert check_command.report_result(tmp_path, bad_report) == 1


def test_check_accepts_a_repository_carrying_gitattributes(tmp_path: Path) -> None:
    """Regression: a `.gitattributes` is what `git lfs track` leaves behind, so
    essentially every real HF model repo has one. An earlier revision counted it
    as an unexpected file and rejected 51 of 51 valid submissions."""
    _make_file(tmp_path / "model.safetensors", BIG_ENOUGH)
    _make_file(tmp_path / "assets/physical-intelligence/libero/norm_stats.json", 1024)
    (tmp_path / ".gitattributes").write_text(
        "*.safetensors filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text("# model card\n", encoding="utf-8")

    report = check_command.check_directory(tmp_path)
    assert report.ok, [issue.code for issue in report.errors]


def test_check_flags_a_repository_of_lfs_pointers(tmp_path: Path) -> None:
    """Cloning without `git lfs pull` leaves ~130-byte pointer files with the
    right names. Every per-file rule passes; only the total size gives it away."""
    _make_file(tmp_path / "model.safetensors", 133)
    _make_file(tmp_path / "assets/physical-intelligence/libero/norm_stats.json", 1024)

    report = check_command.check_directory(tmp_path)
    assert not report.ok
    assert any(issue.code == "total_size_too_small" for issue in report.errors)


def test_check_blocks_a_checkpoint_the_evaluator_will_never_find(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An **openpi** checkpoint nested three levels down, judged by the openpi
    rules -- a π0.5 miner who uploaded a training output directory whole.

    (The vendor's LingBot artifact has the same shape but not the same files;
    it is a different rule book and lives in `HF_CKPT` below.)

    Admission **accepts** this, the evaluator searches two levels and finds
    nothing. Exiting 0 here would send the miner to `submit` -- burn the TAO,
    take the queue slot, score nothing, and the burn is not refunded. Failing
    at the last step costs more than being rejected at the first, so this run
    has to stop before the money moves.
    """
    deep = tmp_path / "checkpoints" / "global_step_50000" / "hf_ckpt"
    _make_file(deep / "model.safetensors", BIG_ENOUGH)
    _make_file(deep / "assets/physical-intelligence/libero/norm_stats.json", 1024)

    report = check_command.check_directory(tmp_path)
    assert report.ok, [issue.code for issue in report.errors]
    assert [issue.code for issue in report.warnings] == ["nested_too_deep"]

    assert check_command.report_result(tmp_path, report) == 1

    out = capsys.readouterr().out
    # the advice has to name the miner's own directory, not "the structure is
    # invalid" -- they have to be able to copy the line and run it
    assert "checkpoints/global_step_50000/hf_ckpt" in out
    assert "--output-dir" in out
    assert "not refunded" in out


def test_check_names_the_directory_that_should_have_been_uploaded(
    tmp_path: Path,
) -> None:
    """Both weight forms, plus the two cases where there is nothing to advise."""
    _make_file(tmp_path / "a/b/c/model.safetensors", 1024)
    assert check_command.weights_subdir(tmp_path) == "a/b/c"

    jax = tmp_path / "jax"
    _make_file(jax / "run/params/ocdbt.process_0/d/0001", 1024)
    assert check_command.weights_subdir(jax) == "run"

    # "nothing was exported" and "it is already at the top" are different
    # answers, and `train` acts on the difference -- see the tests below.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert check_command.weights_subdir(empty) is None
    assert check_command.nesting_advice(empty) == []

    flat = tmp_path / "flat"
    _make_file(flat / "model.safetensors", 1024)
    assert check_command.weights_subdir(flat) == ""


# ─── train: what the run actually produced ───────────────────


def test_train_says_nothing_was_exported_when_there_are_no_weights(
    tmp_path: Path,
) -> None:
    """The state the bundled strategies leave behind: they exercise the
    pipeline, they do not train, and they export no checkpoint.

    The old text told every miner to "merge the adapter into the π0.5 base",
    which is wrong twice: the LingBot competitions use no LoRA at all, and
    nothing merges anything anywhere (task `cli-merge-decision`).
    """
    (tmp_path / "metrics.json").write_text("{}", encoding="utf-8")

    lines = "\n".join(train_command.export_advice(tmp_path, 7))

    assert "no model weights" in lines
    assert "merge" in lines  # says there is none, rather than telling them to
    assert "openroboto check" in lines
    # must not send them on to spend money
    assert "openroboto submit" not in lines


def test_train_names_the_nested_directory_the_vendor_export_produces(
    tmp_path: Path,
) -> None:
    """The expensive one: the official LingBot export lands three levels down,
    one deeper than the evaluator searches, so an unchanged upload is silently
    unevaluable. Catching it here is the whole point of the task -- `check`
    catches it too, but a miner can go straight to `submit`.
    """
    nested = "checkpoints/global_step_50000/hf_ckpt"
    _make_file(tmp_path / nested / "model-00001-of-00006.safetensors", 1024)

    lines = "\n".join(train_command.export_advice(tmp_path, 7))

    # the miner has to be able to copy the line, not decode "invalid layout"
    assert f"openroboto check {tmp_path / nested}" in lines
    assert f"openroboto submit --round 7 --output-dir {tmp_path / nested}" in lines


def test_train_points_at_check_when_the_checkpoint_is_at_the_top(
    tmp_path: Path,
) -> None:
    """The good case still stops at `check`: the verdict is the protocol
    package's, not this command's."""
    _make_file(tmp_path / "model.safetensors", 1024)

    lines = "\n".join(train_command.export_advice(tmp_path, 7))

    assert f"openroboto check {tmp_path}" in lines
    assert "openroboto submit --round 7" in lines
    assert "⚠️" not in lines


def test_check_on_missing_directory_returns_error(tmp_path: Path) -> None:
    args = argparse.Namespace(path=str(tmp_path / "nope"), round=0, config="miner.yaml")
    assert check_command.run(args) == 1


# ─── check: which competition's rules ────────────────────────
#
# The tree below is the vendor's own post-trained artifact,
# `robbyant/lingbot-vla-v2-6b-robotwin`, file for file: the same list is pinned in
# the protocol package's golden vectors as `LINGBOT_POST_TRAINED_TREE`. It is what
# a LingBot miner's training output looks like, because the same script wrote
# both -- which is why "the miner did nothing wrong" and "the evaluator finds no
# weights" are true at the same time.

HF_CKPT = "checkpoints/global_step_50000/hf_ckpt"


def _nested_report() -> FormatReport:
    """What both rule books return for a checkpoint buried past the depth the
    evaluator searches: admitted, with the warning that costs the most."""
    return FormatReport(
        kind=CheckpointKind.PYTORCH,
        errors=(),
        warnings=(
            FormatIssue(
                FormatIssueCode.NESTED_TOO_DEEP,
                "the checkpoint is nested 3 levels deep; the evaluator only "
                "searches 2 levels below the repo root",
            ),
        ),
        counted_size_bytes=25_503_889_124,
    )


def _lingbot_rules(monkeypatch: pytest.MonkeyPatch, checker: Any) -> None:
    """Install `checker` as the LingBot rule book: the routing is what is under
    test here, never the rules themselves -- those are the protocol package's,
    and its golden vectors pin what they say.

    Everything else -- the layout class, the file-name constants -- comes from
    the installed package. There used to be stand-ins here, because the rules
    shipped in a release later than the pin; since the pin moved to 0.7.0 they
    are real, and standing them in now would only hide a pin that drifted back.
    """
    monkeypatch.setattr(check_command.model_format, "check_lingbot_layout", checker)


#: One real tensor name per prefix the LingBot rules require, plus the six shards
#: they live in. Copied from the reference checkpoint's index (1708 tensors in
#: total; the rules do not need the other 1700 and pasting them would hide these).
LINGBOT_WEIGHT_MAP = {
    "model.action_in_proj.weight": "model-00006-of-00006.safetensors",
    "model.action_out_proj.weight": "model-00006-of-00006.safetensors",
    "model.state_proj.weight": "model-00006-of-00006.safetensors",
    "model.qwenvl_with_expert.qwen_expert.model.norm.weight": (
        "model-00006-of-00006.safetensors"
    ),
    **{
        f"model.qwenvl_with_expert.qwenvl.model.language_model.layers.{i}."
        "self_attn.q_proj.weight": f"model-0000{i + 1}-of-00006.safetensors"
        for i in range(5)
    },
}

#: What a competition row would carry. Three cameras and seven joint fields are
#: the shape of the vendor's `configs/vla/robotwin/robotwin.yaml`.
LINGBOT_COMPETITION = {
    "adapter": "sim_lingbot",
    "params": {
        "format": {
            "cameras": ["camera_top", "camera_wrist_left", "camera_wrist_right"],
            "joints": [f"joint_{i}" for i in range(6)] + ["gripper"],
        }
    },
}


def _official_lingbot_tree(root: Path) -> None:
    """Write the vendor's post-trained artifact, sizes and all."""
    _make_file(root / ".gitattributes", 1797)
    _make_file(root / "README.md", 2227)
    _make_file(root / "lingbotvla_cli.yaml", 1024)
    _make_file(root / "assets/lingbot_vla2_framework.png", 1178043)
    for index, size in enumerate(
        (4987151072, 4985113408, 4928593216, 4990740540, 4990095864, 622195024), 1
    ):
        _make_file(root / HF_CKPT / f"model-0000{index}-of-00006.safetensors", size)
    for name, size in (
        ("added_tokens.json", 707),
        ("config.json", 31),
        ("preprocessor_config.json", 782),
        ("special_tokens_map.json", 613),
        ("tokenizer.json", 11422654),
        ("tokenizer_config.json", 5472),
        ("video_preprocessor_config.json", 817),
        ("vocab.json", 2776833),
    ):
        _make_file(root / HF_CKPT / name, size)
    (root / HF_CKPT / "model.safetensors.index.json").write_text(
        json.dumps(
            {"metadata": {"total_size": 25503889124}, "weight_map": LINGBOT_WEIGHT_MAP}
        ),
        encoding="utf-8",
    )


def _lingbot_settings() -> Settings:
    return Settings.from_mapping({"competition": LINGBOT_COMPETITION})


def _lingbot_layout() -> Any:
    """The real `LingbotLayout` the command builds for that competition -- built
    the way production builds it, not assembled here field by field."""
    return check_command.resolve_layout(_lingbot_settings())


def test_check_sends_a_lingbot_competition_to_the_lingbot_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The bug this dispatch exists for: the vendor's own artifact, judged by
    the π0.5 rules, comes back `missing_weights` + `missing_norm_stats` -- two
    complaints about files a LingBot checkpoint is not supposed to have, and no
    mention of the one thing that is actually wrong with it.

    What is asserted here is the routing, not the rules: which protocol function
    is called, with which file list, which competition parameters, and which
    weight map. The rules themselves are the protocol package's, and its golden
    vectors pin what they say about this exact tree.
    """
    _official_lingbot_tree(tmp_path)
    calls: list[tuple[Any, ...]] = []

    def recorder(files: Any, layout: Any, **kwargs: Any) -> Any:
        calls.append((files, layout, kwargs))
        return _nested_report()

    _lingbot_rules(monkeypatch, recorder)

    layout = check_command.resolve_layout(_lingbot_settings())
    report = check_command.check_directory(tmp_path, layout=layout)

    assert len(calls) == 1
    files, sent_layout, kwargs = calls[0]
    paths = {file.path for file in files}
    assert f"{HF_CKPT}/model-00001-of-00006.safetensors" in paths
    assert f"{HF_CKPT}/model.safetensors.index.json" in paths
    # competition parameters reach the layout; file names fall back to the
    # protocol package's constants rather than to a literal spelled out here
    assert sent_layout.camera_names == (
        "camera_top",
        "camera_wrist_left",
        "camera_wrist_right",
    )
    assert len(sent_layout.joint_field_names) == 7
    assert sent_layout.weights_index_file == "model.safetensors.index.json"
    # the descriptor sits at the repo root of the official artifact, but the fix
    # for the nesting is to upload the subdirectory -- requiring both at once
    # would be two contradictory instructions, so the rule stays off by default
    assert sent_layout.cli_config_file is None
    # the index is read, so the shard and tensor rules really run
    assert kwargs["weight_map"] == LINGBOT_WEIGHT_MAP
    assert [issue.code for issue in report.warnings] == ["nested_too_deep"]


def test_check_names_the_hf_ckpt_directory_of_the_official_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The advice a LingBot miner gets, over the real official layout.

    "Your structure is invalid" is not something anyone can act on. This has to
    name their own directory and hand them a line they can run, because the
    alternative is that they upload the tree unchanged, pass admission, burn,
    and score nothing.
    """
    _official_lingbot_tree(tmp_path)
    assert check_command.weights_subdir(tmp_path) == HF_CKPT

    exit_code = check_command.report_result(
        tmp_path, _nested_report(), layout=_lingbot_layout()
    )
    assert exit_code == 1

    out = capsys.readouterr().out
    assert "rules: LingBot-VLA 2.0" in out
    assert f"openroboto submit --output-dir {tmp_path / HF_CKPT}" in out
    assert "not refunded" in out


def test_check_refuses_when_the_installed_protocol_cannot_judge_this_competition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 The LingBot rules ship in a protocol release newer than some
    environments hold -- a miner who never upgraded, or one whose environment
    drifted off the pin, which is why `openroboto doctor` exists.

    The one thing that must never happen is a silent fall back to the π0.5
    rules: the miner would be told "no model weights found" about a checkpoint
    that is fine, would go looking for a fault that is not there, and would
    never see the nesting warning that is the one costing money.

    `delattr` raises if the name is not there, and that is deliberate: while the
    pin was 0.6.0 there was nothing to remove, so this test took the refusal
    branch without removing anything and would have stayed green even if the
    fall back had been read back in. Since the pin moved to 0.7.0 it removes
    rules that are really present, which is the only version of this test that
    proves anything.
    """
    for name in ("LingbotLayout", "check_lingbot_layout"):
        monkeypatch.delattr(check_command.model_format, name)

    with pytest.raises(ConfigError) as excinfo:
        check_command.resolve_layout(_lingbot_settings())

    message = str(excinfo.value)
    assert "pip install -U openroboto" in message
    assert "openroboto-protocol" in message
    # naming the refusal, so nobody later reads the fall back back in
    assert "openpi" in message


def test_check_builds_the_lingbot_layout_out_of_the_pinned_protocol(
    tmp_path: Path,
) -> None:
    """The other half of the capability detection: on the pinned version the
    rules are *there*, and what gets built is the package's own class.

    Without this half, a pin that slipped back to a release without the LingBot
    rules would look like "the refusal works" rather than "every LingBot miner
    is now refused".
    """
    layout = _lingbot_layout()
    assert isinstance(layout, check_command.model_format.LingbotLayout)
    # file names fall back to the package's constants, never to a literal
    # spelled out in this repository -- that is how two copies start drifting
    assert (
        layout.model_config_file == check_command.model_format.LINGBOT_MODEL_CONFIG_FILE
    )
    assert (
        layout.weights_index_file
        == check_command.model_format.LINGBOT_WEIGHTS_INDEX_FILE
    )
    # and the real rules accept the argument shape this command sends them
    _official_lingbot_tree(tmp_path)
    report = check_command.check_directory(tmp_path, layout=layout)
    assert report.ok  # the vendor's own artifact passes admission ...
    assert [issue.code for issue in report.warnings] == ["nested_too_deep"]  # ... and
    # still scores nothing, which is the whole reason this command prints warnings


def test_check_keeps_judging_an_old_config_by_the_pi05_rules(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A miner.yaml written before competitions existed, and a directory with no
    config at all. Upgrading the client must not change one verdict for someone
    who changed nothing."""
    assert check_command.resolve_layout(Settings()) is None
    assert check_command.resolve_layout(Settings.from_mapping({})) is None
    absent = check_command.competition_settings(str(tmp_path / "absent.yaml"))
    assert absent.competition_adapter == ""

    _make_file(tmp_path / "model.safetensors", BIG_ENOUGH)
    _make_file(tmp_path / "assets/physical-intelligence/libero/norm_stats.json", 1024)
    args = argparse.Namespace(
        path=str(tmp_path), round=0, config=str(tmp_path / "absent.yaml")
    )
    assert check_command.run(args) == 0
    assert "rules: π0.5 (openpi)" in capsys.readouterr().out


def test_check_reads_the_competition_out_of_miner_yaml(tmp_path: Path) -> None:
    """The dispatch is driven by the config file, not by a flag: which
    competition you mine is a property of the workspace, not something retyped
    on every command."""
    config = tmp_path / "miner.yaml"
    config.write_text(
        yaml.safe_dump({"competition": LINGBOT_COMPETITION}), encoding="utf-8"
    )
    settings = check_command.competition_settings(str(config))
    assert settings.competition_adapter == "sim_lingbot"


def test_check_says_so_when_the_weight_index_cannot_be_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without the index the protocol package does not evaluate the shard and
    tensor rules at all, while the subnet's admission -- which reads the same
    file -- does. Passing here and being rejected afterwards is the expensive
    order, so the gap is printed rather than swallowed."""
    _official_lingbot_tree(tmp_path)
    index = tmp_path / HF_CKPT / "model.safetensors.index.json"

    index.write_text("{ not json", encoding="utf-8")
    assert check_command.read_weight_map(tmp_path, _lingbot_layout()) is None
    assert "shard and tensor rules were not checked" in capsys.readouterr().out

    index.write_text(json.dumps({"weight_map": "not a map"}), encoding="utf-8")
    assert check_command.read_weight_map(tmp_path, _lingbot_layout()) is None
    assert "shard and tensor rules were not checked" in capsys.readouterr().out

    # no index at all is a different story: the rules that need it say so
    # themselves ("no LingBot-VLA weights found"), so this stays quiet
    index.unlink()
    assert check_command.read_weight_map(tmp_path, _lingbot_layout()) is None
    assert capsys.readouterr().out == ""


# ─── build ───────────────────────────────────────────────────


def test_build_prefers_local_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "openpi-runner").mkdir()
    assert build_command.resolve_context() == "openpi-runner"


def test_build_falls_back_to_the_packaged_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no local directory, use the image definition **inside the package** -- no
    clone and no network access required from the miner.

    This replaced the earlier test for "fall back to the docker git remote context".
    That path simply does not work for a miner who installed via pip: the repository is
    private until launch, and the anonymous fetch of `docker build <git-url>` returns
    401. It also pinned `#main`, so anyone on a fixed CLI version would build with
    main's image definition, which does not match the fixed container interface of
    red line #2.
    """
    monkeypatch.chdir(tmp_path)
    context = Path(build_command.resolve_context())

    assert not context.is_absolute() or context.is_dir(), "the context must exist"
    assert (context / "Dockerfile").is_file(), f"no Dockerfile inside {context}"
    assert "github.com" not in str(context), (
        "must not depend on the remote repository any more"
    )


def test_packaged_runner_context_ships_a_self_contained_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The packaged context must be self-contained: every file the Dockerfile COPYs
    has to be present.

    A missing file shows up as `docker build` failing on the miner's machine while
    everything here stays green -- the completeness of a build context cannot be
    guaranteed by code review.
    """
    from openroboto import runner_context

    context = runner_context()
    dockerfile = context / "Dockerfile"
    assert dockerfile.is_file()

    copied = [
        line.split()[1]
        for line in dockerfile.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("COPY ")
    ]
    assert copied, "the Dockerfile has no COPY at all? then this context is pointless"
    for name in copied:
        assert (context / name).is_file(), (
            f"the Dockerfile COPYs {name}, but the package does not contain it"
        )


def test_build_command_assembly() -> None:
    assert build_command.build_command("img", "ctx") == [
        "docker", "build", "-t", "img", "ctx",
    ]  # fmt: skip
    assert build_command.build_command("img", "ctx", no_cache=True) == [
        "docker", "build", "-t", "img", "--no-cache", "ctx",
    ]  # fmt: skip


# ─── build / train: which image, and whether there is one ────


def _competition_config(tmp_path: Path, **section: Any) -> str:
    """A `miner.yaml` for one competition, written the way `init` writes it."""
    config = tmp_path / "miner.yaml"
    config.write_text(
        yaml.safe_dump({"competition": section}, allow_unicode=True), encoding="utf-8"
    )
    return str(config)


def test_build_uses_the_image_this_competition_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OPENPI_RUNNER_IMAGE", raising=False)
    config = _competition_config(
        tmp_path,
        track="sim",
        seq=3,
        adapter="sim_openpi",
        params={"training": {"image": "openpi-runner:1.4"}},
    )
    args = argparse.Namespace(
        config=config, context="", image="", no_cache=False, dry_run=True
    )
    assert build_command.run(args) == 0
    assert "openpi-runner:1.4" in capsys.readouterr().out


def test_build_will_not_fill_a_competitions_image_name_with_another_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 The name comes from the competition and the contents come from a
    build context, with no step anywhere able to tell the two apart: `docker
    images` lists whatever was built, `doctor` calls it ready and `train` runs
    it.

    Two contexts now ship (`runner/` and `runner/lingbot/`), so this is no
    longer the "only context installs π0.5" case -- and the refusal has to
    survive that. `training` is a claim that `openroboto train` drives the
    image to a checkpoint; `real_xarm6` has no context of its own, so building
    silently here would hand back an image built out of somebody else's base
    that the next command refuses to touch.

    ⚠️ This used to use `sim_lingbot`, which moved to `DOCKER` on 2026-08-26
    once `scripts/verify_lingbot_runner.py` ran green on a card. The property
    is about `training=UNAVAILABLE`, not about LingBot -- `real_xarm6` keeps
    the exact shape (a competition that names an image this client must not fill).

    ⚠️ The season here names its base model. Without that key there is nothing to
    build even for a miner with a GPU, and the refusal drops the `--context` hint
    -- the case below. This one is the season that *has* decided.
    """
    monkeypatch.delenv("OPENPI_RUNNER_IMAGE", raising=False)
    built: list[Any] = []
    monkeypatch.setattr(
        build_command.subprocess, "run", lambda *a, **k: built.append(a)
    )
    config = _competition_config(
        tmp_path,
        track="real",
        seq=2,
        adapter="real_xarm6",
        base_model_family="lingbot_vla",
        params={"training": {"image": "xarm6-runner:1.0"}},
    )
    args = argparse.Namespace(
        config=config, context="", image="", no_cache=False, dry_run=False
    )

    assert build_command.run(args) == 1
    assert built == []
    err = capsys.readouterr().err
    assert "has not been released yet" in err
    # And it says how to build it anyway, naming the context that does exist --
    # otherwise the only path left is guessing at a directory inside site-packages.
    assert "--context" in err and "runner/lingbot" in err


def test_build_names_the_context_of_the_base_model_the_season_actually_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 Same arm, other base model, other context.

    Before the split this was unreachable: `real_xarm6` carried `LINGBOT` in the
    adapter table, so the hint named LingBot's context no matter what the season
    ran on -- and the plan is to bring xArm 6 up on π0.5 first, so the baked-in
    answer was the wrong one. Nothing could have caught it; the base model is a
    property of the season and that table is keyed by hardware.
    """
    monkeypatch.delenv("OPENPI_RUNNER_IMAGE", raising=False)
    config = _competition_config(
        tmp_path,
        track="real",
        seq=2,
        adapter="real_xarm6",
        base_model_family="openpi",
        params={"training": {"image": "xarm6-runner:1.0"}},
    )
    args = argparse.Namespace(
        config=config, context="", image="", no_cache=False, dry_run=False
    )

    assert build_command.run(args) == 1
    err = capsys.readouterr().err
    assert "--context" in err
    assert "runner/lingbot" not in err, "named LingBot's context for a π0.5 season"


def test_build_refuses_without_naming_a_context_it_cannot_pick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A season that has not named a base model gets the refusal **without** the
    "build it yourself from this directory" hint.

    There is no directory to name, and naming one anyway is the exact failure the
    refusal exists to prevent: an image under this competition's name filled with
    somebody else's base model, which nothing downstream can tell apart.
    """
    monkeypatch.delenv("OPENPI_RUNNER_IMAGE", raising=False)
    config = _competition_config(
        tmp_path,
        track="real",
        seq=2,
        adapter="real_xarm6",
        params={"training": {"image": "xarm6-runner:1.0"}},
    )
    args = argparse.Namespace(
        config=config, context="", image="", no_cache=False, dry_run=False
    )

    assert build_command.run(args) == 1
    err = capsys.readouterr().err
    assert "has not been released yet" in err
    assert "ships an unverified build context" not in err
    assert "`--context <directory>` builds it" in err


def test_build_still_builds_an_image_definition_you_brought_yourself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--context` is an explicit act, which is the whole difference: the miner
    chose the contents instead of defaulting into the only ones on hand."""
    monkeypatch.delenv("OPENPI_RUNNER_IMAGE", raising=False)
    config = _competition_config(
        tmp_path,
        track="sim",
        seq=2,
        adapter="sim_lingbot",
        params={"training": {"image": "lingbot-runner:1.2"}},
    )
    args = argparse.Namespace(
        config=config,
        context=str(tmp_path / "my-lingbot-runner"),
        image="",
        no_cache=False,
        dry_run=True,
    )

    assert build_command.run(args) == 0
    assert "lingbot-runner:1.2" in capsys.readouterr().out


def test_an_explicit_image_beats_the_competitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _competition_config(
        tmp_path, track="real", seq=1, params={"training": {"image": "from-params"}}
    )
    args = argparse.Namespace(
        config=config, context="", image="mine:dev", no_cache=False, dry_run=True
    )
    assert build_command.run(args) == 0
    printed = capsys.readouterr().out
    assert "mine:dev" in printed
    assert "from-params" not in printed


def test_the_environment_override_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A miner building their own image sets `OPENPI_RUNNER_IMAGE`, and that has
    always won. Losing to a competition parameter would silently start ignoring
    the image they built."""
    monkeypatch.setenv("OPENPI_RUNNER_IMAGE", "mine:local")
    config = _competition_config(
        tmp_path, track="real", seq=1, params={"training": {"image": "from-params"}}
    )
    args = argparse.Namespace(
        config=config, context="", image="", no_cache=False, dry_run=True
    )
    assert build_command.run(args) == 0
    assert "mine:local" in capsys.readouterr().out


def test_a_config_from_before_competitions_builds_what_it_always_did(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OPENPI_RUNNER_IMAGE", raising=False)
    args = argparse.Namespace(
        config=str(tmp_path / "absent.yaml"),
        context="",
        image="",
        no_cache=False,
        dry_run=True,
    )
    assert build_command.run(args) == 0
    assert DEFAULT_IMAGE in capsys.readouterr().out


@pytest.mark.parametrize("adapter", ["real_xarm6"])
def test_train_refuses_a_competition_whose_training_is_not_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: str
) -> None:
    """🔴 Not a no-op: an empty output directory is something `openroboto check`
    would then deliver a verdict about.

    The failure this prevents is the quiet one: with an image built by an
    earlier release sitting under this competition's name, `docker run`
    succeeds and trains on π0.5, with no error anywhere.

    ⚠️ `sim_lingbot` used to be the second case here, precisely because its
    dataset exists and its image did not -- proof the refusal does not key off
    "is this competition released at all". It ships a verified container as of
    2026-08-26, so it moved to `DOCKER` and out of this list. The parametrize
    stays for the next competition that lands dataset-first; `real_xarm6` has
    neither, which is a weaker case than the one this test was written for.
    """
    called: list[Any] = []
    monkeypatch.setattr(
        train_command, "train_round", lambda **kwargs: called.append(kwargs)
    )
    config = _competition_config(
        tmp_path, track="real", seq=1, adapter=adapter, params={}
    )
    args = argparse.Namespace(
        config=config, output_dir=str(tmp_path / "out"), strategy=""
    )

    assert train_command.run(args) == 1
    assert called == []
    assert not (tmp_path / "out").exists()


# ─── train: the season on disk is the whole input ────────────
#
# `train` used to open control.json before anything else and take the round,
# the status, the dataset and the hyperparameters out of it. One static file
# for a subnet that runs several competitions at once: a LingBot miner was told
# they were on "round 1", handed the π0.5 sample and the π0.5 checkpoint path,
# and nothing on that path could notice.

DATASET = {
    "train": "https://example.invalid/train.json",
    "val": "https://example.invalid/val.json",
}

#: One episode that survives `training/dataset.py::validate_episode`, so the
#: run reaches `docker run` instead of stopping at "training set is empty".
EPISODE = {
    "episode_id": "e1",
    "timestamp": "2026-08-26T00:00:00Z",
    "observation": {"image": [], "wrist_image": [], "state": [0.0]},
    "action": [[0.0]],
    "language_instruction": "pick up the block",
    "license": "MIT",
}


def _train_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    training: dict[str, Any] | None = None,
    hyperparameters: dict[str, Any] | None = None,
    **section: Any,
) -> argparse.Namespace:
    """A workspace mining one season, with `train`'s arguments ready.

    Runs from `tmp_path`: `state/` and the base-model cache are both resolved
    against the working directory.
    """
    monkeypatch.chdir(tmp_path)
    config: dict[str, Any] = {
        "competition": {
            "track": "sim",
            "seq": 7,
            "label": "LingBot-VLA 2.0",
            "adapter": "sim_openpi",
            "status": "active",
            "params": {"training": training if training is not None else {}},
        }
        | section
    }
    if hyperparameters is not None:
        config["training"] = hyperparameters
    (tmp_path / "miner.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
    )
    return argparse.Namespace(
        config=str(tmp_path / "miner.yaml"),
        output_dir=str(tmp_path / "out"),
        strategy="",
    )


def _fake_training(
    monkeypatch: pytest.MonkeyPatch, downloaded: list[str]
) -> list[dict[str, Any]]:
    """Stand in for the download and the container run; record both."""
    ran: list[dict[str, Any]] = []

    def _download(url: str, dest: str) -> str:
        downloaded.append(url)
        Path(dest).write_text("[]", encoding="utf-8")
        return dest

    def _train(**kwargs: Any) -> Any:
        ran.append(kwargs)
        return SimpleNamespace(metrics={"final_loss": 0.5}, proof={})

    monkeypatch.setattr(train_command, "download_dataset", _download)
    monkeypatch.setattr(train_command, "train_round", _train)
    return ran


def test_train_never_opens_control_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The executable half of "train does not touch control.json".

    Blocking `urlopen` catches the whole family at once -- a re-import of
    `fetch_control`, a new HTTP call added later, a helper that reaches for the
    URL "just to check the round". The dataset download is the one call that is
    allowed out, and it is faked above precisely so that the block below is not
    ambiguous.
    """
    monkeypatch.setattr(
        "openroboto.http_client.urlopen",
        lambda *a, **k: pytest.fail("train went to the network"),
    )
    downloaded: list[str] = []
    ran = _fake_training(monkeypatch, downloaded)
    args = _train_workspace(
        tmp_path, monkeypatch, training={"dataset": DATASET, "image": "runner:1.4"}
    )

    assert train_command.run(args) == 0
    assert downloaded == [DATASET["train"], DATASET["val"]]
    assert len(ran) == 1
    # ⚠️ The workspace **does** carry a control.json URL (the environment preset
    # fills one in, and external validators need it to keep answering). The
    # point is that `train` has one right there and still does not open it.
    assert Settings.load(args.config).control_json_url


def test_train_takes_the_round_from_the_season_not_a_subnet_wide_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`competitions.seq` -- the number that differs between two seasons running
    at the same time, which is what control.json's single `round` could not."""
    ran = _fake_training(monkeypatch, [])
    args = _train_workspace(
        tmp_path, monkeypatch, seq=12, training={"dataset": DATASET}
    )

    assert train_command.run(args) == 0
    assert ran[0]["output_dir"] == str(tmp_path / "out" / "round_12")
    assert json.loads((tmp_path / "state" / "round_12.json").read_text())["round"] == 12


def test_train_starts_from_the_checkpoint_this_season_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The starting point, not the baseline. `base_repo` is what the
    leaderboard measures against; this is where the miner's own run begins, and
    for π0.5 they were two different addresses."""
    ran = _fake_training(monkeypatch, [])
    args = _train_workspace(
        tmp_path,
        monkeypatch,
        base_repo="openroboto-ai/pi05-libero-pytorch",
        training={
            "dataset": DATASET,
            "checkpoint": "gs://openpi-assets/checkpoints/pi05_base",
        },
    )

    assert train_command.run(args) == 0
    # `gs://` is swapped for the local cache directory to mount -- the container
    # downloads into it. That branching is unchanged.
    assert ran[0]["checkpoint_path"] == "cache/pi05_base"


def test_train_leaves_the_base_to_the_image_when_the_season_names_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 No π0.5 fallback. `resolve_checkpoint("")` used to substitute the π0.5
    base for every competition, so a LingBot run was handed a path from another
    base model; empty now means `CHECKPOINT_PATH` is not set at all and the
    image uses the base it was built around."""
    ran = _fake_training(monkeypatch, [])
    args = _train_workspace(tmp_path, monkeypatch, training={"dataset": DATASET})

    assert train_command.run(args) == 0
    assert ran[0]["checkpoint_path"] == ""
    assert not (tmp_path / "cache").exists()


def test_train_refuses_a_season_that_has_published_no_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`null` is a real answer and it is not "use the other season's data": the
    run would finish clean on the wrong dataset and cost a fee to find out."""
    ran = _fake_training(monkeypatch, [])
    args = _train_workspace(tmp_path, monkeypatch, training={"image": "runner:1.4"})

    assert train_command.run(args) == 1
    assert ran == []


def test_train_refuses_a_workspace_with_no_season_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no round, no dataset and no base model to guess from -- and
    guessing is what this command stopped doing."""
    monkeypatch.chdir(tmp_path)
    ran = _fake_training(monkeypatch, [])
    (tmp_path / "miner.yaml").write_text("log_level: INFO\n", encoding="utf-8")
    args = argparse.Namespace(
        config=str(tmp_path / "miner.yaml"),
        output_dir=str(tmp_path / "out"),
        strategy="",
    )

    assert train_command.run(args) == 1
    assert ran == []


@pytest.mark.parametrize("status", ["archived", "draft"])
def test_train_refuses_a_season_that_is_not_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """control.json only ever said `active`; a competition row has three words,
    and the two new ones both mean "nowhere to submit this"."""
    ran = _fake_training(monkeypatch, [])
    args = _train_workspace(
        tmp_path, monkeypatch, status=status, training={"dataset": DATASET}
    )

    assert train_command.run(args) == 1
    assert ran == []


def test_the_miners_hyperparameters_reach_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 End to end, `miner.yaml` → `docker run`, with nothing faked in between
    but the process call itself.

    The five environment variable **names** are red line #2 -- a strategy script
    reads `cfg["epochs"]` out of them -- so this asserts the names as literally
    as it asserts the values.
    """
    monkeypatch.delenv("OPENPI_RUNNER_IMAGE", raising=False)
    monkeypatch.setattr(container, "detect_free_gpus", lambda: "")
    monkeypatch.setattr(container, "remove_stale_container", lambda *a, **k: None)
    commands: list[list[str]] = []

    def _run(command: list[str], **kwargs: Any) -> Any:
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(container.subprocess, "run", _run)
    monkeypatch.setattr(
        train_command,
        "download_dataset",
        lambda url, dest: (
            Path(dest).write_text(json.dumps([EPISODE]), encoding="utf-8"),
            dest,
        )[1],
    )
    args = _train_workspace(
        tmp_path,
        monkeypatch,
        training={"dataset": DATASET, "image": "runner:1.4"},
        hyperparameters={
            "epochs": 10,
            "batch_size": 8,
            "learning_rate": 5.0e-5,
            "lora_r": 64,
            "lora_alpha": 128,
        },
    )

    # `final_loss` is missing from a container that printed nothing, so the run
    # is reported as failed -- after the command has been assembled and issued,
    # which is what this test is about.
    train_command.run(args)
    assert len(commands) == 1
    assert "-e" in commands[0]
    passed = [part for part in commands[0] if "=" in part]
    for expected in (
        "EPOCHS=10",
        "BATCH_SIZE=8",
        "LR=5e-05",
        "LORA_R=64",
        "LORA_ALPHA=128",
    ):
        assert expected in passed
    assert commands[0][-1] == "runner:1.4"


def test_the_defaults_reaching_the_container_are_the_old_control_json_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace that touches nothing trains byte for byte as it did when the
    subnet set these five for everybody."""
    ran = _fake_training(monkeypatch, [])
    args = _train_workspace(tmp_path, monkeypatch, training={"dataset": DATASET})

    assert train_command.run(args) == 0
    assert ran[0]["params"] == TrainParams(
        epochs=3, batch_size=4, learning_rate=1e-4, lora_r=32, lora_alpha=64
    )


def test_train_runs_the_same_image_build_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Building one image and training in another is the kind of mismatch that
    only shows up as a training run that behaves oddly."""
    monkeypatch.delenv("OPENPI_RUNNER_IMAGE", raising=False)
    config = _competition_config(
        tmp_path,
        track="sim",
        seq=3,
        adapter="sim_openpi",
        params={"training": {"image": "openpi-runner:1.4"}},
    )
    assert build_command.competition_image(config) == "openpi-runner:1.4"
    assert runner_image(build_command.competition_image(config)) == "openpi-runner:1.4"


# ─── round_state ─────────────────────────────────────────────


def test_state_round_trip_and_resolution(tmp_path: Path) -> None:
    save_state(3, {"step": "training", "status": "completed"}, base=tmp_path)
    save_state(4, {"step": "prep", "status": "in_progress"}, base=tmp_path)

    assert load_state(3, base=tmp_path)["step"] == "training"
    assert is_step_done(load_state(3, base=tmp_path), "training")
    # round 4 did not finish, so auto-resolution must fall back to 3
    assert resolve_round(0, base=tmp_path) == 3
    assert resolve_round(4, base=tmp_path) == 4


def test_resolve_round_refuses_to_guess(tmp_path: Path) -> None:
    with pytest.raises(StateError) as excinfo:
        resolve_round(0, base=tmp_path)
    assert "--round" in str(excinfo.value)


def test_resolve_output_dir_uses_recorded_path(tmp_path: Path) -> None:
    save_state(1, {"round_output": "/somewhere/round_1"}, base=tmp_path)
    assert resolve_output_dir(1, base=tmp_path) == "/somewhere/round_1"
    assert resolve_output_dir(2, base=tmp_path).endswith("round_2")


def test_corrupt_state_file_reads_as_empty(tmp_path: Path) -> None:
    (tmp_path / "round_1.json").write_text("{ 坏掉的 json", encoding="utf-8")
    assert load_state(1, base=tmp_path) == {}


# ─── preflight ───────────────────────────────────────────────


def _ready_state() -> dict[str, Any]:
    return {
        "hf_repo_id": "someone/pi05-abcdefghijkl",
        "hf_url": "https://huggingface.co/someone/pi05-abcdefghijkl/commit/" + "a" * 40,
        "hf_commit": "a" * 40,
        "hotkey_ss58": "5" + "M" * 47,
    }


def test_preflight_passes_on_a_complete_state() -> None:
    assert check_announce_ready(_ready_state(), 1) == []


def test_preflight_names_every_missing_field() -> None:
    reasons = check_announce_ready({}, 1)
    joined = "\n".join(reasons)
    assert "hf_repo_id" in joined
    assert "hf_url" in joined
    assert "hf_commit" in joined
    assert "hotkey_ss58" in joined


def test_preflight_rejects_a_short_commit_sha() -> None:
    state = _ready_state()
    state["hf_commit"] = "abc"
    assert any("hf_commit" in reason for reason in check_announce_ready(state, 1))


# ─── burn window (pure decision, boundary copied from the backend) ────────────


def test_burn_window_boundary_matches_the_backend_exactly() -> None:
    """The backend only rejects on `abs(diff) > window`. These three cases pin the edge.

    One notch stricter blocks submissions the backend would have accepted; one notch
    looser and the miner burns for nothing.
    """
    # exactly equal to the window -> allowed (the backend uses `>`, not `>=`).
    # only "not blocked" is asserted here; the near-the-edge warning fires at the same
    # time, but that belongs to another test.
    assert check_burn_window(1_000, 1_050, 50)[0] == ""

    # one over -> blocked
    blocked, _ = check_burn_window(1_000, 1_051, 50)
    # assert on the numbers, not the prose: wording gets translated and rewritten,
    # while 51 and 50 are the facts of this decision.
    assert "51" in blocked and "50" in blocked

    # symmetry: a burn after the commit counts as distance too
    blocked_reverse, _ = check_burn_window(1_051, 1_000, 50)
    # the symmetry assertion is about "both directions are blocked" and "the distance
    # comes out the same", not about the full sentence -- wording changes, the
    # semantics of abs() do not.
    assert blocked_reverse, (
        "a burn after the commit is equally out of window and must also be blocked"
    )
    assert "51" in blocked_reverse


def test_burn_window_skips_when_either_block_is_unknown() -> None:
    """With a block of 0 the backend skips the whole section, so we skip it too -- we
    must not be stricter than the backend."""
    assert check_burn_window(0, 1_000, 50) == ("", "")
    assert check_burn_window(1_000, 0, 50) == ("", "")


def test_burn_window_warns_before_the_edge_without_blocking() -> None:
    """Close to the edge must warn (the commitment still needs a few blocks to be
    included), but it **must not** count towards the blocking decision."""
    blocked, warning = check_burn_window(1_000, 1_048, 50)
    assert blocked == ""  # 48 < 50, the backend will accept it
    assert warning, "being close to the edge must produce a warning"
    assert "48" in warning and "50" in warning


def test_preflight_size_estimate_includes_the_block_hash() -> None:
    """The old preflight treated block_hash as an empty string and so under-counted by
    64 bytes every time -- a borderline repo name would pass preflight, burn the TAO,
    and then blow up at the on-chain step."""
    size = payload_size(_ready_state(), 1)
    assert size > 64  # the placeholder hash really is part of the estimate
    assert size <= 512


def test_preflight_blocks_an_oversized_repository_name() -> None:
    state = _ready_state()
    state["hf_repo_id"] = "x" * 500
    reasons = check_announce_ready(state, 1)
    assert any("512" in reason for reason in reasons)


# ─── huggingface helpers ─────────────────────────────────────


def test_commit_sha_is_parsed_from_the_commit_url() -> None:
    sha = "b" * 40
    assert commit_sha_from_url(f"https://huggingface.co/u/r/commit/{sha}") == sha
    assert commit_sha_from_url("https://huggingface.co/u/r") == ""


def test_repo_id_follows_the_public_format() -> None:
    settings = Settings.from_mapping({"huggingface": {"username": "kyleab"}})
    address = "5FH32ZXmRZqCuLCS2vhMme6jwznP6NpywGjSqXgcGfvRk2Xp"
    assert build_repo_id(settings, address) == "kyleab/pi05-qXgcGfvRk2Xp"


def test_repo_id_refuses_to_invent_a_fallback() -> None:
    """The old code fell back to the literal `miner` here, so the model was uploaded to
    a repository nobody evaluates."""
    with pytest.raises(ConfigError):
        build_repo_id(Settings(), "")


# ─── status ──────────────────────────────────────────────────


def _history_row(**overrides: Any) -> SubmissionHistoryItem:
    row: dict[str, Any] = {
        "id": 1,
        "task_id": "task-1",
        "uid": 7,
        "hotkey": "5X",
        "hf_repo_id": "miner/model",
        "hf_commit": "c0ffee",
        "commit_block": 1200,
        "commit_block_timestamp": 1_700_000_000,
        "burn_tx_hash": "0xdead",
        "burn_block": 1180,
        "burn_status": "confirmed",
        "block_hash": "0xbeef",
        "eval_status": "done",
    }
    return SubmissionHistoryItem.model_validate({**row, **overrides})


def test_status_normalises_legacy_words() -> None:
    assert (
        status_command.display_status(_history_row(eval_status="done")) == "evaluated"
    )
    assert (
        status_command.display_status(_history_row(eval_status="failed"))
        == "eval_failed"
    )
    assert status_command.display_status(_history_row(eval_status="")) == "?"


def test_status_prints_a_row_without_touching_a_field_that_no_longer_exists(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 Walks the real `run()`, because that is where the breakage was.

    Protocol 0.9.0 dropped `round_num` from both response models while three
    lines in `run()` still read it. Every unit test around this command passed
    -- they all called the small helpers -- and `openroboto status` raised
    `AttributeError` on the first row it tried to print.

    So this one renders an actual row through the actual command. It asserts on
    what a miner needs to identify the submission (`task_id`) rather than on
    the whole line: the wording is for humans and gets rewritten, the fact that
    printing a row does not blow up is the invariant.
    """
    envelope: Any = ListEnvelope[SubmissionHistoryItem].model_validate(
        {
            "data": [_history_row().model_dump(mode="json")],
            "meta": {"request_id": "r-1", "page": _page_meta()},
        }
    )
    empty: Any = ListEnvelope[ScanRejection].model_validate(
        {"data": [], "meta": {"request_id": "r-1", "page": _page_meta()}}
    )
    monkeypatch.setattr(status_command, "fetch_submissions", lambda *a, **k: envelope)
    monkeypatch.setattr(status_command, "fetch_rejections", lambda *a, **k: empty)
    # No workspace on disk -> `say_roster` returns early, which is the shape a
    # miner running `openroboto status --hotkey ...` from anywhere else is in.
    monkeypatch.setattr(status_command, "load_snapshot", lambda _settings: None)

    args = argparse.Namespace(config="nope.yaml", hotkey="5X", round=0, limit=20)
    assert status_command.run(args) == 0

    out = capsys.readouterr().out
    assert "task=task-1" in out
    assert "Submissions (1)" in out


def _page_meta() -> dict[str, Any]:
    return {"total": 1, "limit": 20, "offset": 0, "has_more": False}


def test_status_explains_a_rejection_reason() -> None:
    """The two things a miner needs: a stable error code, and "do I have to burn
    again"."""
    reason = Reason(
        code="BURN_TX_TOO_OLD",
        message="烧的那笔交易太旧了",
        retryable=False,
        source="scan",
    )
    lines = status_command.explain(reason)
    assert "BURN_TX_TOO_OLD" in lines[0]
    assert "Retrying will not give a different result" in lines[1]

    assert status_command.explain(None) == []


def test_status_only_mentions_more_rows_when_there_are_more(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status_command.say_more_hint(False, 3)
    assert capsys.readouterr().out == ""

    status_command.say_more_hint(True, 42)
    assert "42" in capsys.readouterr().out


# ─── doctor ──────────────────────────────────────────────────


def test_doctor_flags_every_field_needed_before_spending() -> None:
    """doctor is the last gate before money is spent, so every missing item must be
    named individually."""
    results = doctor_command.check_settings(Settings())
    failed = {r.name for r in results if not r.ok}
    # assert on the count and the key fields, not on the display names -- display names
    # are for humans and get translated or rewritten.
    assert "netuid" in failed and "hotkey_ss58" in failed
    assert len(failed) == 3, f"an empty config should report 3 items, got {failed}"


def test_doctor_passes_on_a_complete_config() -> None:
    settings = Settings.from_mapping(
        {
            "subnet": {"netuid": 80, "hotkey_ss58": "5" + "M" * 47},
            "huggingface": {"username": "someone", "token": "hf_x"},
        }
    )
    assert all(result.ok for result in doctor_command.check_settings(settings))


def test_doctor_renders_required_and_optional_differently() -> None:
    required = doctor_command.CheckResult("A", False, "missing", required=True)
    optional = doctor_command.CheckResult("B", False, "missing", required=False)
    assert required.render().startswith("❌")
    assert optional.render().startswith("⚠️")


def test_doctor_python_check_matches_the_supported_floor() -> None:
    assert doctor_command.MIN_PYTHON == (3, 11)  # miners are on 3.11, not 3.12
    assert doctor_command.check_python().ok


def test_doctor_reads_the_fee_from_the_season_not_the_subnet_rate() -> None:
    """The season's own `params.fee`, never `settings.burn_rate_tao`.

    That field is control.json's subnet-wide rate, and the subnet runs several
    seasons at once: on `real/1` it reads 0.1 while that season charges 2 TAO.
    A wallet holding 0.5 was ticked green here and ran out at `submit` -- after
    the upload had already gone out.
    """
    settings = Settings.from_mapping(
        {
            "competition": {
                "track": "real",
                "seq": 1,
                "label": "xArm 6",
                "status": "active",
                "params": {
                    "fee": {"kind": "transfer", "amount_tao": 2, "coldkey": "5x"}
                },
            },
            "payment": {"burn_rate_tao": 0.1},
        }
    )
    result = doctor_command.check_competition(settings)
    assert result.ok
    assert "2" in result.detail and "transfer" in result.detail
    assert "0.1" not in result.detail


def test_doctor_never_opens_the_network_to_say_which_season_this_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The season is written into `miner.yaml` by `init`, so reading it is
    offline work. It used to be a `control.json` fetch, which made an
    unreachable host look like a broken workspace."""
    import urllib.request

    def _refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("check_competition must not open the network")

    monkeypatch.setattr(urllib.request, "urlopen", _refuse)

    settings = Settings.from_mapping(
        {
            "competition": {
                "track": "sim",
                "seq": 2,
                "label": "LingBot-VLA 2.0",
                "status": "active",
                "params": {"fee": {"kind": "burn", "amount_tao": 0.1, "coldkey": None}},
            },
            "urls": {"control_json": "https://example.invalid/control.json"},
        }
    )
    result = doctor_command.check_competition(settings)
    assert result.ok
    assert "sim/2" in result.detail


def test_doctor_balance_check_does_not_crash_on_an_unknown_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an unknown rate we must not compare the balance against `None`, and must
    not pretend it is sufficient.

    doctor is the self-check entry point before money is spent, so "could not
    determine" has to be reported honestly as such.
    """

    class _Subtensor:
        def get_balance(self, address: str) -> float:
            return 5.0

        def close(self) -> None:
            pass

    # `check_wallet` imports lazily, so the patch has to go on the source module.
    import openroboto.chain as chain_module

    monkeypatch.setattr(chain_module, "get_subtensor", lambda network: _Subtensor())
    monkeypatch.setattr(doctor_command, "_coldkey_address", lambda settings: "5abc")

    settings = Settings.from_mapping({"subnet": {"netuid": 80}})
    assert settings.burn_rate_tao is None

    result = doctor_command.check_wallet(settings)
    assert result.ok is False
    assert "unknown" in result.detail.lower()


def test_doctor_reports_on_the_image_train_would_actually_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It used to look up the π0.5 default no matter what the competition named,
    so the line said `robot-train-openpi:latest ready` about an image `train`
    was never going to touch."""
    monkeypatch.delenv("OPENPI_RUNNER_IMAGE", raising=False)
    monkeypatch.setattr(doctor_command.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(doctor_command, "_run", lambda command: "sha256:abc")
    config = _competition_config(
        tmp_path,
        track="sim",
        seq=3,
        adapter="sim_openpi",
        params={"training": {"image": "openpi-runner:1.4"}},
    )

    result = doctor_command.check_image(config)
    assert result.ok
    assert "openpi-runner:1.4" in result.detail


def test_doctor_does_not_call_a_foreign_image_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The competition names the image, this client cannot build it, and yet
    something with that name is on the machine -- an earlier release built it out
    of another competition's context. "ready" is the one word that must not
    appear: it is the last place the name and the contents can be told apart.

    ⚠️ Was `sim_lingbot` until 2026-08-26, when a green
    `scripts/verify_lingbot_runner.py` moved it to `DOCKER`. `real_xarm6` is
    the same shape and the property is unchanged -- it is about
    `training=UNAVAILABLE`, not about any one base model.
    """
    monkeypatch.delenv("OPENPI_RUNNER_IMAGE", raising=False)
    monkeypatch.setattr(doctor_command.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(doctor_command, "_run", lambda command: "sha256:abc")
    config = _competition_config(
        tmp_path,
        track="real",
        seq=2,
        adapter="real_xarm6",
        params={"training": {"image": "xarm6-runner:1.0"}},
    )

    result = doctor_command.check_image(config)
    assert result.ok is False
    assert "ready" not in result.detail
    assert "xarm6-runner:1.0" in result.detail
    # ...and it does not fail the whole run: `openroboto build` refuses this
    # competition on purpose, so there is nothing here for the miner to fix.
    assert result.required is False


def test_doctor_without_a_config_checks_what_it_always_did(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENPI_RUNNER_IMAGE", raising=False)
    monkeypatch.setattr(doctor_command.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(doctor_command, "_run", lambda command: "sha256:abc")

    result = doctor_command.check_image()
    assert result.ok
    assert DEFAULT_IMAGE in result.detail


def test_doctor_protocol_check_passes_on_the_pinned_version() -> None:
    """Happy path: whatever the test environment installed came from the same
    pyproject, so the installed version and the declared pin must agree.

    This deliberately asserts against the real metadata rather than a stubbed
    pair. The bug this check guards against is "the two numbers drifted apart
    and nobody noticed", and a stub on both sides would drift right along with
    it.
    """
    result = doctor_command.check_protocol()
    assert result.ok, result.detail
    assert doctor_command.pinned_protocol_version() in result.detail


def test_doctor_protocol_check_flags_a_mismatch_as_a_money_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A miner who ran `pip install -U openroboto-protocol` must be stopped
    here, not after the burn: the fix line has to name the exact version to go
    back to."""
    monkeypatch.setattr(doctor_command, "version", lambda name: "9.9.9")
    monkeypatch.setattr(doctor_command, "pinned_protocol_version", lambda: "0.6.0")

    result = doctor_command.check_protocol()
    assert result.ok is False
    assert result.required is True  # a required failure, so doctor exits non-zero
    assert "9.9.9" in result.detail and "0.6.0" in result.detail
    assert "0.6.0" in result.fix


def test_doctor_protocol_check_says_cannot_tell_when_there_is_no_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No declaration to compare against is "cannot tell", not "mismatch" --
    reporting a failure here would train developers to ignore the item."""
    monkeypatch.setattr(doctor_command, "version", lambda name: "0.6.0")
    monkeypatch.setattr(doctor_command, "pinned_protocol_version", lambda: None)

    result = doctor_command.check_protocol()
    assert result.ok and result.required is False


def test_doctor_reads_the_pin_out_of_metadata_not_a_second_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The expected version comes from the installed metadata. Names are
    compared PEP 503 style, and non-`==` entries are skipped rather than
    mistaken for a pin."""
    monkeypatch.setattr(
        doctor_command,
        "requires",
        lambda name: ["pyyaml>=6.0", "openroboto_protocol==1.2.3", "bittensor>=10.5"],
    )
    assert doctor_command.pinned_protocol_version() == "1.2.3"


def _roster_settings() -> Settings:
    return Settings.from_mapping(
        {
            "backend": {"url": "http://backend.test"},
            "competition": {
                "track": "real",
                "seq": 1,
                "label": "xArm 6 第一届",
                "adapter": "real_xarm6",
                "params": {},
            },
        }
    )


def _entry(hotkey: str, **overrides: Any) -> RosterEntry:
    return RosterEntry.model_validate(
        {
            "hotkey": hotkey,
            "uid": 23,
            "hf_repo_id": "miner/model",
            "payment_status": "paid",
            "hf_access_status": "verified",
            "counts_as_submitted": True,
            **overrides,
        }
    )


def _roster(
    monkeypatch: pytest.MonkeyPatch,
    entries: list[RosterEntry],
    *,
    total: int | None = None,
) -> None:
    monkeypatch.setattr(
        status_command,
        "fetch_competitions",
        lambda *a, **k: SimpleNamespace(data=[_row()]),
    )
    page = SimpleNamespace(
        total=len(entries) if total is None else total,
        limit=1000,
        offset=0,
        has_more=total is not None and total > len(entries),
    )
    monkeypatch.setattr(
        status_command,
        "fetch_roster",
        lambda *a, **k: SimpleNamespace(data=entries, meta=SimpleNamespace(page=page)),
    )


def test_status_counts_the_place_in_the_order_entries_were_joined(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The list arrives newest first; the queue is worked oldest first. Printing
    the index as it arrives would tell the first entrant they are last."""
    mine = "5Hb5muCtV2SqiVkZf1exftoccKrbeYsDf67xZpmSiYEDjmz7"
    _roster(monkeypatch, [_entry("5Other"), _entry(mine), _entry("5Third")])

    status_command.say_roster(_roster_settings(), mine)

    printed = capsys.readouterr().out
    assert "#2 of 3" in printed
    assert mine[:8] in printed
    assert "payment: paid" in printed


def test_status_says_nothing_for_a_config_from_before_competitions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        status_command,
        "fetch_roster",
        lambda *a, **k: pytest.fail("asked for an entry list without a competition"),
    )
    status_command.say_roster(Settings(), "5X")
    assert capsys.readouterr().out == ""


def test_status_does_not_print_an_empty_entry_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _roster(monkeypatch, [_entry("5Other")])
    status_command.say_roster(_roster_settings(), "5Mine")
    assert "not on the entry list" in capsys.readouterr().out


def test_a_backend_without_the_endpoint_does_not_take_status_down(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 This is the troubleshooting command. A 404 here must not cost the
    miner the two sections they actually came for."""
    _roster(monkeypatch, [])
    monkeypatch.setattr(
        status_command,
        "fetch_roster",
        lambda *a, **k: (_ for _ in ()).throw(BackendError("404")),
    )

    status_command.say_roster(_roster_settings(), "5Mine")

    assert "cannot answer entry-list queries" in capsys.readouterr().out


def test_status_says_so_rather_than_computing_a_place_from_part_of_the_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mine = "5Hb5muCtV2SqiVkZ"
    _roster(monkeypatch, [_entry(mine)], total=1200)
    status_command.say_roster(_roster_settings(), mine)
    assert "most recent" in capsys.readouterr().out
