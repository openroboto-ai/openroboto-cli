"""Parsing of `miner.yaml` / `validator.yaml`.

The field names are the key names in the YAML file the miner is holding —
**change one key name and every existing miner's config file stops working**
(AGENTS.md red line #3). So this was a move and nothing else: not one letter of any
key name was touched.

Division of labour:
- `miner.yaml` belongs to **the user** (credentials, paths, their own training
  script, their own hyperparameters);
- the **competition row** belongs to the subnet (this season's fee, image,
  dataset, base checkpoint), and `openroboto init` copies it into the
  `competition:` section of this same file;
- `control.json` is down to `public_key`, the one thing external validators
  have no other way to get. See `config/control.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml
from openroboto_protocol.constants import BURN_BLOCK_WINDOW

from openroboto.config import environments


class ConfigError(Exception):
    """A config item is missing or invalid.

    The message is aimed at the miner and must state which item, what value was
    expected, and how to fix it.
    """


#: Chain limits that bracket `weight_interval_min`, read off netuid 80 on
#: 2026-08-20: `weights_rate_limit = 100` blocks and `activity_cutoff = 5000`
#: blocks, at roughly 12 s per block.
WEIGHT_INTERVAL_FLOOR_MIN = 20
ACTIVITY_CUTOFF_MIN = 16 * 60 + 40

#: How much room to insist on under the cutoff. A validator has to survive a
#: restart, a slow deploy, or one failed extrinsic without falling off the edge,
#: and none of those announce themselves.
CUTOFF_SAFETY_FACTOR = 3


def check_weight_interval(minutes: int) -> list[str]:
    """Check `weight_interval_min` against the two chain limits.

    Returns problems in the shape `require_for_chain` collects them.

    Both failure modes are silent, which is why this refuses rather than warns:

    - **Too often** -- the extrinsic is rejected by `weights_rate_limit`, so no
      weights land. The validator logs a submission; the chain ignores it.
    - **Too rarely** -- past `activity_cutoff` the subnet treats the validator as
      inactive. Its weights stop counting, so the miners it backs earn nothing,
      and nothing reports an error: the process is running, the logs look normal,
      the emission is simply gone.

    The unit is minutes. Production's config carries the comment "(in blocks)",
    which is wrong -- the code multiplies by 60 for seconds. Reading that comment
    and "correcting" the value to a block count lands under the floor, which this
    catches.
    """
    ceiling = ACTIVITY_CUTOFF_MIN // CUTOFF_SAFETY_FACTOR
    if minutes < WEIGHT_INTERVAL_FLOOR_MIN:
        return [
            f"weight_interval_min = {minutes} is below the chain's rate limit "
            f"({WEIGHT_INTERVAL_FLOOR_MIN} min = 100 blocks). Weights set sooner "
            f"than that are rejected, so none land. The unit is minutes -- if you "
            f"copied a value commented '(in blocks)', that comment is wrong."
        ]
    if minutes > ceiling:
        return [
            f"weight_interval_min = {minutes} leaves too little room under "
            f"activity_cutoff ({ACTIVITY_CUTOFF_MIN} min = 5000 blocks). Past the "
            f"cutoff your weights stop counting and the miners you back earn "
            f"nothing, silently. Use {ceiling} or less."
        ]
    return []


@dataclass
class Settings:
    """The full content of one config file."""

    #: Which subnet + backend this config talks to: `mainnet` | `dev`.
    #:
    #: One name for a decision that would otherwise be four independent switches
    #: (`network`, `netuid`, `urls.control_json`, `backend.url`). Changing only
    #: some of them is not harmless — see `config/environments.py` for the two
    #: half-states and what each one costs.
    #:
    #: It supplies **URL defaults only**, never `netuid`: that field has no
    #: default on purpose, because a config that forgets it must fail rather than
    #: quietly pick a subnet. `require_for_chain()` verifies the netuid you set
    #: against the environment you named.
    environment: str = environments.DEFAULT_ENVIRONMENT

    # ─── Bittensor ─────────────────────────────────────
    network: str = "finney"
    # 0 = not configured. **No default netuid**, deliberately: any default (313 on
    # testnet, 80 on mainnet) means a miner.yaml that forgets the field sends the
    # burn to whichever subnet that default names, and the TAO really is burned. A
    # default cannot save you from that mistake, only refusing to start can, so
    # `require_for_chain()` blocks it.
    netuid: int = 0
    wallet_path: str = ""
    coldkey: str = "default"
    hotkey: str = "default"
    hotkey_ss58: str = ""
    wallet_password: str = ""
    # How often the validator calls set_weights, in minutes.
    #
    # Bounded on both sides by the chain, and both bounds bite silently:
    #
    #   floor    weights_rate_limit = 100 blocks (~20 min). Set weights sooner and
    #            the extrinsic is rejected, so none land that cycle.
    #   ceiling  activity_cutoff = 5000 blocks (~16.7 h). Go quiet longer than that
    #            and the subnet stops counting you: your weights are treated as
    #            absent and the miners you back earn nothing.
    #
    # 60 is 3x the floor (never rejected for being early) and leaves 16 cycles of
    # margin under the ceiling. **Not 720** (12 h): that leaves 4.7 h of headroom,
    # so one missed cycle -- or one restart during a deploy -- puts you past the
    # cutoff with nothing to tell you. `check_weight_interval` enforces both.
    #
    # The unit is minutes. Production's config carries the comment "(in blocks)",
    # which is wrong -- the code multiplies by 60 to get seconds.
    weight_interval_min: int = 60

    # ─── Public HTTP resources ─────────────────────────
    #: Where the subnet publishes `public_key`. That is **all** it is read for
    #: now: the season, the status, the dataset and the base checkpoint moved to
    #: the competition row, and the fee has come from `params.fee` since the
    #: pre-payment check landed. `urls.dataset_train` / `dataset_val` are gone
    #: with them -- a dataset URL that is not the one this season publishes is
    #: a training run on the wrong data, and it looks exactly like a normal one.
    control_json_url: str = ""

    # ─── Competition ───────────────────────────────────
    #: The `competition:` section verbatim -- the snapshot `openroboto init`
    #: wrote of the season this workspace mines. Empty = a config from before
    #: competitions existed, and `submit` refuses such a workspace outright
    #: (`commands/submit.py::_no_season`): a fee paid with no season attached is
    #: filed under whichever season the backend defaults to, and is not refunded.
    #:
    #: Stored raw because that is what it is: a copy of one backend row, read
    #: through `competition.Snapshot`. The two fields below are the parts the
    #: commands reach for often enough to deserve a name of their own; they are
    #: read out of this same section, never set independently.
    competition: dict[str, Any] = field(default_factory=dict)
    #: Which competition this workspace mines, as the adapter string
    #: `openroboto init` wrote into `miner.yaml`. Empty = a config from before
    #: competitions existed; see `adapters.DEFAULT_ADAPTER` for what that means.
    competition_adapter: str = ""
    #: `competition.base_model_family` -- which base model this season runs on.
    #: `""` = the file does not say (written before the key existed, or the season
    #: has not decided). Resolved by `adapters.base_model_family()`, which refuses
    #: rather than guesses.
    competition_base_model_family: str = ""
    #: That competition's own parameters, verbatim from the snapshot `init`
    #: wrote. Passed through, never interpreted here: a value the CLI
    #: understands is a value that needs a CLI release to change.
    competition_params: dict[str, Any] = field(default_factory=dict)
    #: `competition.source` -- **which backend served the season above**.
    #: `""` = the file does not say (written before the key existed).
    #:
    #: It is not decoration: `(track, seq)` is unique per database, not across
    #: them, so a season fetched from one backend matches a season on another
    #: and nothing else in this file records which one it was. That is the fact
    #: `require_for_chain()` hands to `check_coherent()`.
    competition_source: str = ""

    # ─── Model ─────────────────────────────────────────
    #: A base checkpoint already on this machine. Empty is the normal case:
    #: the season names its own starting point in `params.training.checkpoint`,
    #: and when neither says anything the training image uses its own default.
    #:
    #: The season wins over this field. The other direction lets a path left
    #: over from an earlier season quietly train the next one on the wrong base.
    vla_checkpoint_path: str = ""

    # ─── Training hyperparameters ──────────────────────
    #: 🔴 **These five are the miner's, not the subnet's.** They are read from
    #: `miner.yaml`, **never** from control.json's `training` block: an epoch
    #: count and a LoRA rank served centrally decide every miner's competition
    #: space for them. The defaults are the values control.json served, so a
    #: workspace that leaves them alone is unaffected.
    #:
    #: They reach the container as `EPOCHS` / `BATCH_SIZE` / `LR` / `LORA_R` /
    #: `LORA_ALPHA` (red line #2 -- strategy scripts read them out of `cfg`,
    #: so the names do not change).
    epochs: int = 3
    batch_size: int = 4
    learning_rate: float = 1e-4
    lora_r: int = 32
    lora_alpha: int = 64

    # ─── HuggingFace ───────────────────────────────────
    hf_token: str = ""
    hf_username: str = ""
    #: Explicit repository to upload to, used verbatim when set.
    #:
    #: A miner who already has a repository whose name the default no longer
    #: derives (the default is season-scoped, see `huggingface/repository.py`)
    #: sets this to keep it. Otherwise the next upload creates a second
    #: repository and re-pushes several GB for no reason.
    hf_repo_id: str = ""
    hf_merged_model_id: str = ""

    # ─── Logging ───────────────────────────────────────
    log_level: str = "INFO"
    log_dir: str = "logs"

    # ─── Miner ─────────────────────────────────────────
    custom_train_script: str = ""

    # ─── Validator ─────────────────────────────────────
    backend_url: str = "https://api.openroboto.ai"
    backend_public_key: str = ""

    #: How many blocks are allowed between burn and announce. Going over means the
    #: backend marks it `rejected`, and the TAO is not refunded.
    #:
    #: ✅ **Comes from the protocol package** (red line #1), never a local copy.
    #: This field only exists so `miner.yaml` can still override it; leave it out
    #: and you get the protocol value.
    #:
    #: Provenance lives with the constant: `scanner.burn_block_window` in the
    #: production `backend.yaml`, enforced at `scanner/burn_verify.py:71`.
    #:
    #: Changing this number means changing the production `backend.yaml` and the
    #: protocol constant in the same breath. Being stricter than the backend is not
    #: the safe direction -- it rejects submissions the backend would have accepted,
    #: and by then the miner has already burned.
    burn_block_window: int = BURN_BLOCK_WINDOW

    def require_for_chain(self) -> None:
        """Minimum config check before going on chain.

        One missing item and it refuses outright — on-chain operations cost money,
        so they must not run with a broken config.
        """
        missing: list[str] = []
        if self.netuid <= 0:
            missing.append("subnet.netuid (80 on mainnet, 313 on testnet)")
        if not self.network:
            missing.append("subnet.network (finney | test | local)")
        # Report missing fields and contradictions **together**, not in two
        # rounds. Reported separately, someone who mistyped the environment name
        # first gets "netuid missing", fills in netuid, and only then sees the
        # real problem -- one command re-run per problem just to learn the next.
        missing += check_weight_interval(self.weight_interval_min)
        missing += environments.check_coherent(
            environment=self.environment,
            network=self.network,
            netuid=self.netuid,
            control_json_url=self.control_json_url,
            backend_url=self.backend_url,
            # 🔴 The fact none of the fields above can contradict: they describe
            # each other, while this one describes where the season came from.
            competition_source=self.competition_source,
        )
        if missing:
            raise ConfigError(
                "This config cannot commit on chain:\n  - "
                + "\n  - ".join(missing)
                + "\n"
                "  \u2192 edit miner.yaml, or run `openroboto init` to generate a "
                "template. TAO burned with a mismatched config is not refunded."
            )

    @classmethod
    def from_yaml(cls, path: str) -> Settings:
        """Read one YAML file. The key names match `templates/miner.yaml` exactly."""
        try:
            with open(path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except FileNotFoundError as exc:
            raise ConfigError(
                f"Config file not found: {path}\n"
                f"  \u2192 `openroboto init <directory>` generates a working "
                f"miner.yaml"
            ) from exc
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

        if not isinstance(raw, dict):
            raise ConfigError(
                f"The top level of {path} must be a mapping (key: value), "
                f"got {type(raw).__name__}"
            )

        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> Settings:
        """Build from an already-parsed YAML mapping.

        Tests go straight through this one, with nothing written to disk.
        """
        cfg = cls()

        # Apply the environment preset first, then read the explicit fields --
        # the order cannot be reversed: explicit values must override the preset.
        cfg.environment = str(data.get("environment", cfg.environment))
        preset = environments.ENVIRONMENTS.get(cfg.environment)
        if preset is not None:
            # `None` = this environment does not decide that item (`local`
            # decides none of the three). Override only where the preset
            # actually supplies a value; otherwise keep the field's own default.
            if preset.network is not None:
                cfg.network = preset.network
            if preset.host is not None:
                cfg.control_json_url = preset.control_json_url
                cfg.backend_url = preset.backend_url
            else:
                # local: clear the built-in production defaults. Keeping them
                # is the dangerous option -- forgetting to set a URL would
                # silently connect to the mainnet backend while you believe you
                # are testing locally.
                cfg.control_json_url = ""
                cfg.backend_url = ""
            # netuid is deliberately not set; see the comment on that field above.

        subnet = _section(data, "subnet")
        cfg.network = subnet.get("network", cfg.network)
        cfg.netuid = int(subnet.get("netuid", cfg.netuid) or 0)
        cfg.wallet_path = subnet.get("wallet_path", cfg.wallet_path)
        # coldkey / hotkey may be parsed as numbers in YAML (a wallet named `123`),
        # so convert both to strings.
        cfg.coldkey = str(subnet.get("coldkey", cfg.coldkey))
        cfg.hotkey = str(subnet.get("hotkey", cfg.hotkey))
        cfg.hotkey_ss58 = subnet.get("hotkey_ss58", cfg.hotkey_ss58)
        cfg.wallet_password = subnet.get("wallet_password", cfg.wallet_password)

        urls = _section(data, "urls")
        cfg.control_json_url = urls.get("control_json", cfg.control_json_url)

        # The competition snapshot. `params` is stored raw: the CLI dispatches on
        # `adapter` and reads the few keys a given step needs, and anything it
        # does not recognize is a key a later competition added -- dropping it
        # here would mean a CLI release per competition parameter.
        competition = _section(data, "competition")
        cfg.competition = competition
        cfg.competition_adapter = str(
            competition.get("adapter", cfg.competition_adapter) or ""
        )
        # Which base model, as opposed to which track: `adapter` does not say
        # (`real_xarm6` names a robot arm). `""` = this file does not say, which
        # `adapters.base_model_family()` resolves or refuses -- never guesses.
        cfg.competition_base_model_family = str(
            competition.get("base_model_family", cfg.competition_base_model_family)
            or ""
        )
        cfg.competition_source = str(competition.get("source") or "")
        cfg.competition_params = _section(competition, "params")

        model = _section(data, "model")
        cfg.vla_checkpoint_path = model.get(
            "vla_checkpoint_path", cfg.vla_checkpoint_path
        )

        # The miner's own hyperparameters. `float()` on the learning rate is not
        # decoration: YAML 1.1 only resolves `1.0e-4` as a float, and a config
        # written `1e-4` arrives here as the *string* "1e-4" -- which would
        # reach `docker run -e LR=1e-4` unchanged and only be noticed by
        # whoever compares two runs' losses.
        training = _section(data, "training")
        cfg.epochs = int(training.get("epochs", cfg.epochs))
        cfg.batch_size = int(training.get("batch_size", cfg.batch_size))
        cfg.learning_rate = float(training.get("learning_rate", cfg.learning_rate))
        cfg.lora_r = int(training.get("lora_r", cfg.lora_r))
        cfg.lora_alpha = int(training.get("lora_alpha", cfg.lora_alpha))

        hf = _section(data, "huggingface")
        cfg.hf_token = hf.get("token", cfg.hf_token)
        cfg.hf_username = hf.get("username", cfg.hf_username)
        cfg.hf_repo_id = hf.get("repo_id", cfg.hf_repo_id)
        cfg.hf_merged_model_id = hf.get("merged_model_id", cfg.hf_merged_model_id)

        cfg.log_level = data.get("log_level", cfg.log_level)
        cfg.log_dir = data.get("log_dir", cfg.log_dir)
        cfg.weight_interval_min = int(
            data.get("weight_interval_min", cfg.weight_interval_min)
        )
        cfg.custom_train_script = data.get(
            "custom_train_script", cfg.custom_train_script
        )

        backend = _section(data, "backend")
        cfg.backend_url = backend.get("url", cfg.backend_url)
        cfg.backend_public_key = backend.get("public_key", cfg.backend_public_key)

        return cfg

    @classmethod
    def load(cls, path: str) -> Settings:
        """Alias for `from_yaml`; the command layer uses this name throughout."""
        return cls.from_yaml(path)


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    """Get a second-level section.

    Returns an empty dict when the section is written empty (`subnet:` with nothing
    after it).
    """
    value = data.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(
            f"Config section `{name}` must be a mapping, got {type(value).__name__}"
        )
    return value
