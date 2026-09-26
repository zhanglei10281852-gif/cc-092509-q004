from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import current_principal
from app.database import get_connection, transaction
from app.core.security import Principal
from app.archives.ownership import OwnershipService
from app.archives.ownership_schemas import (
    InventorCreate,
    OwnershipAgreementSubmit,
    OwnershipDecision,
    OwnershipReferenceCreate,
    OwnershipSignerCreate,
    OwnershipUnitCreate,
    OwnershipWithdraw,
)

router = APIRouter(prefix="/api/ownership", tags=["权属协作"])


@router.post("/units", status_code=status.HTTP_201_CREATED)
def create_unit(payload: OwnershipUnitCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return OwnershipService(connection).create_unit(principal, payload.model_dump())


@router.get("/units")
def list_units(principal: Principal = Depends(current_principal)):
    return OwnershipService(get_connection()).list_units(principal)


@router.post("/units/{unit_id}/signers", status_code=status.HTTP_201_CREATED)
def create_signer(unit_id: int, payload: OwnershipSignerCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return OwnershipService(connection).create_signer(principal, unit_id, payload.model_dump())


@router.get("/units/{unit_id}/signers")
def list_signers(unit_id: int, principal: Principal = Depends(current_principal)):
    return OwnershipService(get_connection()).list_signers(principal, unit_id)


@router.post("/inventors", status_code=status.HTTP_201_CREATED)
def create_inventor(payload: InventorCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return OwnershipService(connection).create_inventor(principal, payload.model_dump())


@router.get("/inventors")
def list_inventors(principal: Principal = Depends(current_principal)):
    return OwnershipService(get_connection()).list_inventors(principal)


@router.post("/dossiers/{dossier_id}/agreements", status_code=status.HTTP_201_CREATED)
def submit_agreement(dossier_id: int, payload: OwnershipAgreementSubmit, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return OwnershipService(connection).submit_agreement(principal, dossier_id, payload.model_dump())


@router.get("/dossiers/{dossier_id}/agreements")
def list_agreements(dossier_id: int, principal: Principal = Depends(current_principal)):
    return OwnershipService(get_connection()).list_agreements(principal, dossier_id)


@router.get("/dossiers/{dossier_id}/ownership")
def current_ownership(dossier_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return OwnershipService(connection).snapshot_current(principal, dossier_id)


@router.get("/dossiers/{dossier_id}/ownership/as-of")
def ownership_as_of(dossier_id: int, at: str = Query(min_length=10, max_length=40), principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return OwnershipService(connection).snapshot_as_of(principal, dossier_id, at)


@router.get("/dossiers/{dossier_id}/ownership/events")
def ownership_events(dossier_id: int, principal: Principal = Depends(current_principal)):
    return OwnershipService(get_connection()).list_events(principal, dossier_id)


@router.post("/dossiers/{dossier_id}/references", status_code=status.HTTP_201_CREATED)
def pin_reference(dossier_id: int, payload: OwnershipReferenceCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return OwnershipService(connection).pin_reference(principal, dossier_id, payload.model_dump())


@router.get("/dossiers/{dossier_id}/references")
def list_references(dossier_id: int, principal: Principal = Depends(current_principal)):
    return OwnershipService(get_connection()).list_references(principal, dossier_id)


@router.get("/agreements/{agreement_id}")
def agreement_detail(agreement_id: int, principal: Principal = Depends(current_principal)):
    return OwnershipService(get_connection()).agreement_detail(principal, agreement_id)


@router.post("/agreements/{agreement_id}/decisions")
def decide_agreement(agreement_id: int, payload: OwnershipDecision, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return OwnershipService(connection).decide(principal, agreement_id, payload.model_dump())


@router.post("/agreements/{agreement_id}/activate")
def activate_agreement(agreement_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return OwnershipService(connection).activate(principal, agreement_id)


@router.post("/agreements/{agreement_id}/withdraw", status_code=status.HTTP_201_CREATED)
def withdraw_agreement(agreement_id: int, payload: OwnershipWithdraw, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return OwnershipService(connection).withdraw(principal, agreement_id, payload.model_dump())
