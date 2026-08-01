from __future__ import annotations

from app.adapters.epost import EpostAdapter
from app.config import settings
from app.events import EventHub


class AdapterRegistry:
    def __init__(self, browser_mode: str, event_hub: EventHub) -> None:
        self._adapters = {
            "domestic_parcel": EpostAdapter(
                "domestic_parcel",
                browser_mode,
                event_hub,
                headless=settings.browser_headless,
                step_delay_ms=settings.live_step_delay_ms,
            ),
            "ems": EpostAdapter(
                "ems",
                browser_mode,
                event_hub,
                headless=settings.browser_headless,
                step_delay_ms=settings.live_step_delay_ms,
            ),
        }

    def get(self, service: str) -> EpostAdapter:
        if service not in self._adapters:
            raise ValueError(f"지원하지 않는 서비스입니다: {service}")
        return self._adapters[service]

    def services(self) -> list[str]:
        return list(self._adapters)

    async def close_all(self) -> None:
        for adapter in self._adapters.values():
            await adapter.close_all_sessions()

    def _adapter_for_run(self, run_id: str) -> EpostAdapter:
        for adapter in self._adapters.values():
            if adapter._session_for_run(run_id) is not None:
                return adapter
        raise LookupError("활성 브라우저 화면이 없습니다.")

    async def click_browser(self, run_id: str, x: float, y: float) -> dict:
        return await self._adapter_for_run(run_id).click_browser(run_id, x, y)

    async def click_preview(self, run_id: str, x: float, y: float) -> dict:
        return await self._adapter_for_run(run_id).click_preview(run_id, x, y)

    async def resolve_dialog(self, run_id: str, accept: bool) -> dict:
        return await self._adapter_for_run(run_id).resolve_dialog(run_id, accept)

    async def activate_handoff(self, run_id: str) -> dict:
        return await self._adapter_for_run(run_id).activate_handoff(run_id)

    async def submit_final(self, run_id: str, *, confirmed: bool) -> dict:
        return await self._adapter_for_run(run_id).submit_final(run_id, confirmed=confirmed)

    async def cancel_handoff(self, run_id: str) -> dict:
        return await self._adapter_for_run(run_id).cancel_handoff(run_id)

    async def resume_ems_customs(
        self,
        run_id: str,
        *,
        weight_kg: float,
        customs_value_usd: float,
        recipient_email: str,
        customs_description_en: str,
        hs_code: str,
        quantity: int,
    ) -> dict:
        return await self.get("ems").resume_ems_customs(
            run_id,
            weight_kg=weight_kg,
            customs_value_usd=customs_value_usd,
            recipient_email=recipient_email,
            customs_description_en=customs_description_en,
            hs_code=hs_code,
            quantity=quantity,
        )
