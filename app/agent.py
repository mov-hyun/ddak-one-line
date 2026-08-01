from __future__ import annotations

from typing import Any

from agents import Agent, ModelSettings, RunConfig, Runner

from app.budget import CostGuard
from app.config import Settings
from app.events import EventHub
from app.tools import EPOST_TOOLS, clear_run_state, current_run_id


AGENT_INSTRUCTIONS = """
당신은 고령 사용자의 우체국 국내소포·EMS 비회원 사전접수를 돕는 딱한줄 에이전트다.

반드시 다음 순서로만 실행한다.
1. 모든 요청에서 select_service_adapter를 정확히 한 번 호출해 등록된 어댑터와 목표를 대조한다.
2. matched가 false면 다른 도구를 호출하지 않고 현재 지원하지 않는다고 한 문장으로 끝낸다.
3. matched가 true면 사용자 문장에서 수취인 관계와 내용물을 찾는다.
4. resolve_household_contact로 관계를 내부 참조값에 연결한다.
5. 관계가 확인되면 assess_shipment_policy를 한 번 호출해 배송지와 물품의 발송 위험을 먼저 판단한다.
6. policy decision이 blocked이면 다른 도구를 호출하지 않고 plain_summary와 reason을 쉬운 말로 전한다.
7. policy decision이 needs_review이면 다른 도구를 호출하지 않고 plain_summary와 next_action만 간결하게 전한다.
8. policy decision이 clear_to_prepare일 때만 stage_postal_parcel을 한 번 호출한다. 이 도구가 저장된 주소를 보고 국내소포와 EMS를 자동 선택한다.
9. 사용자가 말하지 않은 무게와 크기는 빈 값으로 둔다. 결제수단은 요구하지 않는다.
10. stage 결과가 staged이면 verify_epost_stage를 호출한다.
11. 검증이 성공하면 정확히 다음 문장으로 끝낸다.
   아래 접수신청 버튼만 누르면 실제 접수가 되기에 중단했습니다.

안전 및 개인정보 규칙:
- 이름, 주소, 전화번호를 사용자에게 다시 묻지 않는다. 로컬 Vault가 처리한다.
- 개인정보 원문을 답변이나 도구 인수에 만들지 않는다.
- 접수신청 버튼을 절대 누르지 않는다.
- 도구가 실패하거나 검증이 실패하면 성공했다고 말하지 않는다.
- 미국 등 해외 주소는 EMS로 자동 전환한다. 국내 주소를 다시 요구하지 않는다.
- 우체국 소포와 무관한 요청은 레지스트리 대조 결과를 근거로 현재 지원하지 않는다고 한 문장으로 답한다.
- 한 번에 하나의 도구만 호출하고 같은 도구를 반복 호출하지 않는다.
- 사용자가 "쪽지 받는 분"이라고 하면 resolve_household_contact에 정확히 그 관계명을 전달한다.
- 정책 결과를 법률·통관의 최종 허가라고 표현하지 않는다. clear_to_prepare도 우체국 공식 화면 검증 전의 사전 판단이다.
""".strip()

AGENT_INSTRUCTIONS += """

Flexible EMS customs rules:
- Never hardcode a product name, quantity, or HS code for every request.
- Extract the actual parcel contents and any stated quantity from the user's sentence.
- When calling stage_postal_parcel, always pass customs_description_en as a concise uppercase English customs description.
- Pass hs_code as an exact 10 digit Korea Post HS code candidate appropriate to the described product.
- Pass quantity from the request; use 1 only when the user gives no quantity.
- These customs suggestions are shown in the required-information form so the user can review or edit them before they are entered.
- Do not claim that an HS code candidate is an official customs ruling.
""".strip()


def build_agent(settings: Settings) -> Agent:
    return Agent(
        name="딱한줄 우체국 소포 에이전트",
        instructions=AGENT_INSTRUCTIONS,
        model=settings.openai_model,
        model_settings=ModelSettings(
            tool_choice="auto",
            parallel_tool_calls=False,
            reasoning={"effort": "low"},
        ),
        tools=EPOST_TOOLS,
    )


def _event_payload(data: Any) -> dict[str, Any]:
    if hasattr(data, "model_dump"):
        return data.model_dump(mode="json")
    if isinstance(data, dict):
        return data
    return {"value": str(data)}


def _usage_from_event(payload: dict[str, Any]) -> tuple[int, int]:
    response = payload.get("response") or payload
    usage = response.get("usage") or {}
    return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


def _meter_response(
    response: Any,
    metered_response_ids: set[str],
) -> tuple[int, int]:
    payload = _event_payload(response)
    response_id = payload.get("id")
    if not response_id or response_id in metered_response_ids:
        return 0, 0
    current_input, current_output = _usage_from_event(payload)
    if not (current_input or current_output):
        return 0, 0
    metered_response_ids.add(response_id)
    return current_input, current_output


async def run_goal(
    *,
    run_id: str,
    goal: str,
    agent: Agent,
    event_hub: EventHub,
    cost_guard: CostGuard,
) -> None:
    token = current_run_id.set(run_id)
    input_tokens = 0
    output_tokens = 0
    metered_response_ids: set[str] = set()
    try:
        cost_guard.ensure_available()
        await event_hub.publish(
            "status", {"type": "run_started", "run_id": run_id, "goal": goal}
        )
        result = Runner.run_streamed(
            agent,
            input=goal,
            max_turns=8,
            run_config=RunConfig(
                workflow_name="딱한줄 우편 발송 판단과 실행",
                group_id=run_id,
                trace_include_sensitive_data=False,
            ),
        )
        async for event in result.stream_events():
            if event.type == "raw_response_event":
                payload = _event_payload(event.data)
                await event_hub.publish(
                    "raw",
                    {"type": "raw_api_event", "run_id": run_id, "event": payload},
                )
                current_input, current_output = _meter_response(
                    payload.get("response", {}), metered_response_ids
                )
                input_tokens += current_input
                output_tokens += current_output
            elif event.type == "run_item_stream_event":
                await event_hub.publish(
                    "raw",
                    {
                        "type": "agent_sdk_event",
                        "run_id": run_id,
                        "event": _event_payload(event),
                    },
                )
                await event_hub.publish(
                    "status",
                    {"type": "agent_item", "run_id": run_id, "name": event.name},
                )

        # SDK versions differ in how response.completed usage is exposed in the
        # stream. raw_responses is the authoritative fallback and response ids
        # prevent double counting events already metered above.
        for response in getattr(result, "raw_responses", []):
            current_input, current_output = _meter_response(
                response, metered_response_ids
            )
            input_tokens += current_input
            output_tokens += current_output

        estimated_cost = cost_guard.record(run_id, input_tokens, output_tokens)
        await event_hub.publish(
            "status",
            {
                "type": "run_completed",
                "run_id": run_id,
                "output": str(result.final_output),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost_usd": str(estimated_cost),
            },
        )
    except Exception as exc:
        estimated_cost = cost_guard.record(run_id, input_tokens, output_tokens)
        await event_hub.publish(
            "status",
            {
                "type": "run_failed",
                "run_id": run_id,
                "error": type(exc).__name__,
                "message": str(exc),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost_usd": str(estimated_cost),
            },
        )
    finally:
        clear_run_state(run_id)
        current_run_id.reset(token)
