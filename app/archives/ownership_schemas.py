from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class OwnershipUnitCreate(BaseModel):
    unit_code: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=100)


class OwnershipSignerCreate(BaseModel):
    user_id: int = Field(gt=0)
    title: str = Field(default="", max_length=100)
    valid_from: str = Field(min_length=10, max_length=40)
    valid_until: str | None = Field(default=None, min_length=10, max_length=40)


class InventorCreate(BaseModel):
    inventor_code: str = Field(min_length=2, max_length=64)
    display_name: str = Field(min_length=1, max_length=100)
    home_unit_id: int | None = Field(default=None, gt=0)


class OwnershipEntryInput(BaseModel):
    inventor_id: int = Field(gt=0)
    unit_id: int = Field(gt=0)
    share_percent: float = Field(gt=0, le=100)
    valid_from: str = Field(min_length=10, max_length=40)
    valid_until: str = Field(min_length=10, max_length=40)


class OwnershipSignatureInput(BaseModel):
    user_id: int = Field(gt=0)
    role: Literal["inventor", "unit_representative"]
    signed_at: str = Field(min_length=10, max_length=40)
    inventor_id: int | None = Field(default=None, gt=0)
    unit_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def ensure_target(self):
        if self.role == "inventor" and not self.inventor_id:
            raise ValueError("发明人签署必须提供 inventor_id")
        if self.role == "unit_representative" and not self.unit_id:
            raise ValueError("单位代表签署必须提供 unit_id")
        return self


class OwnershipAgreementSubmit(BaseModel):
    agreement_code: str | None = Field(default=None, min_length=3, max_length=64)
    kind: Literal["baseline", "assignment", "supplement"]
    parent_agreement_id: int | None = Field(default=None, gt=0)
    idempotency_key: str | None = Field(default=None, min_length=4, max_length=100)
    effective_from: str = Field(min_length=10, max_length=40)
    effective_until: str | None = Field(default=None, min_length=10, max_length=40)
    reason: str = Field(min_length=2, max_length=500)
    note: str = Field(default="", max_length=500)
    entries: list[OwnershipEntryInput] = Field(min_length=1, max_length=50)
    signatures: list[OwnershipSignatureInput] = Field(min_length=1, max_length=100)


class OwnershipWithdraw(BaseModel):
    agreement_code: str | None = Field(default=None, min_length=3, max_length=64)
    reason: str = Field(min_length=2, max_length=500)
    idempotency_key: str | None = Field(default=None, min_length=4, max_length=100)


class OwnershipDecision(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=500)


class OwnershipReferenceCreate(BaseModel):
    ref_type: Literal["disclosure_version", "patent_family", "external_disclosure"]
    ref_code: str = Field(min_length=2, max_length=100)
    agreement_id: int | None = Field(default=None, gt=0)
    as_of: str | None = Field(default=None, min_length=10, max_length=40)
    note: str = Field(default="", max_length=500)
