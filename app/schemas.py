from __future__ import annotations

from pydantic import BaseModel, Field


class RunRequest(BaseModel):
    goal: str = Field(min_length=2, max_length=300)


class RunAccepted(BaseModel):
    run_id: str
    status: str = "accepted"


class ManualSubmitRequest(BaseModel):
    confirmed: bool = False


class DialogDecisionRequest(BaseModel):
    accept: bool = False


class BrowserClickRequest(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class EmsCustomsRequest(BaseModel):
    weight_kg: float = Field(gt=0, le=30)
    customs_value_usd: float = Field(gt=0, le=10_000)
    recipient_email: str = Field(min_length=5, max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    customs_description_en: str = Field(min_length=2, max_length=50)
    hs_code: str = Field(pattern=r"^\d{10}$")
    quantity: int = Field(ge=1, le=999)
