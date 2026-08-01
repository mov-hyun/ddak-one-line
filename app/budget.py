from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


MODEL_PRICES_PER_MILLION = {
    "gpt-5.6-sol": (Decimal("5"), Decimal("30")),
    "gpt-5.6": (Decimal("5"), Decimal("30")),
    "gpt-5.6-terra": (Decimal("2.5"), Decimal("15")),
    "gpt-5.6-luna": (Decimal("1"), Decimal("6")),
}


@dataclass
class BudgetSnapshot:
    spent_usd: Decimal
    limit_usd: Decimal

    @property
    def remaining_usd(self) -> Decimal:
        return max(Decimal("0"), self.limit_usd - self.spent_usd)


class BudgetExceeded(RuntimeError):
    pass


class CostGuard:
    def __init__(self, ledger_path: Path, model: str, limit_usd: float) -> None:
        self.ledger_path = ledger_path
        self.model = model
        self.limit_usd = Decimal(str(limit_usd))

    def _read(self) -> dict:
        if not self.ledger_path.exists():
            return {"spent_usd": "0", "runs": []}
        return json.loads(self.ledger_path.read_text(encoding="utf-8"))

    def snapshot(self) -> BudgetSnapshot:
        ledger = self._read()
        return BudgetSnapshot(Decimal(ledger["spent_usd"]), self.limit_usd)

    def ensure_available(self, reserve_usd: Decimal = Decimal("0.10")) -> None:
        snapshot = self.snapshot()
        if snapshot.spent_usd + reserve_usd >= snapshot.limit_usd:
            raise BudgetExceeded(
                f"로컬 API 예산 한도 ${snapshot.limit_usd}에 도달했습니다."
            )

    def record(self, run_id: str, input_tokens: int, output_tokens: int) -> Decimal:
        input_price, output_price = MODEL_PRICES_PER_MILLION.get(
            self.model,
            MODEL_PRICES_PER_MILLION["gpt-5.6-terra"],
        )
        cost = (
            Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price
        ) / Decimal(1_000_000)
        ledger = self._read()
        total = Decimal(ledger["spent_usd"]) + cost
        ledger["spent_usd"] = str(total)
        ledger["runs"].append(
            {
                "run_id": run_id,
                "model": self.model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost_usd": str(cost),
            }
        )
        self.ledger_path.write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return cost
