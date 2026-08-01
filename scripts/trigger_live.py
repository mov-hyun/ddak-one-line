from __future__ import annotations

import json
import time

import httpx


def main() -> None:
    base = "http://127.0.0.1:8000"
    run = httpx.post(
        f"{base}/api/runs",
        json={"goal": "큰딸에게 사과를 소포로 부치고 싶어."},
        timeout=10,
    ).json()
    done = None
    handoff = None
    started = time.monotonic()
    while time.monotonic() - started < 55:
        time.sleep(0.8)
        events = httpx.get(f"{base}/api/events/status", timeout=10).json()
        done = next(
            (
                event
                for event in reversed(events)
                if event.get("run_id") == run["run_id"]
                and event.get("type") in {"run_completed", "run_failed"}
            ),
            None,
        )
        handoff = next(
            (
                event
                for event in reversed(events)
                if event.get("run_id") == run["run_id"]
                and event.get("type") == "manual_handoff"
            ),
            None,
        )
        if done:
            break
    print(
        json.dumps(
            {"run_id": run["run_id"], "done": done, "handoff": handoff},
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()
