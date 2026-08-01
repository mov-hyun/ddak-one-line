from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Dialog,
    Page,
    Playwright,
    async_playwright,
)

from app.adapters.base import SiteAdapter, StageResult
from app.events import EventHub


AGREEMENT_URL = (
    "https://service.epost.go.kr/"
    "front.commonpostplus.ParcelRetrieveAcceptPlus.postal?gubun=2"
)
EMS_AGREEMENT_URL = (
    "https://ems.epost.go.kr/front.EmsApply1100c.postal?prepayYn=N"
)


@dataclass
class LiveBrowserSession:
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    handoff_active: bool = False
    pending_dialog: Dialog | None = None
    stream_mode: str = "starting"
    pending_payload: dict[str, Any] | None = None
    dialogs: list[str] | None = None

    async def close(self) -> None:
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()


class EpostAdapter(SiteAdapter):
    def __init__(
        self,
        service: str,
        browser_mode: str,
        event_hub: EventHub,
        *,
        headless: bool = True,
        step_delay_ms: int = 650,
    ) -> None:
        self.service = service
        self.browser_mode = browser_mode
        self.event_hub = event_hub
        self.headless = headless
        self.step_delay_ms = step_delay_ms
        self._sessions: dict[str, LiveBrowserSession] = {}
        self._stream_tasks: dict[str, asyncio.Task[None]] = {}

    def required_fields(self) -> list[str]:
        if self.service == "ems":
            return [
                "sender.name_en",
                "sender.address_domestic",
                "sender.phone",
                "recipient.name_en",
                "recipient.address_international",
                "recipient.phone",
                "recipient.email",
                "shipment.contents",
                "shipment.customs_description_en",
                "shipment.hs_code",
                "shipment.weight",
                "shipment.customs_value",
            ]
        return [
            "sender.name_ko",
            "sender.address_domestic",
            "sender.phone",
            "recipient.name_ko",
            "recipient.address_domestic",
            "recipient.phone",
            "shipment.contents",
        ]

    def _missing(
        self,
        sender: dict[str, str],
        recipient: dict[str, str],
        shipment: dict[str, Any],
    ) -> list[str]:
        scopes = {"sender": sender, "recipient": recipient, "shipment": shipment}
        missing: list[str] = []
        for field in self.required_fields():
            scope, key = field.split(".", 1)
            value = scopes[scope].get(key)
            if value is None or str(value).strip() == "":
                missing.append(field)
        return missing

    async def stage(
        self,
        *,
        run_id: str,
        sender: dict[str, str],
        recipient: dict[str, str],
        shipment: dict[str, Any],
    ) -> StageResult:
        missing = self._missing(sender, recipient, shipment)
        if missing:
            if (
                self.service == "ems"
                and self.browser_mode == "live"
                and set(missing) <= {
                    "recipient.email",
                    "shipment.customs_description_en",
                    "shipment.hs_code",
                    "shipment.weight",
                    "shipment.customs_value",
                }
            ):
                await self._open_ems_live(
                    run_id=run_id,
                    sender=sender,
                    recipient=recipient,
                    shipment=shipment,
                )
            await self.event_hub.publish(
                "status",
                {
                    "type": "needs_information",
                    "run_id": run_id,
                    "service": self.service,
                    "missing_fields": missing,
                },
            )
            return StageResult(
                status="needs_information",
                service=self.service,
                current_step="required_information",
                missing_fields=missing,
                evidence={
                    "customs_description_en": shipment.get("customs_description_en", ""),
                    "hs_code": shipment.get("hs_code", ""),
                    "quantity": shipment.get("quantity", 1),
                },
            )

        if self.browser_mode == "live":
            if self.service == "ems":
                raise NotImplementedError(
                    "EMS 필수 통관정보가 확인된 뒤 실제 접수 화면 자동화를 진행합니다."
                )
            return await self._stage_domestic_live(
                run_id=run_id,
                sender=sender,
                recipient=recipient,
                shipment=shipment,
            )

        steps = [
            ("service_selected", "서비스 경로 선택", 12),
            ("agreements", "비회원 약관 확인", 28),
            ("sender", "보내는 사람 정보 입력", 46),
            ("recipient", "받는 사람 정보 입력", 64),
            ("shipment", "소포 정보 입력", 78),
            ("review", "접수정보 검증", 90),
            ("final_boundary", "접수신청 직전 안전 중단", 100),
        ]
        for step, label, progress in steps:
            await self.event_hub.publish(
                "status",
                {
                    "type": "browser_step",
                    "run_id": run_id,
                    "service": self.service,
                    "step": step,
                    "label": label,
                    "progress": progress,
                    "browser_mode": "simulated",
                },
            )
            await asyncio.sleep(0.22)

        return StageResult(
            status="staged",
            service=self.service,
            current_step="final_submission_boundary",
            evidence={
                "browser_mode": "simulated",
                "required_fields_complete": True,
                "submission_occurred": False,
            },
        )

    async def _open_ems_live(
        self,
        *,
        run_id: str,
        sender: dict[str, str],
        recipient: dict[str, str],
        shipment: dict[str, Any],
    ) -> None:
        """Open the real nonmember EMS form before asking for customs values."""
        await self.close_all_sessions()
        session = await self._launch_session()
        session.pending_payload = {
            "sender": sender,
            "recipient": recipient,
            "shipment": shipment,
        }
        self._sessions[run_id] = session
        self._stream_tasks[run_id] = asyncio.create_task(
            self._stream_browser(run_id, session)
        )
        page = session.page

        async def dismiss_dialog(dialog: Dialog) -> None:
            if session.dialogs is None:
                session.dialogs = []
            session.dialogs.append(dialog.message)
            await dialog.dismiss()

        page.on("dialog", dismiss_dialog)
        try:
            await page.goto(EMS_AGREEMENT_URL, wait_until="domcontentloaded")
            await self._publish_live_step(
                run_id=run_id,
                page=page,
                step="ems_nonmember",
                label="EMS 비회원 접수 경로 진입",
                progress=12,
                focus_selector="#rbAgree",
            )
            await self.event_hub.publish(
                "status",
                {
                    "type": "required_terms_summary",
                    "run_id": run_id,
                    "summary": "우체국이 이름·주소·전화번호를 EMS 접수와 배송에 사용합니다.",
                    "duration_ms": 5000,
                },
            )
            await page.locator("#rbAgree").check()
            await page.locator("#rbPolicy").check()
            guest_password = sender.get("guest_password", "1111")
            await page.locator("#guest_orderpw").fill(guest_password)
            await page.locator("#guest_orderpw2").fill(guest_password)
            await page.get_by_role("link", name="다음", exact=True).click()
            await page.wait_for_load_state("domcontentloaded")
            ems_link = page.get_by_role("link", name="국제특급(EMS)", exact=True)
            await ems_link.wait_for(state="visible")
            await ems_link.click()
            await page.wait_for_load_state("domcontentloaded")
            await page.locator("#emslUseGuideInfoChkBox").wait_for(state="visible")
            await self._publish_live_step(
                run_id=run_id,
                page=page,
                step="ems_selected",
                label="미국 주소 확인 · EMS 실제 접수 화면",
                progress=20,
                focus_selector="#emslUseGuideInfoChkBox",
            )
        except Exception:
            await session.close()
            self._sessions.pop(run_id, None)
            raise

    @staticmethod
    async def _force_fill(page: Page, selector: str, value: str) -> None:
        locator = page.locator(selector)
        await locator.evaluate(
            """(element, nextValue) => {
                element.removeAttribute('readonly');
                element.value = nextValue;
                element.dispatchEvent(new Event('input', {bubbles: true}));
                element.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            value,
        )

    @staticmethod
    def _international_recipient_parts(address: str) -> dict[str, str]:
        match = re.match(
            r"^(.*?),\s*([^,]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s+USA$",
            address.strip(),
            re.IGNORECASE,
        )
        if match is None:
            return {"detail": address, "city": "", "state": "", "postal": ""}
        return {
            "detail": match.group(1),
            "city": match.group(2),
            "state": match.group(3).upper(),
            "postal": match.group(4),
        }

    @staticmethod
    def _customs_description(value: str) -> str:
        normalized = re.sub(r"[^A-Z0-9 /-]", "", value.upper())
        normalized = re.sub(r"\s+", " ", normalized).strip(" /-")
        if len(normalized) < 2:
            raise ValueError("EMS 통관 품목명은 영문으로 2자 이상 입력해 주세요.")
        return normalized[:50]

    @staticmethod
    def _hs_code(value: str) -> str:
        normalized = re.sub(r"\D", "", value)
        if len(normalized) != 10:
            raise ValueError("EMS HS 코드는 우체국 검색에 사용하는 숫자 10자리여야 합니다.")
        return normalized

    @staticmethod
    async def _select_official_hs_code(page: Page, hs_code: str) -> str:
        """Select an exact result in Korea Post's HSCODE search popup."""
        async with page.expect_popup() as popup_info:
            await page.locator("#btnGoHscSrch").click()
        popup = await popup_info.value
        try:
            await popup.wait_for_load_state("domcontentloaded")
            await popup.locator("#keyword").fill(hs_code)
            async with popup.expect_navigation(wait_until="domcontentloaded"):
                await popup.locator("a[onclick*='goSrch']").click()
            exact_result = popup.get_by_text(hs_code, exact=True)
            if await exact_result.count() != 1:
                raise ValueError(
                    f"우체국 공식 HSCODE 검색에서 {hs_code}를 찾지 못했습니다. "
                    "품목에 맞는 우체국 10자리 코드를 확인해 수정해 주세요."
                )
            await exact_result.click()
            await page.wait_for_timeout(200)
        finally:
            if not popup.is_closed():
                await popup.close()

        selected = (await page.locator("#ems_hs_code").input_value()).strip()
        if selected != hs_code:
            raise RuntimeError("우체국 공식 HSCODE 선택 결과를 원래 화면에서 확인하지 못했습니다.")
        return (await page.locator("#ems_contents_EM").input_value()).strip()

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
    ) -> dict[str, Any]:
        session = self._session_for_run(run_id)
        if session is None or session.page.is_closed():
            raise LookupError("이어갈 EMS 접수 화면이 없습니다.")
        if self.service != "ems" or session.pending_payload is None:
            raise RuntimeError("EMS 추가정보 입력 단계가 아닙니다.")

        page = session.page
        payload = session.pending_payload
        sender = payload["sender"]
        recipient = payload["recipient"]
        recipient["email"] = recipient_email
        shipment = payload["shipment"]
        shipment["weight"] = str(weight_kg)
        shipment["customs_value"] = str(customs_value_usd)
        shipment["customs_description_en"] = self._customs_description(
            customs_description_en
        )
        shipment["hs_code"] = self._hs_code(hs_code)
        shipment["quantity"] = max(1, min(int(quantity), 999))

        await page.locator("#emslUseGuideInfoChkBox").check()
        await page.locator("#ems_name").fill(sender["name_en"])
        await self._force_fill(page, "#ems_add", sender.get("postal_code", ""))
        await self._force_fill(
            page,
            "#ems_add3",
            sender.get("address_base_en", sender.get("address_domestic", "")),
        )
        await page.locator("#ems_add4").fill(sender.get("address_detail_en", ""))
        await self._fill_phone(
            page,
            ("#ems_phone2", "#ems_phone3", "#ems_phone4"),
            sender["phone"],
        )
        session.dialogs = []
        await page.locator("a.btn_next").nth(0).click()
        await page.wait_for_timeout(250)
        if session.dialogs:
            raise RuntimeError("EMS 보내는 분 확인: " + session.dialogs[-1])
        await self._publish_live_step(
            run_id=run_id,
            page=page,
            step="ems_sender",
            label="EMS 보내는 분 정보 입력",
            progress=42,
            focus_selector="#receivename_eng",
        )

        parts = self._international_recipient_parts(
            recipient["address_international"]
        )
        await page.locator("#receivename_eng").fill(recipient["name_en"])
        await self._force_fill(page, "#countrycd_eng", "US")
        await self._force_fill(page, "#countrycd_eng2", "UNITED STATES OF AMERICA")
        await self._force_fill(page, "#countrycd_eng3", "US")
        await page.locator("#receivezipcode_eng").fill(parts["postal"])
        await page.locator("#receivezipcode_eng1").fill(parts["detail"])
        await page.locator("#receivezipcode_eng2").fill(parts["city"])
        await page.locator("#receivezipcode_eng3").fill(parts["state"])
        digits = re.sub(r"\D", "", recipient["phone"])
        if digits.startswith("1"):
            digits = digits[1:]
        phone_parts = ("1", digits[:-8] or "10", digits[-8:-4], digits[-4:])
        for selector, value in zip(
            (
                "#receivetelno_eng",
                "#receivetelno_eng2",
                "#receivetelno_eng3",
                "#receivetelno_eng4",
            ),
            phone_parts,
        ):
            await page.locator(selector).fill(value)
        await page.locator("#receivemail_eng").fill(recipient_email)
        session.dialogs = []
        await page.locator("a.btn_next").nth(1).click()
        await page.wait_for_timeout(250)
        if session.dialogs:
            raise RuntimeError("EMS 받는 분 확인: " + session.dialogs[-1])
        await self._publish_live_step(
            run_id=run_id,
            page=page,
            step="ems_recipient",
            label="EMS 받는 분 미국 주소 입력",
            progress=64,
            focus_selector="#non_papers",
        )

        await page.locator("#non_papers").check()
        await page.locator("#ems_EM_gubun2").check()
        await self._force_fill(page, "#ems_hs_code", shipment["hs_code"])
        await self._force_fill(
            page, "#ems_contents_EM", shipment["customs_description_en"]
        )
        await self._force_fill(page, "#ems_number_EM", str(shipment["quantity"]))
        await self._force_fill(
            page, "#ems_weight", str(int(round(weight_kg * 1000)))
        )
        await page.locator("#apprvCurrSel").select_option("USD")
        await self._force_fill(page, "#ems_value_EM", str(customs_value_usd))
        await page.locator("#nationSel").select_option("KR")
        official_description = await self._select_official_hs_code(
            page, shipment["hs_code"]
        )
        shipment["customs_description_en"] = official_description
        session.dialogs = []
        await page.locator("#addCustom").click()
        await page.wait_for_timeout(250)
        if session.dialogs:
            raise RuntimeError("EMS 세관신고 확인: " + session.dialogs[-1])
        saved_content = (await page.locator("#content_1").input_value()).strip()
        saved_weight = (await page.locator("#weight_1").input_value()).strip()
        if saved_content != shipment["customs_description_en"] or not saved_weight:
            raise RuntimeError("EMS 세관신고 내역이 실제 화면에 저장되지 않았습니다.")
        await self._publish_live_step(
            run_id=run_id,
            page=page,
            step="ems_customs_added",
            label="EMS 세관신고 내역 저장 확인",
            progress=86,
            focus_selector="#content_1",
        )
        session.dialogs = []
        await page.locator("a.btn_next").nth(2).click()
        await page.wait_for_timeout(300)
        if session.dialogs:
            raise RuntimeError("EMS 세관신고 단계 확인: " + session.dialogs[-1])
        await self._publish_live_step(
            run_id=run_id,
            page=page,
            step="ems_optional_information",
            label="EMS 세관신고 완료 · 부가정보 단계 이동",
            progress=94,
            focus_selector="#btnAddTemp",
        )
        # The original run websocket has already completed while the UI waits for
        # the extra EMS values. Return a fresh frame with the POST response so the
        # stage cannot keep showing the stale, top-of-form screenshot.
        await page.wait_for_timeout(350)
        shot = await page.screenshot(type="jpeg", quality=92, full_page=False)
        session.pending_payload = None
        return {
            "status": "continued",
            "service": "ems",
            "current_step": "ems_optional_information",
            "submission_occurred": False,
            "url": page.url,
            "frame_data": base64.b64encode(shot).decode("ascii"),
            "viewport": page.viewport_size or {"width": 1100, "height": 720},
        }

    async def _launch_session(self) -> LiveBrowserSession:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            channel="chrome",
            headless=self.headless,
            args=[
                "--disable-popup-blocking",
                "--window-position=32,32",
                "--window-size=1280,900",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1100, "height": 720},
            device_scale_factor=1,
            is_mobile=False,
            has_touch=False,
            locale="ko-KR",
            timezone_id="Asia/Seoul",
        )
        page = await context.new_page()
        page.set_default_timeout(15_000)
        return LiveBrowserSession(playwright, browser, context, page)

    async def close_all_sessions(self) -> None:
        stream_tasks = list(self._stream_tasks.values())
        self._stream_tasks.clear()
        for task in stream_tasks:
            task.cancel()
        if stream_tasks:
            await asyncio.gather(*stream_tasks, return_exceptions=True)
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            try:
                await session.close()
            except Exception:
                pass

    async def _publish_frame(
        self,
        run_id: str,
        session: LiveBrowserSession,
        frame_data: str,
    ) -> None:
        await self.event_hub.publish(
            "status",
            {
                "type": "browser_frame",
                "run_id": run_id,
                "service": self.service,
                "step": "interactive_live",
                "image": "data:image/jpeg;base64," + frame_data,
                "frame_data": frame_data,
                "url": session.page.url,
                "interactive": False,
                "viewport": session.page.viewport_size or {"width": 390, "height": 844},
                "stream_mode": session.stream_mode,
            },
            store=False,
        )

    async def _stream_browser_cdp(
        self, run_id: str, session: LiveBrowserSession
    ) -> None:
        cdp = await session.context.new_cdp_session(session.page)
        frames: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        last_accepted = 0.0

        async def handle_frame(params: dict[str, Any]) -> None:
            nonlocal last_accepted
            try:
                await cdp.send(
                    "Page.screencastFrameAck",
                    {"sessionId": params["sessionId"]},
                )
                now = time.monotonic()
                if now - last_accepted < 1 / 12:
                    return
                last_accepted = now
                data = str(params.get("data") or "")
                if not data:
                    return
                if frames.full():
                    try:
                        frames.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                frames.put_nowait(data)
            except Exception:
                return

        def on_frame(params: dict[str, Any]) -> None:
            asyncio.create_task(handle_frame(params))

        cdp.on("Page.screencastFrame", on_frame)
        await cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": 90,
                "maxWidth": 1100,
                "maxHeight": 850,
                "everyNthFrame": 1,
            },
        )
        session.stream_mode = "cdp"
        await self.event_hub.publish(
            "status",
            {"type": "stream_mode", "run_id": run_id, "mode": "cdp"},
        )
        try:
            while self._sessions.get(run_id) is session and not session.page.is_closed():
                try:
                    frame_data = await asyncio.wait_for(frames.get(), timeout=3.0)
                except TimeoutError as exc:
                    raise RuntimeError("cdp_frame_timeout") from exc
                await self._publish_frame(run_id, session, frame_data)
        finally:
            try:
                await cdp.send("Page.stopScreencast")
            except Exception:
                pass
            try:
                await cdp.detach()
            except Exception:
                pass

    async def _stream_browser_polling(
        self, run_id: str, session: LiveBrowserSession
    ) -> None:
        previous_digest = ""
        previous_url = ""
        consecutive_failures = 0
        session.stream_mode = "screenshot_polling"
        await self.event_hub.publish(
            "status",
            {
                "type": "stream_mode",
                "run_id": run_id,
                "mode": "screenshot_polling",
            },
        )
        while self._sessions.get(run_id) is session and not session.page.is_closed():
            try:
                if session.page.url != previous_url:
                    previous_url = session.page.url
                    await self._fit_page_to_viewport(session.page)
                image = await session.page.screenshot(
                    type="jpeg", quality=90, full_page=False
                )
                digest = hashlib.sha1(image).hexdigest()
                if digest != previous_digest:
                    previous_digest = digest
                    await self._publish_frame(
                        run_id,
                        session,
                        base64.b64encode(image).decode("ascii"),
                    )
                consecutive_failures = 0
            except Exception:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    raise RuntimeError("screenshot_stream_failed")
            await asyncio.sleep(0.5)

    async def _stream_browser(self, run_id: str, session: LiveBrowserSession) -> None:
        try:
            try:
                await self._stream_browser_polling(run_id, session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.event_hub.publish(
                    "status",
                    {
                        "type": "stream_fallback",
                        "run_id": run_id,
                        "reason": type(exc).__name__,
                        "mode": "cdp",
                    },
                )
                await self._stream_browser_cdp(run_id, session)
        except asyncio.CancelledError:
            pass
        finally:
            self._stream_tasks.pop(run_id, None)

    @staticmethod
    async def _fit_page_to_viewport(page: Page) -> float:
        return await page.evaluate(
            """() => {
                const root = document.documentElement;
                const body = document.body;
                root.style.zoom = '1';
                const pageWidth = Math.max(
                    root.scrollWidth,
                    root.offsetWidth,
                    body ? body.scrollWidth : 0,
                    body ? body.offsetWidth : 0
                );
                const viewportWidth = Math.max(window.innerWidth, 760);
                const scale = Math.min(1, viewportWidth / Math.max(pageWidth, viewportWidth));
                root.style.zoom = String(scale);
                return scale;
            }"""
        )

    def _session_for_run(self, run_id: str) -> LiveBrowserSession | None:
        return self._sessions.get(run_id)

    async def activate_handoff(self, run_id: str) -> dict[str, Any]:
        session = self._session_for_run(run_id)
        if session is None or session.page.is_closed():
            raise LookupError("활성 브라우저 화면이 없습니다.")
        final_button = session.page.locator("#btnReq")
        if await final_button.count() != 1 or not await final_button.is_visible():
            raise RuntimeError("접수신청 직전 화면이 아닙니다.")
        session.handoff_active = True
        shot = await session.page.screenshot(type="jpeg", quality=92)
        await self.event_hub.publish(
            "status",
            {
                "type": "manual_handoff_activated",
                "run_id": run_id,
                "url": session.page.url,
            },
            store=False,
        )
        return {
            "status": "ready",
            "run_id": run_id,
            "url": session.page.url,
            "frame_data": base64.b64encode(shot).decode("ascii"),
            "final_button_text": "접수신청",
            "submission_occurred": False,
        }

    async def submit_final(self, run_id: str, *, confirmed: bool) -> dict[str, Any]:
        session = self._session_for_run(run_id)
        if session is None or session.page.is_closed():
            raise LookupError("활성 브라우저 화면이 없습니다.")
        if not session.handoff_active:
            raise PermissionError("먼저 사용자 인계 화면을 열어 주세요.")
        if not confirmed:
            raise PermissionError("실제 접수 의사를 명시적으로 확인해야 합니다.")
        if session.pending_dialog is not None:
            return {
                "status": "confirmation_required",
                "message": session.pending_dialog.message,
            }
        final_button = session.page.locator("#btnReq")
        if await final_button.count() != 1 or not await final_button.is_visible():
            raise RuntimeError("접수신청 버튼을 확인할 수 없습니다.")
        await self.event_hub.publish(
            "status",
            {"type": "manual_submit_requested", "run_id": run_id},
        )
        await final_button.click()
        await session.page.wait_for_timeout(500)
        if session.pending_dialog is not None:
            return {
                "status": "confirmation_required",
                "message": session.pending_dialog.message,
            }
        return {
            "status": "site_processing",
            "message": "우체국 화면의 처리 결과를 확인해 주세요.",
        }

    async def cancel_handoff(self, run_id: str) -> dict[str, Any]:
        session = self._session_for_run(run_id)
        if session is None or session.page.is_closed():
            raise LookupError("활성 브라우저 화면이 없습니다.")
        if session.pending_dialog is not None:
            dialog = session.pending_dialog
            session.pending_dialog = None
            await dialog.dismiss()
        session.handoff_active = False
        await self.event_hub.publish(
            "status",
            {"type": "manual_handoff_cancelled", "run_id": run_id},
        )
        return {"status": "cancelled", "submission_occurred": False}

    async def click_browser(self, run_id: str, x: float, y: float) -> dict[str, Any]:
        session = self._session_for_run(run_id)
        if session is None or session.page.is_closed():
            raise LookupError("활성 브라우저 화면이 없습니다.")
        if not session.handoff_active:
            raise PermissionError("에이전트 실행이 끝난 뒤 직접 조작할 수 있습니다.")
        if session.pending_dialog is not None:
            raise PermissionError("먼저 화면의 확인창에 응답해 주세요.")

        viewport = session.page.viewport_size or {"width": 390, "height": 844}
        await session.page.mouse.click(
            x * viewport["width"],
            y * viewport["height"],
        )
        return {"status": "clicked", "x": x, "y": y}

    async def click_preview(self, run_id: str, x: float, y: float) -> dict[str, Any]:
        session = self._session_for_run(run_id)
        if session is None or session.page.is_closed():
            raise LookupError("활성 브라우저 화면이 없습니다.")
        if session.pending_dialog is not None:
            raise PermissionError("확인창은 새 창에서 직접 처리해 주세요.")

        final_button = session.page.locator("#btnReq")
        if await final_button.count() != 1 or not await final_button.is_visible():
            raise PermissionError("에이전트 실행이 끝난 뒤 화면을 조작할 수 있습니다.")

        viewport = session.page.viewport_size or {"width": 1100, "height": 720}
        click_x = x * viewport["width"]
        click_y = y * viewport["height"]
        hits_final_button = await session.page.evaluate(
            """({x, y}) => {
                const target = document.elementFromPoint(x, y);
                return Boolean(target && (target.id === 'btnReq' || target.closest('#btnReq')));
            }""",
            {"x": click_x, "y": click_y},
        )
        final_box = await final_button.bounding_box()
        if hits_final_button or (final_box and (
            final_box["x"] <= click_x <= final_box["x"] + final_box["width"]
            and final_box["y"] <= click_y <= final_box["y"] + final_box["height"]
        )):
            raise PermissionError("접수신청은 새 창에서 최종 확인 후 진행해 주세요.")

        await session.page.mouse.click(click_x, click_y)
        await session.page.wait_for_timeout(250)
        await self.event_hub.publish(
            "status",
            {"type": "preview_click", "run_id": run_id, "x": x, "y": y},
            store=False,
        )
        return {"status": "clicked", "x": x, "y": y, "submission_occurred": False}

    async def resolve_dialog(self, run_id: str, accept: bool) -> dict[str, Any]:
        session = self._session_for_run(run_id)
        if session is None:
            raise LookupError("활성 브라우저 화면이 없습니다.")
        dialog = session.pending_dialog
        if dialog is None:
            raise LookupError("응답할 확인창이 없습니다.")
        session.pending_dialog = None
        if accept:
            await dialog.accept()
        else:
            await dialog.dismiss()
        await session.page.wait_for_timeout(700)
        await self.event_hub.publish(
            "status",
            {
                "type": "browser_dialog_resolved",
                "run_id": run_id,
                "accepted": accept,
            },
            store=False,
        )
        return {
            "status": "accepted" if accept else "dismissed",
            "message": "우체국 화면에서 최종 처리 결과를 확인해 주세요." if accept else "실제 접수를 취소했습니다.",
        }

    async def _publish_live_step(
        self,
        *,
        run_id: str,
        page: Page,
        step: str,
        label: str,
        progress: int,
        focus_selector: str | None = None,
    ) -> None:
        await self._fit_page_to_viewport(page)
        if focus_selector:
            locator = page.locator(focus_selector)
            if await locator.count() == 1:
                await locator.evaluate(
                    "element => element.scrollIntoView({block: 'center', inline: 'center'})"
                )
        await self.event_hub.publish(
            "status",
            {
                "type": "browser_step",
                "run_id": run_id,
                "service": self.service,
                "step": step,
                "label": label,
                "progress": progress,
                "browser_mode": "live",
                "url": page.url,
            },
        )
        if self.step_delay_ms > 0:
            await page.wait_for_timeout(self.step_delay_ms)

    @staticmethod
    async def _fill_domestic_address(
        page: Page,
        *,
        prefix: str,
        contact: dict[str, str],
    ) -> None:
        if prefix == "sender":
            selectors = {
                "zip": "#tSndZipcd",
                "base": "#tSndAddr1",
                "detail": "#tSndAddr2",
            }
        else:
            selectors = {
                "zip": "#tReceiverZipcode1",
                "base": "#tReceiverAddr1",
                "detail": "#tReceiverAddr2",
            }
        values = {
            "zip": contact.get("postal_code", ""),
            "base": contact.get("address_base", contact.get("address_domestic", "")),
            "detail": contact.get("address_detail", ""),
        }
        for key in ("zip", "base"):
            await page.locator(selectors[key]).evaluate(
                "(element) => element.removeAttribute('readonly')"
            )
        for key, value in values.items():
            await page.locator(selectors[key]).fill(value)

    @staticmethod
    def _phone_parts(phone: str) -> tuple[str, str, str]:
        digits = re.sub(r"\D", "", phone)
        if len(digits) < 10:
            raise ValueError("휴대전화 번호 형식이 올바르지 않습니다.")
        return digits[:3], digits[3:-4], digits[-4:]

    @staticmethod
    async def _fill_phone(page: Page, selectors: tuple[str, str, str], phone: str) -> None:
        for selector, value in zip(selectors, EpostAdapter._phone_parts(phone)):
            await page.locator(selector).evaluate(
                """(element, nextValue) => {
                    element.value = nextValue;
                    element.dispatchEvent(new Event('input', {bubbles: true}));
                    element.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                value,
            )

    async def _stage_domestic_live(
        self,
        *,
        run_id: str,
        sender: dict[str, str],
        recipient: dict[str, str],
        shipment: dict[str, Any],
    ) -> StageResult:
        await self.close_all_sessions()
        session = await self._launch_session()
        self._sessions[run_id] = session
        self._stream_tasks[run_id] = asyncio.create_task(
            self._stream_browser(run_id, session)
        )
        page = session.page
        dialogs: list[str] = []

        async def dismiss_dialog(dialog: Dialog) -> None:
            if session.handoff_active:
                session.pending_dialog = dialog
                await self.event_hub.publish(
                    "status",
                    {
                        "type": "browser_dialog",
                        "run_id": run_id,
                        "message": dialog.message,
                        "dialog_type": dialog.type,
                    },
                    store=False,
                )
                return
            dialogs.append(dialog.message)
            await dialog.dismiss()

        page.on("dialog", dismiss_dialog)
        try:
            await page.goto(AGREEMENT_URL, wait_until="domcontentloaded")
            await self._publish_live_step(
                run_id=run_id,
                page=page,
                step="service_selected",
                label="우체국 비회원 접수 경로 진입",
                progress=10,
                focus_selector="#agree1",
            )

            await self.event_hub.publish(
                "status",
                {
                    "type": "required_terms_summary",
                    "run_id": run_id,
                    "summary": "우체국이 이름·주소·전화번호를 소포 접수와 배송에 사용합니다.",
                    "duration_ms": 5000,
                },
            )
            await page.locator("#agree1").check()
            await page.locator("#agree2").check()
            guest_password = sender.get("guest_password", "1111")
            await page.locator("#guest_orderpw").fill(guest_password)
            await page.locator("#guest_orderpw2").fill(guest_password)
            async with page.expect_navigation(wait_until="domcontentloaded"):
                await page.get_by_role("link", name="다음", exact=True).click()
            await self._publish_live_step(
                run_id=run_id,
                page=page,
                step="agreements",
                label="비회원 약관 및 신청 비밀번호 입력",
                progress=24,
                focus_selector="#parcelUseGuideInfoChkBox",
            )

            await page.locator("#parcelUseGuideInfoChkBox").check()
            await page.locator("#tSndNm").fill(sender["name_ko"])
            await self._fill_domestic_address(page, prefix="sender", contact=sender)
            await self._fill_phone(
                page,
                ("#tSndHTel1", "#tSndHTel2", "#tSndHTel3"),
                sender["phone"],
            )
            sender_next = page.locator("#divSender a.btn_next")
            if await sender_next.count() != 1:
                raise RuntimeError("보내는 분 다음 버튼을 식별하지 못했습니다.")
            await sender_next.click()
            if dialogs:
                phone_values = [
                    await page.locator(selector).input_value()
                    for selector in ("#tSndHTel1", "#tSndHTel2", "#tSndHTel3")
                ]
                raise RuntimeError(
                    "보내는 분 검증 실패: "
                    + dialogs.pop()
                    + f" (입력 길이: {[len(value) for value in phone_values]})"
                )
            await self._publish_live_step(
                run_id=run_id,
                page=page,
                step="sender",
                label="보내는 분 정보 입력",
                progress=43,
                focus_selector="#divPostal",
            )

            if await page.locator("#tMailDivCd2").is_enabled():
                await page.locator("#tMailDivCd2").check()
            await page.locator("#labProductCode").select_option(
                label="농/수/축산물(일반)"
            )
            await page.locator("#tMailCont").fill(str(shipment["contents"]))
            await page.locator("#mailCnt").fill("1")
            postal_next = page.locator("#divPostal a.btn_next")
            if await postal_next.count() != 1:
                raise RuntimeError("우편물 정보 다음 버튼을 식별하지 못했습니다.")
            await postal_next.click()
            if dialogs:
                raise RuntimeError("우편물 정보 검증 실패: " + dialogs.pop())
            await self._publish_live_step(
                run_id=run_id,
                page=page,
                step="shipment",
                label="우편물 정보 입력",
                progress=62,
                focus_selector="#divReceiver",
            )

            await page.locator("#tReceiverName").fill(recipient["name_ko"])
            await self._fill_domestic_address(
                page, prefix="recipient", contact=recipient
            )
            await self._fill_phone(
                page,
                ("#tReceiverHTel1", "#tReceiverHTel2", "#tReceiverHTel3"),
                recipient["phone"],
            )
            receiver_add = page.locator("#divReceiver #btnRecvAddrAdd")
            if await receiver_add.count() != 1:
                raise RuntimeError("받는 분 목록 추가 버튼을 식별하지 못했습니다.")
            await receiver_add.click()
            receiver_dialogs = dialogs.copy()
            dialogs.clear()
            receiver_errors = [
                message
                for message in receiver_dialogs
                if "추가되었습니다" not in message
                and "보내는 분 주소를 수정할 수 없습니다" not in message
            ]
            if receiver_errors:
                raise RuntimeError("받는 분 검증 실패: " + receiver_errors[-1])
            await self._publish_live_step(
                run_id=run_id,
                page=page,
                step="recipient",
                label="받는 분 정보 입력 및 목록 추가",
                progress=82,
                focus_selector="#btnReq",
            )

            final_button = page.locator("#btnReq")
            if await final_button.count() != 1 or not await final_button.is_visible():
                raise RuntimeError("최종 접수신청 안전 경계를 확인하지 못했습니다.")
            await self._publish_live_step(
                run_id=run_id,
                page=page,
                step="final_boundary",
                label="접수신청 직전 안전 중단",
                progress=100,
                focus_selector="#btnReq",
            )
            return StageResult(
                status="staged",
                service=self.service,
                current_step="final_submission_boundary",
                evidence={
                    "browser_mode": "live",
                    "required_fields_complete": True,
                    "submission_occurred": False,
                    "run_id": run_id,
                    "url": page.url,
                },
            )
        except Exception:
            await session.close()
            self._sessions.pop(run_id, None)
            raise

    async def verify(self, result: StageResult) -> dict[str, Any]:
        verified = (
            result.status == "staged"
            and result.current_step == "final_submission_boundary"
            and result.final_button_text == "접수신청"
            and result.final_button_clicked is False
        )
        verification = {
            "verified": verified,
            "service": result.service,
            "boundary": result.current_step,
            "button_text": result.final_button_text,
            "submission_occurred": result.final_button_clicked,
            "browser_mode": result.evidence.get("browser_mode"),
        }
        if result.evidence.get("browser_mode") == "live":
            run_id = result.evidence.get("run_id")
            session = self._sessions.get(str(run_id))
            if session is None:
                verification["verified"] = False
                verification["error"] = "live_session_missing"
            else:
                final_button = session.page.locator("#btnReq")
                verification["verified"] = (
                    verification["verified"]
                    and await final_button.count() == 1
                    and await final_button.is_visible()
                    and (await final_button.inner_text()).strip() == "접수신청"
                )
                verification["url"] = session.page.url
                verification["stream_mode"] = session.stream_mode
                verification["manual_handoff"] = False
                verification["browser_kept_open"] = True
                box = await final_button.evaluate(
                    """element => {
                        element.scrollIntoView({block: 'center', inline: 'center'});
                        const rect = element.getBoundingClientRect();
                        return {
                            x: rect.x, y: rect.y,
                            width: rect.width, height: rect.height,
                            viewportWidth: window.innerWidth,
                            viewportHeight: window.innerHeight
                        };
                    }"""
                )
                if (
                    box
                    and box["x"] >= 0
                    and box["y"] >= 0
                    and box["x"] + box["width"] <= box["viewportWidth"]
                    and box["y"] + box["height"] <= box["viewportHeight"]
                ):
                    edge = {
                        "x": box["x"] / box["viewportWidth"],
                        "y": box["y"] / box["viewportHeight"],
                        "w": box["width"] / box["viewportWidth"],
                        "h": box["height"] / box["viewportHeight"],
                    }
                    await self.event_hub.publish(
                        "status",
                        {"type": "final_edge", "run_id": str(run_id), **edge},
                    )
                await self.event_hub.publish(
                    "status",
                    {
                        "type": "safe_stop",
                        "run_id": str(run_id),
                        "service": self.service,
                        "button_text": "접수신청",
                        "message": "아래 접수신청 버튼만 누르면 실제 접수가 되기에 중단했습니다.",
                    },
                )
        return verification
