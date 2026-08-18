"""`protocol/` 三个 vendored 副本已经作废，这里钉住"作废"这件事本身。

背景：这套协议代码曾在四个仓库里各存一份，没有版本号也没有一致性检查，
`protocol/types.py` 漂了 105 行、`payment.py` 漂了 313 行 —— 矿工按 A 编码、
后端按 B 解码。`openroboto-protocol` 就是为了消灭这种副本而抽出来的。

按 `SCOPE.md`，继承自 `openroboto-subnet` 的旧文件一律不删，所以三个文件还在磁盘上。
"装了包又留着旧副本"正是漂移路径本身，靠自觉躲不掉，于是有了这几条：

1. `protocol.seed` 已经没有自己的实现，只是对协议包的再导出；
2. 仓库里除两处已知的历史遗留外，没有任何地方还 import 它们；
3. 三个文件顶部都写着 DEPRECATED；
4. 而这一切**没有**弄坏现有矿工的旧训练流程（`protocol.types` 仍能独立 import）。

`protocol/types.py` 不能改成再导出：它的值**已经**漂了（`TOP_K_EMISSION_WEIGHTS`
在这里是 `[0.70, 0.20, 0.10]`，协议包里是生效的 `(0.07, 0.02, 0.01)`，差十倍），
再导出等于悄悄改行为。它只能整体作废，理由写在文件顶部。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import openroboto_protocol.seed as pkg_seed
import protocol.seed

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDORED = ("protocol/__init__.py", "protocol/seed.py", "protocol/types.py")

# 仍在 import vendored 副本的地方。**只允许缩短，不允许变长。**
# 两处都卡在同一件事上：`PI05_BASE_CHECKPOINT` 与 `VLAEpisode` 在
# `openroboto-protocol` 里没有对应物（该不该进协议包要人裁，见 types.py 顶部）。
KNOWN_LEGACY_IMPORTERS = {
    "miner.py": "from protocol.types import PI05_BASE_CHECKPOINT",
    "miner/trainer_vla.py": "from protocol.types import VLAEpisode",
}


def _git_grep(pattern: str, *pathspec: str) -> list[str]:
    """在**被 git 跟踪的**文件里搜，返回 `路径:行号:内容`。

    用 git grep 而不是 rglob：`.venv/` 里装着 `openroboto_protocol` 本身，
    走文件系统会把它自己搜出来。
    """
    result = subprocess.run(
        ["git", "grep", "-nE", pattern, "--", *pathspec],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # git grep 无匹配时退出码为 1，那是正常结果不是错误。
    assert result.returncode in (0, 1), result.stderr
    return [line for line in result.stdout.splitlines() if line]


def test_vendored_seed_is_a_re_export_not_a_copy() -> None:
    """`protocol.seed` 的每个符号必须**就是**协议包里的那一个（同一对象）。

    相等不够，要同一性：副本里写一个值相同的实现，明天就能被单独改掉。
    """
    assert protocol.seed.derive_seed is pkg_seed.derive_seed
    assert protocol.seed.verify_seed is pkg_seed.verify_seed
    assert protocol.seed.drand_round_url is pkg_seed.drand_round_url
    assert protocol.seed.DRAND_CHAIN_HASH == pkg_seed.DRAND_CHAIN_HASH
    assert protocol.seed.DRAND_API == pkg_seed.DRAND_API


def test_documented_seed_example_still_reproduces() -> None:
    """`docs/SEED_GENERATION.md` 里那组公开示例值，两条 import 路径必须同解。

    这一条是"改成再导出没有动行为"的证据。种子公式变一个字符，
    历史评测就全部不可复现 —— 所以它不能只靠 code review 保证。
    """
    block_hash = "0x" + "11" * 32
    expected = 3898936287
    assert pkg_seed.derive_seed(block_hash, 1, "22" * 32) == expected
    assert protocol.seed.derive_seed(block_hash, 1, "22" * 32) == expected


def test_legacy_training_path_survives_without_the_protocol_package() -> None:
    """`protocol/types.py` 必须在**没装协议包**的环境里也能 import。

    这条是给现有矿工兜底的：`miner.py` / `miner/trainer_vla.py` 是旧训练流程，
    矿工装依赖用的是 `requirements.txt`，那份清单里没有 `openroboto-protocol`。
    如果 `protocol/__init__.py` 里留一行 `from openroboto_protocol...`，
    Python 在 `from protocol.types import PI05_BASE_CHECKPOINT` 时会先跑父包的
    `__init__.py`，训练当场崩在第一步 —— 而装这个仓的人不在团队里，
    我们不会立刻知道，只会看到提交量下降。

    做法：把 `sys.modules["openroboto_protocol"]` 置成 None，之后任何
    `import openroboto_protocol[.x]` 都会抛 ImportError，等价于"没装"。
    """
    probe = (
        "import sys; sys.modules['openroboto_protocol'] = None;"
        "from protocol.types import PI05_BASE_CHECKPOINT;"
        "print(PI05_BASE_CHECKPOINT)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"没装 openroboto-protocol 时 protocol.types 就 import 不了了，"
        f"现有矿工的训练会崩：\n{result.stderr}"
    )
    assert result.stdout.strip() == "gs://openpi-assets/checkpoints/pi05_base"


def test_no_new_imports_of_the_vendored_copy() -> None:
    """全仓 import `protocol` 的地方只剩两处历史遗留，多一处就红。

    新增一处 = 又开出一条静默走到副本上的路径，也就是当年漂 105 行的那条路。
    """
    # 排除本文件：第 27 行的 `import protocol.seed` 是**这条测试自己的取证手段**
    # （它要拿到副本里的函数才能和协议包逐值比对），不是违规的调用点。
    # 不排除的表现是这条测试永远红，而且是**提交之后才红** —— `git grep` 只看被
    # 跟踪的文件，`tests/` 还没进版本库时它扫不到自己，本地全绿一提交就炸。
    hits = _git_grep(
        r"^[[:space:]]*(from|import) protocol([. ]|$)",
        "*.py",
        ":!tests/test_vendored_protocol.py",
    )
    found = {line.split(":", 2)[0]: line.split(":", 2)[2].strip() for line in hits}
    # 三个 vendored 文件自己的 re-export 语句写的是 `from openroboto_protocol...`，
    # 所以不会出现在这里；真出现了说明有人把它改回了副本内部 import。
    assert found == KNOWN_LEGACY_IMPORTERS, (
        f"vendored protocol/ 的 import 清单变了：{found}\n"
        f"期望只剩：{KNOWN_LEGACY_IMPORTERS}"
    )


def test_every_vendored_file_is_marked_deprecated() -> None:
    """三个文件顶部都必须写着 DEPRECATED 和替代品的名字。

    留着一份没有标注的副本，下一个人读到的就是"这是本仓的协议实现"。
    """
    for relpath in VENDORED:
        head = (REPO_ROOT / relpath).read_text(encoding="utf-8")[:600]
        assert "DEPRECATED" in head, f"{relpath} 顶部没有 DEPRECATED 标注"
        assert "openroboto-protocol" in head, f"{relpath} 没说被谁取代"


def test_docs_do_not_teach_miners_to_import_the_copy() -> None:
    """面向矿工的文档一律指向 `openroboto_protocol`。

    README 与 docs/ 里那行 `from protocol.seed import derive_seed` 比代码更危险：
    照抄的人不在团队里，他们复现不出 seed 时不会来问，只会认为后端算错了。
    """
    assert _git_grep(r"^[[:space:]]*(from|import) protocol([. ]|$)", "*.md") == []
