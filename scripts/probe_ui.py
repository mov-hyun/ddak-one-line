from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright


VERDICT = "아래 접수신청 버튼만 누르면 실제 접수가 되기에 중단했습니다."


def main() -> None:
    output = Path("output/playwright")
    output.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1078, "height": 908})
        page.goto("http://127.0.0.1:8000/", wait_until="networkidle")

        assert page.locator("body[data-state='idle']").count() == 1
        assert page.locator("input:visible").count() == 1
        assert page.locator("#runButton:visible").count() == 1
        assert page.locator(".pipeline-panel").count() == 1
        assert page.locator(".browser-panel").count() == 1
        assert page.locator(".raw-panel").count() == 1
        page.screenshot(path=str(output / "console-idle-1078x908.png"))

        page.locator("#goalInput").fill("큰딸에게 사과를 소포로 부치고 싶어.")
        page.locator("#runButton").click()
        page.locator("#frozenGoal:visible").wait_for()
        page.locator("body[data-state='running']").wait_for()
        page.wait_for_function("document.querySelector('#frame').src.startsWith('data:image/jpeg;base64,')", timeout=60_000)
        page.wait_for_timeout(3_000)
        page.screenshot(path=str(output / "console-running-1078x908.png"))

        page.locator("body[data-state='stopped']").wait_for(timeout=150_000)
        assert page.locator("#stopCaption").inner_text() == VERDICT
        assert page.locator("[data-step='halt'].is-halt").count() == 1
        assert float(page.locator("#edge").evaluate("e => getComputedStyle(e).opacity")) > 0.9
        page.screenshot(path=str(output / "console-stopped-1078x908.png"))

        raw_events = page.request.get("http://127.0.0.1:8000/api/events/raw").json()
        raw_text = json.dumps(raw_events, ensure_ascii=False)
        for private_value in (
            "홍길동",
            "임꺽정",
            "010-1111-1111",
            "010-2222-2222",
            "사평대로57길",
            "사직로 161",
        ):
            assert private_value not in raw_text

        status_events = page.request.get("http://127.0.0.1:8000/api/events/status").json()
        verifications = [event for event in status_events if event.get("type") == "verification"]
        assert verifications
        run_id = verifications[-1]["run_id"]
        page.wait_for_timeout(5_000)
        status_events = page.request.get("http://127.0.0.1:8000/api/events/status").json()
        completed = [
            event
            for event in status_events
            if event.get("type") == "run_completed" and event.get("run_id") == run_id
        ]
        assert completed
        run_events = [event for event in status_events if event.get("run_id") == run_id]
        assert any(event.get("type") == "stream_mode" and event.get("mode") == "screenshot_polling" for event in run_events)
        assert not any(event.get("type") == "stream_fallback" for event in run_events)
        assert any(event.get("type") == "verification" and event.get("verified") is True for event in run_events)
        assert float(completed[-1]["estimated_cost_usd"]) >= 0

        print(json.dumps({"ui": "ok", "run_id": run_id, "raw_private_values": False}, ensure_ascii=False))
        browser.close()


if __name__ == "__main__":
    main()
