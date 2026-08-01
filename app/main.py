from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.adapters import AdapterRegistry
from app.agent import build_agent, run_goal
from app.budget import CostGuard
from app.config import settings
from app.events import event_hub
from app.schemas import (
    BrowserClickRequest,
    DialogDecisionRequest,
    EmsCustomsRequest,
    ManualSubmitRequest,
    RunAccepted,
    RunRequest,
)
from app.tools import configure_tools
from app.vault import VaultRepository


vault = VaultRepository(settings.data_dir / "vault.db", settings.data_dir / ".vault.key")
adapters = AdapterRegistry(settings.browser_mode, event_hub)
cost_guard = CostGuard(
    settings.data_dir / "cost_ledger.json",
    settings.openai_model,
    settings.max_spend_usd,
)
configure_tools(vault, adapters, event_hub)
agent = build_agent(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await adapters.close_all()


app = FastAPI(title="딱한줄 우체국 소포", version="1.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=settings.web_dir), name="static")
app.mount(
    "/visual-assets",
    StaticFiles(directory=settings.root / "output" / "imagegen"),
    name="visual-assets",
)


def _page(name: str) -> FileResponse:
    return FileResponse(settings.web_dir / name)


@app.get("/")
@app.get("/demo")
@app.get("/stage")
@app.get("/console")
async def demo() -> FileResponse:
    return _page("demo.html")


@app.get("/raw")
async def raw() -> FileResponse:
    return _page("raw.html")


@app.get("/handoff")
async def handoff() -> FileResponse:
    return _page("handoff.html")


def _handoff_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


@app.post("/api/handoff/{run_id}/activate")
async def activate_handoff(run_id: str) -> dict:
    try:
        return await adapters.activate_handoff(run_id)
    except Exception as exc:
        raise _handoff_error(exc) from exc


@app.post("/api/handoff/{run_id}/submit")
async def submit_handoff(run_id: str, request: ManualSubmitRequest) -> dict:
    try:
        return await adapters.submit_final(run_id, confirmed=request.confirmed)
    except Exception as exc:
        raise _handoff_error(exc) from exc


@app.post("/api/handoff/{run_id}/dialog")
async def resolve_handoff_dialog(run_id: str, request: DialogDecisionRequest) -> dict:
    try:
        return await adapters.resolve_dialog(run_id, request.accept)
    except Exception as exc:
        raise _handoff_error(exc) from exc


@app.post("/api/handoff/{run_id}/cancel")
async def cancel_handoff(run_id: str) -> dict:
    try:
        return await adapters.cancel_handoff(run_id)
    except Exception as exc:
        raise _handoff_error(exc) from exc


@app.post("/api/runs/{run_id}/click")
async def click_run_browser(run_id: str, request: BrowserClickRequest) -> dict:
    try:
        return await adapters.click_preview(run_id, request.x, request.y)
    except Exception as exc:
        raise _handoff_error(exc) from exc


@app.post("/api/runs/{run_id}/ems-customs")
async def resume_ems_customs(run_id: str, request: EmsCustomsRequest) -> dict:
    try:
        return await adapters.resume_ems_customs(
            run_id,
            weight_kg=request.weight_kg,
            customs_value_usd=request.customs_value_usd,
            recipient_email=request.recipient_email,
            customs_description_en=request.customs_description_en,
            hs_code=request.hs_code,
            quantity=request.quantity,
        )
    except Exception as exc:
        raise _handoff_error(exc) from exc


@app.get("/health")
async def health() -> dict:
    budget = cost_guard.snapshot()
    vault_health = vault.health()
    adapter = adapters.get("domestic_parcel")
    missing_fields = adapter._missing(
        vault.get_contact("contact:self") or {},
        vault.get_contact("contact:eldest_daughter") or {},
        {"contents": "사과"},
    )
    return {
        "ready": settings.has_openai_key and vault_health["ready"] and not missing_fields,
        "openai_key": settings.has_openai_key,
        "model": settings.openai_model,
        "browser_mode": settings.browser_mode,
        "browser_visible": settings.browser_mode == "live" and not settings.browser_headless,
        "live_step_delay_ms": settings.live_step_delay_ms,
        "vault": vault_health,
        "budget": {
            "spent_usd": str(budget.spent_usd),
            "limit_usd": str(budget.limit_usd),
            "remaining_usd": str(budget.remaining_usd),
        },
        "services": ["domestic_parcel", "ems"],
        "final_action_blocked": True,
        "manual_handoff_available": True,
        "required_fields_complete": not missing_fields,
        "missing_fields": missing_fields,
    }


@app.post("/api/runs", response_model=RunAccepted)
async def create_run(request: RunRequest) -> RunAccepted:
    run_id = uuid.uuid4().hex[:12]
    asyncio.create_task(
        run_goal(
            run_id=run_id,
            goal=request.goal,
            agent=agent,
            event_hub=event_hub,
            cost_guard=cost_guard,
        )
    )
    return RunAccepted(run_id=run_id)


@app.get("/api/events/{channel}")
async def event_history(channel: str) -> list[dict]:
    if channel not in {"status", "raw"}:
        return []
    return event_hub.history(channel)


def _run_messages(event: dict) -> list[dict]:
    event_type = event.get("type")
    if event_type == "run_started":
        return [{"type": "state", "value": "running", "timestamp": event.get("timestamp")}]
    if event_type == "browser_frame" and event.get("frame_data"):
        return [{
            "type": "frame",
            "data": event["frame_data"],
            "url": event.get("url"),
            "viewport": event.get("viewport"),
            "stream_mode": event.get("stream_mode"),
        }]
    if event_type == "final_edge":
        return [
            {
                "type": "edge",
                "x": event["x"],
                "y": event["y"],
                "w": event["w"],
                "h": event["h"],
            }
        ]
    if event_type == "browser_step":
        return [
            {
                "type": "step",
                "step": event.get("step"),
                "label": event.get("label"),
                "progress": event.get("progress", 0),
                "url": event.get("url"),
                "timestamp": event.get("timestamp"),
            }
        ]
    if event_type == "required_terms_summary":
        return [{
            "type": "terms_summary",
            "text": event.get("summary"),
            "duration_ms": event.get("duration_ms", 5000),
        }]
    if event_type == "adapter_selected":
        return [{
            "type": "adapter",
            "matched": event.get("matched", False),
            "adapter": event.get("adapter"),
            "registered_adapters": event.get("registered_adapters", []),
            "timestamp": event.get("timestamp"),
        }]
    if event_type == "service_routed":
        return [{
            "type": "route",
            "service": event.get("service"),
            "route_reason": event.get("route_reason"),
            "country": event.get("country"),
            "timestamp": event.get("timestamp"),
        }]
    if event_type == "contact_resolved":
        return [{
            "type": "contact",
            "matched": event.get("matched", False),
            "contact_ref": event.get("contact_ref"),
            "display_label": event.get("display_label"),
            "timestamp": event.get("timestamp"),
        }]
    if event_type == "policy_assessment":
        return [{
            "type": "policy",
            "decision": event.get("decision"),
            "destination_country": event.get("destination_country"),
            "service": event.get("service"),
            "category": event.get("category"),
            "plain_summary": event.get("plain_summary"),
            "reason": event.get("reason"),
            "next_action": event.get("next_action"),
            "rule_id": event.get("rule_id"),
            "source_title": event.get("source_title"),
            "source_url": event.get("source_url"),
            "timestamp": event.get("timestamp"),
        }]
    if event_type == "stage_result":
        return [{
            "type": "stage_result",
            "status": event.get("status"),
            "current_step": event.get("current_step"),
            "missing_fields": event.get("missing_fields", []),
            "evidence": event.get("evidence", {}),
            "timestamp": event.get("timestamp"),
        }]
    if event_type == "safe_stop":
        return [{
            "type": "safe_stop",
            "button_text": event.get("button_text"),
            "message": event.get("message"),
            "timestamp": event.get("timestamp"),
        }]
    if event_type == "agent_item":
        return [{"type": "agent", "name": event.get("name", "agent_event")}]
    if event_type == "verification" and event.get("verified") is True:
        return [
            {"type": "state", "value": "stopped"},
            {
                "type": "verdict",
                "text": "아래 접수신청 버튼만 누르면 실제 접수가 되기에 중단했습니다.",
            },
        ]
    if event_type == "run_failed":
        return [{"type": "error", "text": "우체국 접수를 완료하지 못했습니다."}]
    if event_type == "run_completed":
        return [
            {
                "type": "completed",
                "input_tokens": event.get("input_tokens", 0),
                "output_tokens": event.get("output_tokens", 0),
                "cost": event.get("estimated_cost_usd", "0"),
                "output": event.get("output", ""),
            }
        ]
    return []


@app.websocket("/ws/run")
async def run_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = await event_hub.subscribe("status")
    run_id = ""
    try:
        first = await websocket.receive_json()
        if first.get("type") != "goal" or not str(first.get("text") or "").strip():
            await websocket.send_json({"type": "error", "text": "한 줄 목표가 필요합니다."})
            return
        goal = str(first["text"]).strip()[:300]
        run_id = uuid.uuid4().hex[:12]
        await websocket.send_json(
            {
                "type": "run",
                "run_id": run_id,
                "model": settings.openai_model,
                "browser": settings.browser_mode,
            }
        )
        asyncio.create_task(
            run_goal(
                run_id=run_id,
                goal=goal,
                agent=agent,
                event_hub=event_hub,
                cost_guard=cost_guard,
            )
        )
        while True:
            event = await queue.get()
            if event.get("run_id") != run_id:
                continue
            for message in _run_messages(event):
                await websocket.send_json(message)
            if event.get("type") in {"run_completed", "run_failed"}:
                break
    except WebSocketDisconnect:
        pass
    finally:
        await event_hub.unsubscribe("status", queue)


@app.websocket("/ws/raw")
async def raw_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = await event_hub.subscribe("raw")
    include_history = websocket.query_params.get("history", "1") != "0"
    wrapped = websocket.query_params.get("wrapped", "0") == "1"
    try:
        if include_history:
            for wrapper in event_hub.history("raw"):
                payload = wrapper if wrapped else wrapper.get("event", wrapper)
                await websocket.send_text(json.dumps(payload, ensure_ascii=False))
        while True:
            wrapper = await queue.get()
            payload = wrapper if wrapped else wrapper.get("event", wrapper)
            await websocket.send_text(json.dumps(payload, ensure_ascii=False))
    except WebSocketDisconnect:
        pass
    finally:
        await event_hub.unsubscribe("raw", queue)


@app.websocket("/ws/handoff/{run_id}")
async def handoff_socket(websocket: WebSocket, run_id: str) -> None:
    await websocket.accept()
    queue = await event_hub.subscribe("status")
    try:
        while True:
            event = await queue.get()
            if event.get("run_id") != run_id:
                continue
            event_type = event.get("type")
            if event_type == "browser_frame" and event.get("frame_data"):
                await websocket.send_json(
                    {
                        "type": "frame",
                        "data": event["frame_data"],
                        "url": event.get("url"),
                    }
                )
            elif event_type == "browser_dialog":
                await websocket.send_json(
                    {
                        "type": "dialog",
                        "message": event.get("message", "우체국 확인이 필요합니다."),
                    }
                )
            elif event_type in {"manual_handoff_cancelled", "browser_dialog_resolved"}:
                await websocket.send_json({"type": event_type, **event})
    except WebSocketDisconnect:
        pass
    finally:
        await event_hub.unsubscribe("status", queue)


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
