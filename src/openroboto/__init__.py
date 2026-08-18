"""OpenRoboto 子网（Bittensor netuid 80）的用户 CLI。

包名 `openroboto`，命令 `openroboto`。矿工与外部验证者能敲的一切都在这里，
从初始化训练环境到提交上链，全程不需要 clone 任何仓库。
"""

from __future__ import annotations

from typing import Final

__version__: Final = "0.1.0"
"""客户端版本。

前身 `rt.py` 全文没有一处版本号 —— 矿工报「我提交失败了」时，
没有任何办法知道他手上跑的是哪一版代码，只能靠猜。
每条上链前的日志都会打这个号（`openroboto --version` 也打），
以后再看日志能一眼分辨客户端。
"""

GITHUB_REPO_URL: Final = "https://github.com/openroboto-ai/openroboto-cli.git"
"""公开仓库地址，唯一来源。

`scripts/deploy_miner.sh` 的默认值一直是占位符 `your-org/robot-train-subnet`，
那个仓库根本不存在 —— 照文档跑必然在 git clone 那一步失败。
`openroboto build` 在本地没有 openpi-runner/ 时也用它作为 docker 的远程构建上下文。
"""

OPENPI_RUNNER_CONTEXT: Final = "openpi-runner"
"""训练镜像的构建上下文目录名（仓库内，不进 pip 包）。"""
