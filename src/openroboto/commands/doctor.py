"""`openroboto doctor` —— 花钱之前把能查的都查掉。

这条命令的存在理由只有一个：**「烧了 TAO 才发现环境不对」这种体验要消失。**
每一项都给出「哪一项不满足 / 期望值 / 怎么修」，不满足就退非零码。

分两类：
- 必须项（config / docker / 镜像 / control.json）—— 不满足直接判失败；
- 参考项（GPU、HF token、钱包余额）—— 环境里没装 bittensor / 没有卡时给提示，
  不判失败，因为 `check`、`status` 这些命令本来就不需要它们。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass

from openroboto.config import ConfigError, ControlFetchError, Settings, fetch_control
from openroboto.console import say
from openroboto.training.container import runner_image

MIN_PYTHON = (3, 11)


@dataclass(frozen=True)
class CheckResult:
    """一项检查的结论。`fix` 是给矿工照着敲的下一步。"""

    name: str
    ok: bool
    detail: str
    fix: str = ""
    required: bool = True

    def render(self) -> str:
        mark = "✅" if self.ok else ("❌" if self.required else "⚠️ ")
        return f"{mark} {self.name}: {self.detail}"


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "doctor", help="环境自检：GPU / Docker / 配置 / 余额"
    )
    parser.add_argument("--config", default="miner.yaml")
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    results = [check_python()]

    settings: Settings | None
    try:
        settings = Settings.load(args.config)
        results.append(
            CheckResult("配置文件", True, f"{args.config} 解析通过")
        )
    except ConfigError as exc:
        settings = None
        results.append(
            CheckResult(
                "配置文件", False, str(exc).splitlines()[0], "openroboto init ."
            )
        )

    if settings is not None:
        results.extend(check_settings(settings))
        results.append(check_control(settings))
        results.append(check_hf_token(settings))
        results.append(check_wallet(settings))

    results.append(check_docker())
    results.append(check_gpu())
    results.append(check_image())

    for result in results:
        say(result.render())
        if not result.ok and result.fix:
            say(f"   → {result.fix}")

    failed = [r for r in results if not r.ok and r.required]
    say("")
    if failed:
        say(f"❌ {len(failed)} 项必须修：{'、'.join(r.name for r in failed)}")
        return 1
    say("✅ 必须项全部通过")
    return 0


def check_python() -> CheckResult:
    info = sys.version_info
    version = f"{info.major}.{info.minor}.{info.micro}"
    ok = sys.version_info[:2] >= MIN_PYTHON
    return CheckResult(
        "Python", ok, version, f"需要 >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}", required=True
    )


def check_settings(settings: Settings) -> list[CheckResult]:
    """必填字段。缺了不一定现在就炸，但一定会在花钱那一步炸。"""
    results = [
        CheckResult(
            "netuid",
            settings.netuid > 0,
            str(settings.netuid) if settings.netuid else "未配置",
            "miner.yaml → subnet.netuid（主网 80）",
        ),
        CheckResult(
            "hotkey_ss58",
            bool(settings.hotkey_ss58),
            settings.hotkey_ss58 or "未配置",
            "miner.yaml → subnet.hotkey_ss58；HF 仓库名由它的后 12 位推导",
        ),
        CheckResult(
            "HF 账号",
            bool(settings.hf_username and settings.hf_token),
            f"username={settings.hf_username or '未配置'} "
            f"token={'已配置' if settings.hf_token else '未配置'}",
            "miner.yaml → huggingface.username / token（token 要有写权限）",
        ),
        CheckResult(
            "control.json 地址",
            bool(settings.control_json_url),
            settings.control_json_url or "未配置",
            "miner.yaml → urls.control_json",
        ),
    ]
    return results


def check_control(settings: Settings) -> CheckResult:
    """control.json 能不能拉到，本轮是什么状态、费率多少。"""
    if not settings.control_json_url:
        return CheckResult(
            "control.json", False, "未配置地址", "miner.yaml → urls.control_json"
        )
    try:
        control = fetch_control(settings.control_json_url).control or {}
    except ControlFetchError as exc:
        return CheckResult(
            "control.json", False, str(exc), "这是基建问题，不是配置错；确认网络与地址"
        )

    raw_payment = control.get("payment")
    payment = raw_payment if isinstance(raw_payment, dict) else {}
    return CheckResult(
        "control.json",
        True,
        f"round={control.get('round')} status={control.get('status')} "
        f"burn_rate={payment.get('burn_rate_tao')} TAO",
    )


def check_docker() -> CheckResult:
    if not shutil.which("docker"):
        return CheckResult("Docker", False, "没找到 docker", "装 Docker：https://get.docker.com")
    version = _run(["docker", "--version"])
    if version is None:
        return CheckResult(
            "Docker", False, "docker 命令跑不起来", "确认 docker 守护进程在跑"
        )
    return CheckResult("Docker", True, version)


def check_gpu() -> CheckResult:
    """GPU 与 NVIDIA 容器运行时。没有卡也能跑 check/status，所以不判失败。"""
    if not shutil.which("nvidia-smi"):
        return CheckResult(
            "GPU",
            False,
            "没找到 nvidia-smi",
            "训练需要 NVIDIA 驱动；只提交模型可以忽略",
            required=False,
        )
    names = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    toolkit = shutil.which("nvidia-ctk") is not None
    detail = (names or "?").replace("\n", " / ")
    if not toolkit:
        return CheckResult(
            "GPU",
            False,
            f"{detail}（缺 nvidia-container-toolkit）",
            "装 nvidia-container-toolkit，否则 `docker run --gpus all` 用不了卡",
            required=False,
        )
    return CheckResult("GPU", True, detail)


def check_image() -> CheckResult:
    image = runner_image()
    if not shutil.which("docker"):
        return CheckResult(
            "训练镜像", False, "没有 docker，查不了", "先装 Docker", required=False
        )
    found = _run(["docker", "images", "-q", image])
    if not found:
        return CheckResult("训练镜像", False, f"{image} 不存在", "openroboto build")
    return CheckResult("训练镜像", True, f"{image} 已就绪")


def check_hf_token(settings: Settings) -> CheckResult:
    """token 有没有效。无效的 token 会让上传在几个 GB 之后才失败。"""
    if not settings.hf_token:
        return CheckResult(
            "HF token", False, "未配置", "miner.yaml → huggingface.token"
        )
    try:
        from huggingface_hub import HfApi
    except ImportError:
        return CheckResult(
            "HF token",
            False,
            "没装 huggingface_hub",
            "pip install openroboto",
            required=False,
        )
    try:
        who = HfApi(token=settings.hf_token).whoami()
    except Exception as exc:
        return CheckResult(
            "HF token", False, f"校验失败：{exc}", "换一个有写权限的 token"
        )
    return CheckResult("HF token", True, f"登录为 {who.get('name', '?')}")


def check_wallet(settings: Settings) -> CheckResult:
    """钱包能不能打开、coldkey 余额够不够本轮 burn。

    这里的 `except Exception` 是故意的：钱包这一层的异常来自 bittensor SDK
    （`KeyFileError`、substrate 的连接错误……），类型不在我们的控制之内。
    **doctor 自己崩掉是最糟的结果** —— 矿工跑体检就是因为环境有问题，
    体检工具不能因为环境有问题而不出报告。
    """
    try:
        address = _coldkey_address(settings)
    except ImportError:
        return CheckResult(
            "钱包",
            False,
            "没装 bittensor",
            "pip install openroboto（提交上链需要）",
            required=False,
        )
    except Exception as exc:
        return CheckResult(
            "钱包",
            False,
            str(exc).splitlines()[0],
            "用 `btcli wallet list` 核对 coldkey / hotkey 名字与钱包路径",
        )

    if not address:
        return CheckResult(
            "钱包", True, "已加载（读不到 coldkey 地址，跳过余额）", required=False
        )

    try:
        from openroboto.chain import get_subtensor

        subtensor = get_subtensor(settings.network)
        try:
            balance = float(subtensor.get_balance(address))
        finally:
            subtensor.close()
    except Exception as exc:
        return CheckResult("钱包", True, f"已加载（余额查不到：{exc}）", required=False)

    enough = balance >= settings.burn_rate_tao
    return CheckResult(
        "钱包余额",
        enough,
        f"{balance:.4f} TAO（本轮要烧 {settings.burn_rate_tao} TAO）",
        "余额不足，充值后再 submit —— 烧到一半失败照样要重来",
    )


def _coldkey_address(settings: Settings) -> str:
    """打开钱包并读出 coldkey 公钥地址。

    `wallet.coldkeypub` 是**属性访问触发文件读**：钱包目录里没有 coldkeypub.txt
    时它抛 `KeyFileError`，而不是返回 None —— 本机实测过一次 doctor 因此崩掉。
    """
    from openroboto.chain import open_wallet

    wallet = open_wallet(settings)
    coldkeypub = wallet.coldkeypub
    return str(getattr(coldkeypub, "ss58_address", "") or "")


def _run(command: list[str]) -> str | None:
    """跑一条只读命令，取 stdout。失败给 None。"""
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()
