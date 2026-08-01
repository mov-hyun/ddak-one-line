from __future__ import annotations

import base64
import binascii

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from app.config import settings


class PostalPartyOcr(BaseModel):
    name_ko: str = Field(description="한글 이름. 없으면 빈 문자열")
    name_en: str = Field(description="영문 이름. 없으면 빈 문자열")
    address: str = Field(description="쪽지에 적힌 전체 주소")
    postal_code: str = Field(description="우편번호 숫자. 없으면 빈 문자열")
    address_base: str = Field(description="도로명/지번 기본 주소")
    address_detail: str = Field(description="동·호수 등 상세 주소. 없으면 빈 문자열")
    phone: str = Field(description="연락처. 원문 구분기호를 유지")
    email: str = Field(description="이메일. 없으면 빈 문자열")
    country_code: str = Field(description="ISO 2자리 국가코드. 판단 불가하면 빈 문자열")


class AddressNoteOcr(BaseModel):
    sender: PostalPartyOcr
    recipient: PostalPartyOcr
    contents: str = Field(description="소포 내용물. 쪽지에 없으면 빈 문자열")
    confidence: str = Field(description="high, medium, low 중 하나")
    warnings: list[str] = Field(description="읽기 어렵거나 누락된 필드의 짧은 한국어 설명")


def _image_data_url(image_base64: str, mime_type: str) -> str:
    encoded = image_base64.split(",", 1)[-1].strip()
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("이미지 데이터가 올바르지 않습니다.") from exc
    if not raw or len(raw) > 8 * 1024 * 1024:
        raise ValueError("이미지는 8MB 이하만 사용할 수 있습니다.")
    if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("JPG, PNG, WEBP 이미지만 사용할 수 있습니다.")
    return f"data:{mime_type};base64,{encoded}"


async def extract_address_note(image_base64: str, mime_type: str) -> AddressNoteOcr:
    client = AsyncOpenAI()
    response = await client.responses.parse(
        model=settings.openai_model,
        instructions=(
            "당신은 한국 우편 접수용 종이 쪽지 판독기다. 사진에서 '보내는 분'과 "
            "'받는 분'을 구분해 이름, 주소, 우편번호, 상세주소, 전화번호를 그대로 추출한다. "
            "문맥으로 개인정보를 만들어내지 말고 읽히지 않는 값은 빈 문자열로 둔다. "
            "해외 주소면 country_code와 영문 이름을 가능한 범위에서 추출한다. "
            "불확실하거나 누락된 필드는 warnings에 적는다."
        ),
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "이 주소 쪽지에서 보내는 분과 받는 분 정보를 분리해 주세요.",
                    },
                    {
                        "type": "input_image",
                        "image_url": _image_data_url(image_base64, mime_type),
                        "detail": "high",
                    },
                ],
            }
        ],
        text_format=AddressNoteOcr,
        reasoning={"effort": "low"},
        max_output_tokens=900,
        store=False,
    )
    if response.output_parsed is None:
        raise ValueError("쪽지에서 주소 정보를 읽지 못했습니다.")
    return response.output_parsed
