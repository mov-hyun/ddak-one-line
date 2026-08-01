from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    output = Path("output/console/console-idle-1280x720.png")
    output.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto("http://127.0.0.1:8000/", wait_until="networkidle")
        assert page.locator(".pipeline-panel").is_visible()
        assert page.locator(".browser-panel").is_visible()
        assert page.locator(".raw-panel").is_visible()
        assert page.locator("#runButton").is_visible()
        assert page.evaluate("document.body.scrollWidth") == 1280
        assert page.evaluate("document.body.scrollHeight") == 720
        page.screenshot(path=str(output))
        browser.close()


if __name__ == "__main__":
    main()
