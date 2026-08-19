"""`protocol/` 那份 vendored 副本已经删除，这里钉住"它不许回来"。

背景：这套协议代码曾在四个仓库里各存一份，没有版本号也没有一致性检查，
`protocol/types.py` 漂了 105 行、`payment.py` 漂了 313 行 —— 矿工按 A 编码、
后端按 B 解码。`openroboto-protocol` 就是为了消灭这种副本而抽出来的。

**2026-08-19：三个文件已随旧结构一起删除**（此前按 `SCOPE.md`「旧文件一律不删」
留在磁盘上，作为再导出的空壳）。这个文件因此从"看守一份已作废的副本"改成
"确认副本没有以任何形式回来"，外加一条与副本无关、但必须一直成立的断言：
公开文档里那组种子示例值仍然可复现。

为什么删了还要留这些用例：漂移不是靠自觉躲得掉的。新增一份副本不会让任何东西
报错，只会让某天的评测复现不出来 —— 那正是当年 105 行的走法。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import openroboto_protocol.seed as pkg_seed

REPO_ROOT = Path(__file__).resolve().parent.parent


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


def test_documented_seed_example_still_reproduces() -> None:
    """`docs/SEED_GENERATION.md` 里那组公开示例值必须一直算得出来。

    这条与 vendored 副本无关，删掉副本之后**更**需要它：种子公式变一个字符，
    历史评测就全部不可复现，而矿工是拿这组值验证我们没有针对谁挑 seed 的。
    协议包自己也有黄金向量测试；这一条钉的是"**文档里印的那个数**"。
    """
    block_hash = "0x" + "11" * 32
    assert pkg_seed.derive_seed(block_hash, 1, "22" * 32) == 3898936287


def test_no_vendored_protocol_copy_exists() -> None:
    """仓库里不许有 `protocol/` 下的 Python 文件。

    `.github/workflows/protocol-guards.yml` 也查这一条（CI 层，跨语言都拦）。
    两处都留着是故意的：本地 `pytest` 能立刻发现，CI 拦住绕过本地钩子的提交。
    """
    copies = _git_grep(".", "*protocol/*.py")
    tracked = subprocess.run(
        ["git", "ls-files", "*protocol/*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    assert tracked == [], (
        f"vendored protocol 副本又出现了：{tracked}\n"
        f"协议相关的东西只能来自 `openroboto-protocol` 包（AGENTS.md 红线 #1）"
    )
    assert copies == []


def test_nothing_imports_a_local_protocol_module() -> None:
    """全仓不许有 `import protocol` / `from protocol import ...`。

    副本删了，但 import 语句还能被写出来（比如从旧分支 cherry-pick 回来）。
    那会变成 ImportError，而不是静默走到副本上 —— 但仍然要拦，
    因为下一步就是有人"把缺的文件补回来"。
    """
    hits = _git_grep(r"^[[:space:]]*(from|import) protocol([. ]|$)", "*.py")
    assert hits == [], f"还有地方 import 本地 protocol 模块：{hits}"


def test_docs_do_not_teach_miners_to_import_the_copy() -> None:
    """面向矿工的文档一律指向 `openroboto_protocol`。

    文档里那行 `from protocol.seed import derive_seed` 比代码更危险：
    照抄的人不在团队里，他们复现不出 seed 时不会来问，只会认为后端算错了。
    """
    assert _git_grep(r"^[[:space:]]*(from|import) protocol([. ]|$)", "*.md") == []
