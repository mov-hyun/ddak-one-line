import json
from pathlib import Path

import pytest

from app.adapters import AdapterRegistry
from app.adapters.epost import EpostAdapter
from app.agent import AGENT_INSTRUCTIONS, _meter_response, build_agent
from app.budget import CostGuard
from app.config import settings
from app.events import EventHub
from app.main import _ocr_contact_payload, _run_messages
from app.schemas import OcrPostalParty
from app.shipping_policy import ShippingPolicyEngine
from app.tools import (
    assess_shipment_policy,
    clear_run_state,
    configure_tools,
    current_run_id,
    resolve_household_contact,
    select_service_adapter,
    stage_postal_parcel,
    verify_epost_stage,
)
from app.vault import VaultRepository


def build_test_dependencies(tmp_path: Path):
    hub = EventHub()
    vault = VaultRepository(tmp_path / "vault.db", tmp_path / ".vault.key")
    adapters = AdapterRegistry("simulated", hub)
    configure_tools(vault, adapters, hub)
    return hub, vault, adapters


def test_agent_has_epost_pipeline_tools() -> None:
    agent = build_agent(settings)
    tool_names = {tool.name for tool in agent.tools}
    assert tool_names == {
        "select_service_adapter",
        "resolve_household_contact",
        "assess_shipment_policy",
        "stage_postal_parcel",
        "verify_epost_stage",
    }
    assert "범용" not in agent.name
    assert "접수신청 버튼을 절대 누르지 않는다" in AGENT_INSTRUCTIONS


@pytest.mark.asyncio
async def test_adapter_registry_rejects_unrelated_goal(tmp_path: Path) -> None:
    _, _, _ = build_test_dependencies(tmp_path)
    token = current_run_id.set("adapter-mismatch")
    try:
        result = json.loads(await select_service_adapter.__wrapped__("정부24에서 주민등록등본 떼줘"))
    finally:
        clear_run_state("adapter-mismatch")
        current_run_id.reset(token)
    assert result["matched"] is False
    assert result["registered_adapters"] == ["epost_domestic", "epost_ems"]


def test_vault_resolves_relationship_without_exposing_payload(tmp_path: Path) -> None:
    vault = VaultRepository(tmp_path / "vault.db", tmp_path / ".vault.key")
    match = vault.resolve_relationship("큰딸")
    assert match is not None
    assert match.contact_ref == "contact:eldest_daughter"
    assert not hasattr(match, "phone")


@pytest.mark.asyncio
async def test_contact_tool_returns_reference_only(tmp_path: Path) -> None:
    _, _, _ = build_test_dependencies(tmp_path)
    run_id = "contact-test"
    token = current_run_id.set(run_id)
    try:
        result = json.loads(await resolve_household_contact.__wrapped__("큰딸"))
    finally:
        clear_run_state(run_id)
        current_run_id.reset(token)
    assert result == {
        "matched": True,
        "contact_ref": "contact:eldest_daughter",
        "display_label": "큰딸",
    }
    assert "010-" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_policy_blocks_fresh_apples_to_us_before_browser(tmp_path: Path) -> None:
    hub, _, _ = build_test_dependencies(tmp_path)
    run_id = "policy-fresh-apple"
    token = current_run_id.set(run_id)
    try:
        result = json.loads(
            await assess_shipment_policy.__wrapped__(
                recipient_ref="contact:younger_daughter",
                contents="생사과 한 상자",
            )
        )
    finally:
        clear_run_state(run_id)
        current_run_id.reset(token)

    assert result["decision"] == "blocked"
    assert result["destination_country"] == "US"
    assert result["category"] == "fresh_produce"
    assert result["rule_id"] == "us_fresh_produce"
    assert "010-" not in json.dumps(result, ensure_ascii=False)
    assert hub.history("status")[-1]["type"] == "policy_assessment"


def test_policy_allows_ordinary_domestic_goods_to_continue() -> None:
    engine = ShippingPolicyEngine()
    result = engine.assess(
        {"address_domestic": "서울", "address_international": ""},
        "도자기 머그컵 2개",
    )
    assert result.decision == "clear_to_prepare"
    assert result.service == "domestic_parcel"


@pytest.mark.asyncio
async def test_domestic_pipeline_stops_before_submission(tmp_path: Path) -> None:
    _, _, _ = build_test_dependencies(tmp_path)
    run_id = "domestic-safe-stop"
    token = current_run_id.set(run_id)
    try:
        staged = json.loads(
            await stage_postal_parcel.__wrapped__(
                recipient_ref="contact:eldest_daughter",
                contents="사과",
            )
        )
        verified = json.loads(await verify_epost_stage.__wrapped__())
    finally:
        clear_run_state(run_id)
        current_run_id.reset(token)

    assert staged["status"] == "staged"
    assert staged["current_step"] == "final_submission_boundary"
    assert staged["final_button_text"] == "접수신청"
    assert staged["final_button_clicked"] is False
    assert verified["verified"] is True
    assert verified["submission_occurred"] is False


