from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


RULES_PATH = Path(__file__).with_name("shipping_rules.json")


@dataclass(frozen=True)
class ShipmentAssessment:
    decision: str
    destination_country: str
    service: str
    category: str
    plain_summary: str
    reason: str
    next_action: str
    rule_id: str
    source_title: str
    source_url: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


class ShippingPolicyEngine:
    """Data-driven preflight for high-impact postal restrictions.

    This layer does not pretend to issue a customs ruling. It catches known
    high-risk cases before browser entry and leaves ordinary items to Korea
    Post's own validation flow.
    """

    def __init__(self, rules_path: Path = RULES_PATH) -> None:
        payload = json.loads(rules_path.read_text(encoding="utf-8"))
        self.rules: list[dict[str, Any]] = payload["rules"]
        self.default_source = payload["default_source"]

    @staticmethod
    def destination(recipient: dict[str, str]) -> tuple[str, str]:
        if recipient.get("address_domestic", "").strip():
            return "KR", "domestic_parcel"
        international = recipient.get("address_international", "").upper()
        if " USA" in f" {international}" or international.endswith("USA"):
            return "US", "ems"
        return "INTL", "ems"

    @staticmethod
    def _normalized(value: str) -> str:
        return re.sub(r"\s+", " ", value.strip().lower())

    def assess(self, recipient: dict[str, str], contents: str) -> ShipmentAssessment:
        country, service = self.destination(recipient)
        normalized = self._normalized(contents)

        for rule in self.rules:
            countries = rule.get("destination_countries", ["*"])
            services = rule.get("services", ["*"])
            if "*" not in countries and country not in countries:
                continue
            if "*" not in services and service not in services:
                continue
            if not any(keyword.lower() in normalized for keyword in rule["keywords"]):
                continue
            return ShipmentAssessment(
                decision=rule["decision"],
                destination_country=country,
                service=service,
                category=rule["category"],
                plain_summary=rule["plain_summary"],
                reason=rule["reason"],
                next_action=rule["next_action"],
                rule_id=rule["id"],
                source_title=rule["source_title"],
                source_url=rule["source_url"],
            )

        return ShipmentAssessment(
            decision="clear_to_prepare",
            destination_country=country,
            service=service,
            category="ordinary_goods",
            plain_summary="접수를 준비할 수 있어요.",
            reason="현재 확인된 고위험 품목 규칙과 충돌하지 않습니다.",
            next_action="우체국 공식 화면에서 주소와 통관정보를 다시 검증합니다.",
            rule_id="default_official_validation",
            source_title=self.default_source["title"],
            source_url=self.default_source["url"],
        )
