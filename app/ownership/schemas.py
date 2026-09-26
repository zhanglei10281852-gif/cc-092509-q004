"""权属协作流程的接口模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

SubjectType = Literal["dossier", "patent_family", "external_disclosure"]
ChangeKind = Literal["initial", "transfer", "supplement", "withdrawal"]


class OwnershipUnitCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=120)


class OwnershipShare(BaseModel):
    inventor_name: str = Field(min_length=1, max_length=100)
    inventor_user_id: int | None = Field(default=None, gt=0)
    unit_code: str = Field(min_length=2, max_length=64)
    share: float = Field(gt=0, le=1)
    effective_at: str = Field(min_length=10, max_length=40)
    expires_at: str | None = Field(default=None, max_length=40)


class AgreementSubmit(BaseModel):
    agreement_code: str | None = Field(default=None, min_length=3, max_length=64)
    change_kind: ChangeKind = "transfer"
    supersedes_agreement_id: int | None = Field(default=None, gt=0)
    idempotency_key: str = Field(min_length=4, max_length=100)
    effective_at: str = Field(min_length=10, max_length=40)
    shares: list[OwnershipShare] = Field(min_length=1, max_length=50)
    signer_user_ids: list[int] = Field(min_length=1, max_length=50)
    note: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _check_signers(self) -> "AgreementSubmit":
        if len(set(self.signer_user_ids)) != len(self.signer_user_ids):
            raise ValueError("签署人不能重复")
        inventors = {share.inventor_name for share in self.shares}
        if len(inventors) != len(self.shares):
            raise ValueError("发明人不能重复")
        return self


class AgreementReview(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=500)


class FamilyCreate(BaseModel):
    family_code: str = Field(min_length=3, max_length=64)
    root_dossier_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=200)