@pytest.mark.asyncio
async def test_us_address_routes_to_ems_and_requests_only_customs_data(tmp_path: Path) -> None:
    hub, _, _ = build_test_dependencies(tmp_path)
    run_id = "ems-auto-route"
    token = current_run_id.set(run_id)
    try:
        staged = json.loads(
            await stage_postal_parcel.__wrapped__(
                recipient_ref="contact:younger_daughter",
                contents="사과",
                customs_description_en="FRESH APPLES",
                hs_code="0808100000",
            )
        )
    finally:
        clear_run_state(run_id)
        current_run_id.reset(token)

    assert staged["status"] == "needs_information"
    assert staged["service"] == "ems"
    assert staged["missing_fields"] == [
        "recipient.email",
        "shipment.weight",
        "shipment.customs_value",
    ]
    route = next(
        event for event in hub.history("status")
        if event.get("type") == "service_routed"
    )
    assert route["service"] == "ems"
    assert route["country"] == "US"
    assert route["route_reason"] == "recipient_has_international_address"


@pytest.mark.asyncio
async def test_unregistered_contact_fails_without_guessing(tmp_path: Path) -> None:
    _, _, _ = build_test_dependencies(tmp_path)
    token = current_run_id.set("missing-contact")
    try:
        result = json.loads(
            await stage_postal_parcel.__wrapped__(
                recipient_ref="contact:unknown",
                contents="사과",
            )
        )
    finally:
        clear_run_state("missing-contact")
        current_run_id.reset(token)
    assert result["status"] == "failed"
    assert result["error"] == "contact_not_found"


def test_cost_guard_has_hard_stop(tmp_path: Path) -> None:
    guard = CostGuard(tmp_path / "cost.json", settings.openai_model, 35)
    assert guard.record("test", 1000, 1000) > 0
    assert guard.snapshot().remaining_usd < 35


def test_sdk_response_usage_fallback_is_deduplicated() -> None:
    metered: set[str] = set()
    early_response = {"id": "resp_test", "usage": None}
    response = {
        "id": "resp_test",
        "usage": {"input_tokens": 966, "output_tokens": 23},
    }
    assert _meter_response(early_response, metered) == (0, 0)
    assert metered == set()
    assert _meter_response(response, metered) == (966, 23)
    assert _meter_response(response, metered) == (0, 0)


def test_run_websocket_protocol_maps_only_product_events() -> None:
    assert _run_messages({"type": "browser_frame", "frame_data": "jpeg"}) == [
        {"type": "frame", "data": "jpeg", "url": None, "viewport": None, "stream_mode": None}
    ]
    assert _run_messages(
        {"type": "final_edge", "x": 0.1, "y": 0.7, "w": 0.8, "h": 0.1}
    ) == [{"type": "edge", "x": 0.1, "y": 0.7, "w": 0.8, "h": 0.1}]
    assert _run_messages(
        {"type": "browser_step", "step": "sender", "label": "보내는 분 정보 입력", "progress": 43}
    ) == [{
        "type": "step",
        "step": "sender",
        "label": "보내는 분 정보 입력",
        "progress": 43,
        "url": None,
        "timestamp": None,
    }]
    assert _run_messages({
        "type": "stage_result",
        "status": "needs_information",
        "current_step": "required_information",
        "missing_fields": ["recipient.address_domestic"],
    }) == [{
        "type": "stage_result",
        "status": "needs_information",
        "current_step": "required_information",
        "missing_fields": ["recipient.address_domestic"],
        "evidence": {},
        "timestamp": None,
    }]
    assert _run_messages({"type": "verification", "verified": False}) == []
    policy_message = _run_messages({
        "type": "policy_assessment",
        "decision": "blocked",
        "destination_country": "US",
        "service": "ems",
        "category": "fresh_produce",
        "plain_summary": "생사과는 미국으로 바로 보낼 수 없어요.",
        "reason": "검역 제한",
        "next_action": "대체품 선택",
        "rule_id": "us_fresh_produce",
        "source_title": "USDA APHIS",
        "source_url": "https://www.aphis.usda.gov/",
    })[0]
    assert policy_message["type"] == "policy"
    assert policy_message["decision"] == "blocked"
    assert policy_message["source_title"] == "USDA APHIS"
    assert _run_messages({
        "type": "required_terms_summary",
        "summary": "핵심 한 문장",
        "duration_ms": 5000,
    }) == [{"type": "terms_summary", "text": "핵심 한 문장", "duration_ms": 5000}]
    assert _run_messages({"type": "verification", "verified": True}) == [
        {"type": "state", "value": "stopped"},
        {
            "type": "verdict",
            "text": "아래 접수신청 버튼만 누르면 실제 접수가 되기에 중단했습니다.",
        },
    ]


