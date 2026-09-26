"""权属协作流程接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import current_principal
from app.core.errors import ValidationError
from app.core.security import Principal
from app.database import get_connection, transaction
from app.ownership.schemas import AgreementReview, AgreementSubmit, FamilyCreate, OwnershipUnitCreate
from app.ownership.service import OwnershipService

router = APIRouter(prefix="/api/ownership", tags=["权属协作"])

_SUBJECT_TYPES = {"dossier", "patent_family"}
_REF_KINDS = {"disclosure_version", "patent_family", "external_disclosure"}


@router.post("/units", status_code=status.HTTP_201_CREATED)
def create_unit(payload: OwnershipUnitCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return OwnershipService(connection).create_unit(principal, payload.model_dump())


@router.get("/units")
def list_units(principal: Principal = Depends(current_principal)):
    return OwnershipService(get_connection()).list_units(principal)


@router.post("/{subject_type}/{subject_id}/agreements", status_code=status.HTTP_201_CREATED)
def submit_agreement(
    subject_type: str,
    subject_id: int,
    payload: AgreementSubmit,
    principal: Principal = Depends(current_principal),
):
    if subject_type not in _SUBJECT_TYPES:
        raise ValidationError("subject_type 必须是 dossier 或 patent_family")
    with transaction(immediate=True) as connection:
        return OwnershipService(connection).submit_agreement(
            principal, subject_type, subject_id, payload.model_dump()
        )


@router.get("/{subject_type}/{subject_id}/agreements")
def list_agreements(subject_type: str, subject_id: int, principal: Principal = Depends(current_principal)):
    if subject_type not in _SUBJECT_TYPES:
        raise ValidationError("subject_type 必须是 dossier 或 patent_family")
    return OwnershipService(get_connection()).list_agreements(principal, subject_type, subject_id)


@router.post("/agreements/{agreement_id}/reviews")
def review_agreement(
    agreement_id: int,
    payload: AgreementReview,
    principal: Principal = Depends(current_principal),
):
    with transaction(immediate=True) as connection:
        return OwnershipService(connection).review_agreement(
            principal, agreement_id, payload.model_dump()
        )


@router.get("/{subject_type}/{subject_id}/current")
def current_ownership(
    subject_type: str,
    subject_id: int,
    at: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    if subject_type not in _SUBJECT_TYPES:
        raise ValidationError("subject_type 必须是 dossier 或 patent_family")
    return OwnershipService(get_connection()).current_ownership(principal, subject_type, subject_id, at)


@router.get("/{subject_type}/{subject_id}/events")
def list_events(subject_type: str, subject_id: int, principal: Principal = Depends(current_principal)):
    if subject_type not in _SUBJECT_TYPES:
        raise ValidationError("subject_type 必须是 dossier 或 patent_family")
    return OwnershipService(get_connection()).list_events(principal, subject_type, subject_id)


@router.get("/{subject_type}/{subject_id}/references")
def list_references(subject_type: str, subject_id: int, principal: Principal = Depends(current_principal)):
    if subject_type not in _SUBJECT_TYPES:
        raise ValidationError("subject_type 必须是 dossier 或 patent_family")
    return OwnershipService(get_connection()).list_references(principal, subject_type, subject_id)


@router.get("/references/{ref_kind}/{ref_id}")
def resolve_reference(ref_kind: str, ref_id: int, principal: Principal = Depends(current_principal)):
    if ref_kind not in _REF_KINDS:
        raise ValidationError("ref_kind 必须是 disclosure_version、patent_family 或 external_disclosure")
    return OwnershipService(get_connection()).resolve_reference(principal, ref_kind, ref_id)


@router.post("/families", status_code=status.HTTP_201_CREATED)
def create_family(payload: FamilyCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return OwnershipService(connection).create_family(principal, payload.model_dump())


@router.get("/families")
def list_families(principal: Principal = Depends(current_principal)):
    return OwnershipService(get_connection()).list_families(principal)
