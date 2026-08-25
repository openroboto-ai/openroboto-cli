"""Command layer: init / check / build / status / round_state / preflight.

All pure local logic: no network, no GPU, no chain.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import pytest
from openroboto_protocol.schemas import Reason, SubmissionHistoryItem

from openroboto.commands import build as build_command
from openroboto.commands import check as check_command
from openroboto.commands import doctor as doctor_command
from openroboto.commands import init as init_command
from openroboto.commands import status as status_command
from openroboto.config import ConfigError, ControlFetchError, Settings
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

BIG_ENOUGH = 11 * 1024 * 1024  # the protocol requires >= 10 MB per repo; below that it
# is judged "only a pointer was uploaded"


# ─── init ────────────────────────────────────────────────────


def test_init_releases_config_and_strategy(tmp_path: Path) -> None:
    """Miners clone nothing: the templates ship in the wheel and init unpacks them."""
    args = argparse.Namespace(
        directory=str(tmp_path / "my-miner"),
        strategy="simple",
        validator=False,
        force=False,
    )
    assert init_command.run(args) == 0

    target = tmp_path / "my-miner"
    assert (target / "miner.yaml").is_file()
    assert "def train(" in (target / "train_strategy.py").read_text(encoding="utf-8")
    # the template must be readable back by our own parser
    assert Settings.load(str(target / "miner.yaml")).netuid == 80


def test_init_does_not_overwrite_without_force(tmp_path: Path) -> None:
    config = tmp_path / "miner.yaml"
    config.write_text("subnet:\n  netuid: 80\n", encoding="utf-8")

    args = argparse.Namespace(
        directory=str(tmp_path), strategy="simple", validator=False, force=False
    )
    init_command.run(args)
    assert config.read_text(encoding="utf-8") == "subnet:\n  netuid: 80\n"

    args.force = True
    init_command.run(args)
    assert "OpenRoboto" in config.read_text(encoding="utf-8")


def test_init_validator_writes_validator_config_only(tmp_path: Path) -> None:
    args = argparse.Namespace(
        directory=str(tmp_path), strategy="simple", validator=True, force=False
    )
    init_command.run(args)
    assert (tmp_path / "validator.yaml").is_file()
    assert not (tmp_path / "train_strategy.py").exists()


def test_init_gitignores_the_file_holding_the_wallet_password(tmp_path: Path) -> None:
    """`.gitignore` must block the config file -- this is a security item, not a
    convenience item.

    `miner.yaml` holds `subnet.wallet_password` and `huggingface.token`, and it is
    entirely reasonable for a miner to version their own `train_strategy.py`. Without
    this line, the very first `git add .` commits the wallet password, and **there is
    no warning of any kind**.
    """
    for validator in (False, True):
        target = tmp_path / ("val" if validator else "miner")
        args = argparse.Namespace(
            directory=str(target),
            strategy="simple",
            validator=validator,
            force=False,
        )
        assert init_command.run(args) == 0

        ignored = (target / ".gitignore").read_text(encoding="utf-8")
        assert "miner.yaml" in ignored
        assert "validator.yaml" in ignored
        # the strategy script, conversely, **must** stay committable -- it is the
        # miner's own work and deserves a history.
        assert "train_strategy.py" not in ignored


def test_init_writes_a_workspace_readme_that_names_the_next_command(
    tmp_path: Path,
) -> None:
    """The workspace ships its own manual. A miner should not have to open a web page
    to find out which command comes next."""
    args = argparse.Namespace(
        directory=str(tmp_path / "w"), strategy="simple", validator=False, force=False
    )
    assert init_command.run(args) == 0

    readme = (tmp_path / "w" / "README.md").read_text(encoding="utf-8")
    for command in ("openroboto doctor", "openroboto train", "openroboto check"):
        assert command in readme, f"README does not mention {command}"
    # skipping check costs non-refundable TAO, so this sentence must be there
    assert "not refunded" in readme


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
    """🔴 The layout of the vendor's own post-trained artifact: weights under
    `checkpoints/global_step_N/hf_ckpt/`, three levels down.

    Admission **accepts** it, the evaluator searches two levels and finds
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

    empty = tmp_path / "empty"
    empty.mkdir()
    assert check_command.weights_subdir(empty) == ""
    assert check_command.nesting_advice(empty) == []

    flat = tmp_path / "flat"
    _make_file(flat / "model.safetensors", 1024)
    assert check_command.weights_subdir(flat) == ""


def test_check_on_missing_directory_returns_error(tmp_path: Path) -> None:
    args = argparse.Namespace(path=str(tmp_path / "nope"), round=0)
    assert check_command.run(args) == 1


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
        "round_num": 1,
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


def test_status_round_filter() -> None:
    rows = [_history_row(round_num=1), _history_row(round_num=2)]
    assert status_command._by_round(rows, 0) == rows
    assert status_command._by_round(rows, 2) == [rows[1]]


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
    assert len(failed) == 4, f"an empty config should report 4 items, got {failed}"


def test_doctor_passes_on_a_complete_config() -> None:
    settings = Settings.from_mapping(
        {
            "subnet": {"netuid": 80, "hotkey_ss58": "5" + "M" * 47},
            "huggingface": {"username": "someone", "token": "hf_x"},
            "urls": {"control_json": "https://example.invalid/control.json"},
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


def test_doctor_control_check_applies_the_rate_it_fetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`check_control` must **write the fetched rate into settings**, not just display
    it.

    `check_wallet` runs after it and relies on `settings.burn_rate_tao` to decide
    whether the balance is enough. If the rate is only displayed and not applied, the
    balance check always reports "rate unknown" -- and nobody would notice.
    """
    from openroboto.config.control import ControlFetch

    monkeypatch.setattr(
        doctor_command,
        "fetch_control",
        lambda url: ControlFetch(
            control={"payment": {"burn_rate_tao": 0.1}}, etag="etag-1"
        ),
    )
    settings = Settings.from_mapping(
        {"urls": {"control_json": "https://example.invalid/control.json"}}
    )
    assert settings.burn_rate_tao is None

    result = doctor_command.check_control(settings)
    assert result.ok
    assert settings.burn_rate_tao == 0.1  # applied, not merely printed
    assert "0.1" in result.detail


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


def test_doctor_survives_an_unreachable_control_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A miner may run doctor on a machine with no route to the internet --
    exactly to find out why. An unreachable control.json is reported as one
    failed item with a readable sentence; it must not raise, and must not stop
    the remaining checks from running.
    """

    def _unreachable(url: str) -> object:
        raise ControlFetchError("Cannot reach https://example.invalid/control.json")

    monkeypatch.setattr(doctor_command, "fetch_control", _unreachable)
    settings = Settings.from_mapping(
        {"urls": {"control_json": "https://example.invalid/control.json"}}
    )

    result = doctor_command.check_control(settings)
    assert result.ok is False
    assert "Cannot reach" in result.detail
    assert "network" in result.fix  # tells them where to look, not just that it failed
