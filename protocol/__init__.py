"""⚠️ DEPRECATED —— 已被 `openroboto-protocol` 取代，此目录待归档。

**任何 import 都应改掉**：
`from protocol import derive_seed` → `from openroboto_protocol.seed import derive_seed`

这个包名（`protocol`）是本仓从 `openroboto-subnet` 继承下来的手工副本。按 `SCOPE.md`，
旧文件一律不删，但新结构里一个都不重建 —— `src/openroboto/` 全部从
`openroboto-protocol` 装（见 `preflight.py` / `chain/commitment.py` / `commands/check.py`）。

留着副本本身就是缺陷：装了包又留着旧副本，`import` 会静默走到副本上，
正是当年 `protocol/types.py` 漂 105 行、`payment.py` 漂 313 行的那条路径。
所以 `protocol/seed.py` 现在只是对 `openroboto_protocol.seed` 的再导出，
副本不再有独立的实现可以漂。

## 为什么这里**没有**包级 re-export

原来这个文件写着 `from .seed import derive_seed, ...`。现在删掉了，一行 import 都不留，
因为它会在 `protocol` 包被**任何方式**触及时都要求 `openroboto_protocol` 装在环境里 ——
包括 `from protocol.types import ...`（Python 会先执行父包的 `__init__.py`）。

而 `protocol/types.py` 还有两个使用者（`miner.py` / `miner/trainer_vla.py`），
它们是给矿工照着跑的旧训练流程，装依赖用的是 `requirements.txt`，
那份清单里**没有** `openroboto-protocol`。留着包级 re-export 就等于让现有矿工的
训练在 `from protocol.types import PI05_BASE_CHECKPOINT` 那一行直接崩掉 ——
装这个仓的人不在团队里，我们不会立刻知道，只会看到提交量下降。

代价是 `from protocol import derive_seed` 这条路没了。它从未出现在任何面向矿工的文档里
（README 与 `docs/SEED_GENERATION.md` 写的一直是 `from protocol.seed import ...`），
全仓也无人使用。要种子派生就装协议包，那是唯一还会被维护的一份：

    pip install "openroboto-protocol==1.0.0"
    from openroboto_protocol.seed import derive_seed

`protocol/types.py` 是唯一还没有归宿的一份 —— 原因写在那个文件的顶部。
"""

# 故意为空：这里不许再出现任何 import，理由见上面那段。
__all__: list[str] = []
