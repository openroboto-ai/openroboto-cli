"""No hard-coded TAO amounts in the miner-facing docs.

Why
---
The fee is a **per-season operating value**. Changing it is one operations call
against the competitions API -- no release, no deploy. A number copied into the
docs is therefore a second source that nothing updates.

That is not hypothetical. The simulation fee moved 0.1 -> 0.2 on 2026-09-03 and
every one of these documents still said 0.1 for six days. A miner following them
would have prepared the wrong amount, and burn verification is fail-closed: the
submission is rejected and the burned TAO is not refunded.

⚠️ The rule is **"no TAO amount"**, not "not 0.1". Rewriting the number to 0.2
only postpones the next time it goes stale. To talk about the fee, point at
`params.fee.amount_tao` on the competitions API -- the same source the CLI reads
in the moment before it pays.

Historical statements are allowed, and have to be: `PAYMENT.md` explains what an
older season charged, which is a fact about the past and cannot go stale. Mark
those lines with the pragma below so this check can tell "what it used to be"
apart from "what you owe today".
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Miner-facing documents. A file that tells a miner what to pay belongs here.
DOCS = (
    "README.md",
    "docs/SUBNET_OVERVIEW.md",
    "docs/MINER_LINGBOT.md",
    "docs/MINER.md",
    "docs/PAYMENT.md",
    "docs/MIGRATION.md",
)

#: `0.1 TAO` / `1.5 TAO` / `2 TAO`. Bare "TAO" is fine -- that is the token, not
#: a rate. Only "number next to TAO" is a rate someone can act on.
AMOUNT = re.compile(r"\b\d+(?:\.\d+)?\s*TAO\b")

#: Opt out for a line that states a *historical* amount. Keep the reason on the
#: same line: the next person has to be able to tell a deliberate exemption from
#: a forgotten one.
PRAGMA = "docs-fee-check: historical"


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    bad: list[str] = []
    for name in DOCS:
        path = root / name
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PRAGMA in line:
                continue
            for hit in AMOUNT.findall(line):
                bad.append(f"  {name}:{number}  {hit}")

    if bad:
        print("Hard-coded TAO amounts in the miner docs:\n" + "\n".join(bad))
        print(
            "\nThe fee is a per-season operating value -- changing it does not ship a"
            " release, so a copy here goes stale silently (it did, for six days,"
            " on 2026-09-03).\nPoint at `params.fee.amount_tao` on the competitions"
            f" API instead, or mark a genuinely historical line `{PRAGMA}`."
        )
        return 1

    print("ok - no hard-coded fees in the miner docs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
