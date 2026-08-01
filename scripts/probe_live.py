from __future__ import annotations

import asyncio
import json

from app.main import adapters, event_hub, vault


async def main() -> None:
    adapter = adapters.get("domestic_parcel")
    queue = await event_hub.subscribe("status")
    frames = 0
    stop_collecting = asyncio.Event()

    async def collect_frames() -> None:
        nonlocal frames
        while not stop_collecting.is_set():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.25)
            except TimeoutError:
                continue
            if event.get("run_id") == "live-probe" and event.get("type") == "browser_frame":
                frames += 1

    collector = asyncio.create_task(collect_frames())
    result = await adapter.stage(
        run_id="live-probe",
        sender=vault.get_contact("contact:self"),
        recipient=vault.get_contact("contact:eldest_daughter"),
        shipment={"contents": "사과"},
    )
    print(
        json.dumps(
            {
                "status": result.status,
                "step": result.current_step,
                "evidence": result.evidence,
            },
            ensure_ascii=False,
        )
    )
    verification = await adapter.verify(result)
    print(json.dumps(verification, ensure_ascii=False))
    await asyncio.sleep(0.5)
    stop_collecting.set()
    await collector
    modes = [
        event.get("mode")
        for event in event_hub.history("status")
        if event.get("run_id") == "live-probe" and event.get("type") == "stream_mode"
    ]
    edges = [
        event
        for event in event_hub.history("status")
        if event.get("run_id") == "live-probe" and event.get("type") == "final_edge"
    ]
    print(json.dumps({"frames": frames, "modes": modes, "edge": edges[-1] if edges else None}, ensure_ascii=False))
    assert frames > 0
    assert verification.get("stream_mode") == "screenshot_polling"
    assert "screenshot_polling" in modes
    assert edges
    assert 0 <= edges[-1]["x"] < 1
    assert 0 <= edges[-1]["y"] < 1
    await event_hub.unsubscribe("status", queue)
    await adapter.close_all_sessions()


if __name__ == "__main__":
    asyncio.run(main())
