"""⚠️ DEPRECATED —— 已被 `openroboto-protocol` 取代，此文件待归档。

**任何 import 都应改掉**：
`from protocol.seed import ...` → `from openroboto_protocol.seed import ...`

为什么这个文件还在：`SCOPE.md` 规定从 `openroboto-subnet` 继承来的旧文件一律不删
（仓库改名后 GitHub 会重定向旧 URL，矿工手里的 clone 地址不断）。所以它留在原地，
但**不再有自己的实现** —— 下面全部是从 `openroboto_protocol.seed` 的再导出。

为什么必须是再导出，而不是"留一份副本 + 写一句别用了"：
装了包**又**留着旧副本，`import` 会静默走到副本上，谁都不会收到警告，
直到某天评测复现不出来才发现两边跑的不是一套代码。这不是假想 ——
这套协议代码曾在四个仓库里各存一份，`protocol/types.py` 漂了 105 行、
`payment.py` 漂了 313 行，于是矿工按 A 编码、后端按 B 解码。
种子派生当时**还没**漂，`openroboto-protocol` 就是趁它还没漂时抽出来的。
改成再导出之后，这条漂移路径在物理上就不存在了：这里没有可以被单独改动的代码。

种子派生是整个子网最红的一条线 —— 公式变一个字符，**历史评测全部不可复现**。
`openroboto_protocol.seed` 里的实现与这里原来的实现逐字一致（抽包时一个字节都没改），
并且有链上真实发生过的黄金向量钉着。`tests/test_packaging.py` 里有一条用例
拿 `docs/SEED_GENERATION.md` 的示例值核对两条 import 路径给出同一个 seed。

⚠️ 这个模块现在需要 `openroboto-protocol==1.0.0` 装在环境里。它是 `openroboto`
包的依赖，正常安装即可；用旧的 `requirements.txt` 装出来的环境里没有它，
会得到一个 ImportError —— 那是**故意的**：明着报错好过安静地用一份可能已经漂了的副本。
代价被限制在这一个模块内：`protocol/__init__.py` 一行 import 都没有，所以
`from protocol.types import ...`（旧训练流程还在用）不受影响，理由写在那个文件里。

归档时机：`miner.py` / `miner/trainer_vla.py` 不再需要 `protocol/types.py` 之后，
整个 `protocol/` 目录连同 `.github/workflows/protocol-guards.yml` 里的豁免清单一起删。
"""

from __future__ import annotations

from openroboto_protocol.seed import (
    DRAND_API,
    DRAND_CHAIN_HASH,
    derive_seed,
    drand_round_url,
    verify_seed,
)

__all__ = [
    "DRAND_API",
    "DRAND_CHAIN_HASH",
    "derive_seed",
    "drand_round_url",
    "verify_seed",
]