@pytest.mark.asyncio
async def test_screenshot_failure_falls_back_to_cdp(monkeypatch) -> None:
    hub = EventHub()
    adapter = EpostAdapter("domestic_parcel", "live", hub)
    session = object()
    adapter._sessions["fallback"] = session  # type: ignore[assignment]
    cdp_called = False

    async def fail_polling(run_id, current_session) -> None:
        raise RuntimeError("forced_screenshot_failure")

    async def record_cdp(run_id, current_session) -> None:
        nonlocal cdp_called
        cdp_called = True

    monkeypatch.setattr(adapter, "_stream_browser_polling", fail_polling)
    monkeypatch.setattr(adapter, "_stream_browser_cdp", record_cdp)

    await adapter._stream_browser("fallback", session)  # type: ignore[arg-type]

    assert cdp_called is True
    assert any(
        event.get("type") == "stream_fallback"
        and event.get("mode") == "cdp"
        for event in hub.history("status")
    )


def test_product_ui_is_bright_two_thirds_live_workspace() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "web" / "demo.html").read_text(encoding="utf-8")
    javascript = (root / "web" / "app.js").read_text(encoding="utf-8")
    styles = (root / "web" / "styles.css").read_text(encoding="utf-8")
    adapter = (root / "app" / "adapters" / "epost.py").read_text(encoding="utf-8")

    assert 'class="live-workspace"' in html
    assert 'class="control-panel"' in html
    assert 'id="runButton"' in html
    assert "보내도 되는지부터" in html
    assert 'id="decisionPanel"' in html
    assert "RAW API 열기" in html
    assert 'id="q"' in html
    assert 'id="openHandoff"' in html
    assert 'target="_blank"' in html
    assert 'id="rawPopup"' in html
    assert 'id="rawToggle"' in html
    assert 'id="termsPopup"' in html
    assert 'id="missingInfoPopup"' in html
    assert 'id="emsCustomsForm"' in html
    assert 'id="emsWeight"' in html
    assert 'id="emsValue"' in html
    assert 'id="emsRecipientEmail"' in html
    assert 'id="emsCustomsDescription"' in html
    assert 'id="emsHsCode"' in html
    assert 'id="emsQuantity"' in html
    assert 'id="ocrOpen"' in html
    assert 'id="ocrFile"' in html
    assert 'id="ocrReviewForm"' in html
    assert "EMS 계속하기" in html
    assert 'href="/raw"' not in html
    assert "/ws/run" in javascript
    assert "/api/runs/${encodeURIComponent(activeRunId)}/click" in javascript
    assert "let sent = false" in javascript
    assert "/ws/raw?history=1&wrapped=1" in javascript
    assert "showTermsSummary" in javascript
    assert "needsInformation" in javascript
    assert "/ems-customs" in javascript
    assert "weight_kg" in javascript
    assert "customs_value_usd" in javascript
    assert "customs_description_en" in javascript
    assert "hs_code" in javascript
    assert "showPolicyDecision" in javascript
    assert "/api/ocr/address-note" in javascript
    assert "/api/ocr/contacts" in javascript
    assert ".decision-panel.is-blocked" in styles
    assert "grid-template-columns: minmax(0, 2fr)" in styles
    assert ".browser-screen" in styles
    assert "object-fit: contain" in styles
    assert "Math.min(screen.clientWidth / viewport.width" in javascript
    assert "click_preview" in adapter
    assert "phone-frame" not in html


def test_ocr_contact_allows_blank_country_and_defaults_to_domestic() -> None:
    party = OcrPostalParty(
        name_ko="테스트수신인",
        address="가상시 가상구 테스트로 1",
        postal_code="00000",
        phone="010-0000-0000",
        country_code="",
    )
    payload = _ocr_contact_payload(
        "recipient",
        party.model_dump(mode="json"),
        "쪽지 받는 분",
    )
    assert payload["address_domestic"] == "가상시 가상구 테스트로 1"
    assert payload["address_international"] == ""


def test_ems_customs_values_are_normalized_without_product_constants() -> None:
    assert EpostAdapter._customs_description("  ceramic mugs  ") == "CERAMIC MUGS"
    assert EpostAdapter._hs_code("6912.00.4400") == "6912004400"
    with pytest.raises(ValueError):
        EpostAdapter._hs_code("691200")


def test_manual_handoff_requires_explicit_user_confirmation() -> None:
    root = Path(__file__).resolve().parents[1]
    handoff = (root / "web" / "handoff.html").read_text(encoding="utf-8")
    adapter = (root / "app" / "adapters" / "epost.py").read_text(encoding="utf-8")

    assert "실제 접수신청" in handoff
    assert "접수하지 않기" in handoff
    assert "confirmed: true" in handoff
    assert "우체국 최종 확인" in handoff
    assert "if not confirmed" in adapter
    assert "먼저 사용자 인계 화면을 열어 주세요" in adapter
