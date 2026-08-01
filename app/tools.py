from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import asdict
from typing import Any

from agents import function_tool

from app.adapters import AdapterRegistry
from app.events import EventHub
from app.shipping_policy import ShippingPolicyEngine
from app.vault import VaultRepository


current_run_id: ContextVar[str] = ContextVar("current_run_id", default="unknown")

_vault: VaultRepository | None = None
_adapters: AdapterRegistry | None = None
_event_hub: EventHub | None = None
_stage_results: dict[str, Any] = {}
_policy_engine = ShippingPolicyEngine()


@function_tool
async def select_service_adapter(goal: str) -> str:
    """사용자 목표와 등록된 서비스 어댑터를 대조해 실행 가능 여부를 판단한다."""
    _, adapters, event_hub = _dependencies()
    normalized = goal.strip().lower()
    epost_terms = ("우체국", "소포", "택배", "부치", "보내", "배송")
    matched = any(term in normalized for term in epost_terms)
    result = {
        "matched": matched,
        "adapter": "epost_postal_router" if matched else None,
        "service": "postal_parcel" if matched else None,
        "registered_adapters": ["epost_domestic", "epost_ems"],
    }
    await event_hub.publish(
        "status",
        {"type": "adapter_selected", "run_id": current_run_id.get(), **result},
    )
    return json.dumps(result, ensure_ascii=False)


def configure_tools(
    vault: VaultRepository,
    adapters: AdapterRegistry,
    event_hub: EventHub,
) -> None:
    global _vault, _adapters, _event_hub
    _vault = vault
    _adapters = adapters
    _event_hub = event_hub


def _dependencies() -> tuple[VaultRepository, AdapterRegistry, EventHub]:
    if _vault is None or _adapters is None or _event_hub is None:
        raise RuntimeError("우체국 도구가 초기화되지 않았습니다.")
    return _vault, _adapters, _event_hub


def clear_run_state(run_id: str) -> None:
    _stage_results.pop(run_id, None)


@function_tool
async def resolve_household_contact(relationship: str) -> str:
    """가족 관계 표현을 개인정보가 없는 내부 연락처 참조값으로 연결한다."""
    vault, _, event_hub = _dependencies()
    match = vault.resolve_relationship(relationship)
    result = (
        {"matched": False, "relationship": relationship}
        if match is None
        else {
            "matched": True,
            "contact_ref": match.contact_ref,
            "display_label": match.relationship,
        }
    )
    await event_hub.publish(
        "status",
        {
            "type": "contact_resolved",
            "run_id": current_run_id.get(),
            **result,
        },
    )
    return json.dumps(result, ensure_ascii=False)


@function_tool
async def assess_shipment_policy(recipient_ref: str, contents: str) -> str:
    """배송지와 물품을 공식 근거 기반 규칙에 대조해 접수 전 위험을 판단한다.

    개인정보 원문은 반환하지 않는다. clear_to_prepare는 법적 허가가 아니며
    이후 우체국 공식 화면 검증을 계속해야 한다.
    """
    vault, _, event_hub = _dependencies()
    recipient = vault.get_contact(recipient_ref)
    if recipient is None:
        return json.dumps(
            {"decision": "blocked", "error": "contact_not_found"},
            ensure_ascii=False,
        )
    assessment = _policy_engine.assess(recipient, contents)
    result = assessment.as_dict()
    await event_hub.publish(
        "status",
        {
            "type": "policy_assessment",
            "run_id": current_run_id.get(),
            **result,
        },
    )
    await event_hub.publish(
        "raw",
        {
            "type": "policy_assessment",
            "run_id": current_run_id.get(),
            "event": {"type": "policy_assessment", **result},
        },
    )
    return json.dumps(result, ensure_ascii=False)


@function_tool
async def stage_postal_parcel(
    recipient_ref: str,
    contents: str,
    customs_description_en: str = "",
    hs_code: str = "",
    quantity: int = 1,
    weight: str = "",
    dimensions: str = "",
    customs_value: str = "",
) -> str:
    """Route a parcel by its saved address and prepare the real postal form.

    Extract contents and quantity from the request. For EMS, provide a concise
    uppercase English customs description and an exact numeric 10 digit Korea
    Post HS code candidate. The user can review and edit both suggestions before entry.
    Leave weight, dimensions, and value empty when they were not stated.
    """
    """주소에 따라 국내소포 또는 EMS를 자동 선택해 접수 직전까지 준비한다.

    개인정보는 로컬 Vault에서 읽어 브라우저에 직접 입력한다. 모델에는 원문을
    반환하지 않는다. 사용자가 말하지 않은 무게와 크기는 빈 값으로 둔다.
    """
    vault, adapters, event_hub = _dependencies()
    run_id = current_run_id.get()
    sender = vault.get_contact("contact:self")
    recipient = vault.get_contact(recipient_ref)
    if sender is None or recipient is None:
        return json.dumps(
            {"status": "failed", "error": "contact_not_found"},
            ensure_ascii=False,
        )

    has_domestic_address = bool(recipient.get("address_domestic", "").strip())
    has_international_address = bool(
        recipient.get("address_international", "").strip()
    )
    if has_domestic_address:
        service = "domestic_parcel"
        route_reason = "recipient_has_domestic_address"
    elif has_international_address:
        service = "ems"
        route_reason = "recipient_has_international_address"
    else:
        return json.dumps(
            {"status": "failed", "error": "recipient_address_not_found"},
            ensure_ascii=False,
        )

    await event_hub.publish(
        "status",
        {
            "type": "service_routed",
            "run_id": run_id,
            "service": service,
            "route_reason": route_reason,
            "country": "US" if service == "ems" else "KR",
        },
    )

    result = await adapters.get(service).stage(
        run_id=run_id,
        sender=sender,
        recipient=recipient,
        shipment={
            "contents": contents.strip(),
            "customs_description_en": customs_description_en.strip(),
            "hs_code": hs_code.strip(),
            "quantity": max(1, quantity),
            "weight": weight.strip(),
            "dimensions": dimensions.strip(),
            "customs_value": customs_value.strip(),
        },
    )
    _stage_results[run_id] = result
    await event_hub.publish(
        "status",
        {
            "type": "stage_result",
            "run_id": run_id,
            "status": result.status,
            "service": result.service,
            "current_step": result.current_step,
            "missing_fields": result.missing_fields,
            "evidence": result.evidence,
        },
    )
    return json.dumps(asdict(result), ensure_ascii=False)


@function_tool
async def verify_epost_stage() -> str:
    """실제 화면이 접수신청 직전이며 최종 버튼이 클릭되지 않았는지 검증한다."""
    _, adapters, event_hub = _dependencies()
    run_id = current_run_id.get()
    result = _stage_results.get(run_id)
    if result is None:
        verification = {"verified": False, "error": "no_stage_result"}
    else:
        verification = await adapters.get(result.service).verify(result)
    await event_hub.publish(
        "status",
        {"type": "verification", "run_id": run_id, **verification},
    )
    await event_hub.publish(
        "raw",
        {
            "type": "safety_boundary",
            "run_id": run_id,
            "event": {
                "type": "safety_boundary",
                "verified": verification.get("verified", False),
                "final_button_clicked": False,
            },
        },
    )
    return json.dumps(verification, ensure_ascii=False)


EPOST_TOOLS = [
    select_service_adapter,
    resolve_household_contact,
    assess_shipment_policy,
    stage_postal_parcel,
    verify_epost_stage,
]
