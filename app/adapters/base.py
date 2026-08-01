from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StageResult:
    status: str
    service: str
    current_step: str
    final_button_text: str = "접수신청"
    final_button_clicked: bool = False
    missing_fields: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


class SiteAdapter(ABC):
    service: str

    @abstractmethod
    def required_fields(self) -> list[str]: ...

    @abstractmethod
    async def stage(
        self,
        *,
        run_id: str,
        sender: dict[str, str],
        recipient: dict[str, str],
        shipment: dict[str, Any],
    ) -> StageResult: ...

    @abstractmethod
    async def verify(self, result: StageResult) -> dict[str, Any]: ...
