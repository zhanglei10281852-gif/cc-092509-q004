"""权属协作服务：份额登记、协议变更、双人复核、时效快照与链式事件。"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal, request_fingerprint
from app.archives.repository import DossierRepository
from app.ownership.repository import (
    AgreementRepository,
    OwnershipEventRepository,
    OwnershipReferenceRepository,
    OwnershipSnapshotRepository,
    OwnershipUnitRepository,
    PatentFamilyRepository,
)
from app.services.audit import AuditService

SUBJECT_TABLES = {
    "dossier": ("dossiers", "档案"),
    "patent_family": ("patent_families", "专利家族"),
}
TOTAL_TOLERANCE = 1e-6


class OwnershipService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.units = OwnershipUnitRepository(connection)
        self.snapshots = OwnershipSnapshotRepository(connection)
        self.agreements = AgreementRepository(connection)
        self.events = OwnershipEventRepository(connection)
        self.references = OwnershipReferenceRepository(connection)
        self.families = PatentFamilyRepository(connection)
        self.dossiers = DossierRepository(connection)
        self.audit = AuditService(connection, self.clock)

    # ----- 权属单位 -----

    def create_unit(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("ownership.write")
        if self.units.by_code(data["code"]):
            raise ConflictError("权属单位编码已经存在")
        now = to_storage(self.clock.now())
        unit = self.units.create(data, now)
        self.audit.record(principal, "ownership.unit.create", "ownership_unit", str(unit["id"]), after=unit)
        return unit

    def list_units(self, principal: Principal) -> list[dict[str, Any]]:
        principal.require("ownership.read")
        return self.units.list()

    # ----- 权属快照查询 -----

    def current_ownership(
        self, principal: Principal, subject_type: str, subject_id: int, at: str | None = None
    ) -> dict[str, Any]:
        principal.require("ownership.read")
        self._require_subject(subject_type, subject_id)
        at_storage = to_storage(from_storage(at)) if at else to_storage(self.clock.now())
        snapshot = self.snapshots.effective_at(subject_type, subject_id, at_storage)
        if snapshot is None:
            raise NotFoundError("该时刻不存在生效的权属快照")
        result = dict(snapshot)
        result["agreement"] = (
            self._agreement_brief(snapshot["agreement_id"]) if snapshot.get("agreement_id") else None
        )
        result["query_at"] = at_storage or to_storage(self.clock.now())
        return result

    # ----- 协议提交 -----

    def submit_agreement(
        self, principal: Principal, subject_type: str, subject_id: int, data: dict[str, Any]
    ) -> dict[str, Any]:
        principal.require("ownership.write")
        self._require_subject(subject_type, subject_id)
        now_dt = self.clock.now()
        now = to_storage(now_dt)
        effective_storage = to_storage(from_storage(data["effective_at"]))
        shares = self._validated_shares(data["shares"])
        previous = self.snapshots.latest(subject_type, subject_id)
        self._validate_change_kind(data["change_kind"], previous, data.get("supersedes_agreement_id"))
        signer_ids = self._validated_signers(principal, shares, data["signer_user_ids"])
        if data["change_kind"] != "initial" and from_storage(effective_storage) < now_dt:
            raise ValidationError("变更协议生效时间不能早于提交时间，历史权属不得被新协议改写")

        fingerprint = request_fingerprint(
            {
                "change_kind": data["change_kind"],
                "supersedes": data.get("supersedes_agreement_id"),
                "effective_at": effective_storage,
                "shares": shares,
            }
        )
        existing = self.agreements.by_idempotency(subject_type, subject_id, data["idempotency_key"])
        if existing is not None:
            if existing["request_fingerprint"] != fingerprint:
                raise ConflictError("同一幂等键已用于内容不同的权属协议")
            return {**self.agreements.get(existing["id"]), "replayed": True}

        cross_unit = self._is_cross_unit(data["change_kind"], shares, previous)
        agreement_code = data.get("agreement_code") or f"OWN-{uuid.uuid4().hex[:12]}"
        agreement = self.agreements.insert(
            subject_type=subject_type,
            subject_id=subject_id,
            change_kind=data["change_kind"],
            supersedes_id=data.get("supersedes_agreement_id"),
            agreement_code=agreement_code,
            idempotency_key=data["idempotency_key"],
            fingerprint=fingerprint,
            cross_unit=cross_unit,
            required_approvals=2,
            requested_by=principal.user_id,
            effective_at=effective_storage,
            note=data.get("note", ""),
            shares=shares,
            signer_user_ids=signer_ids,
            now=now,
        )
        if cross_unit:
            self.events.append(
                subject_type, subject_id, "agreement.submitted", principal.user_id, now,
                agreement_id=agreement["id"],
                details={"change_kind": data["change_kind"], "cross_unit": True, "shares": shares},
            )
        else:
            snapshot = self._make_effective(agreement, shares, effective_storage, now)
            agreement = self.agreements.get(agreement["id"])
            agreement["snapshot"] = snapshot
        self.audit.record(
            principal,
            "ownership.agreement.submit",
            "ownership_agreement",
            str(agreement["id"]),
            after={"state": agreement["state"], "cross_unit": cross_unit, "code": agreement_code},
        )
        return {**agreement, "replayed": False}

    def review_agreement(self, principal: Principal, agreement_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("ownership.review")
        agreement = self.agreements.get(agreement_id)
        if agreement["state"] != "pending_review":
            raise ConflictError("协议不在待复核状态")
        if agreement["requested_by"] == principal.user_id:
            raise ValidationError("申请人不能复核自己提交的跨单位转让协议")
        if not self._active_user(principal.user_id):
            raise ValidationError("复核人账户不可用")
        for review in agreement["reviews"]:
            if review["reviewer_user_id"] == principal.user_id:
                raise ConflictError("该协议已经由当前复核人复核")
        now = to_storage(self.clock.now())
        self.agreements.add_review(agreement_id, principal.user_id, data["decision"], data.get("comment", ""), now)
        if data["decision"] == "reject":
            self.agreements.set_state(agreement_id, "rejected", now)
            self.events.append(
                agreement["subject_type"], agreement["subject_id"], "agreement.rejected",
                principal.user_id, now, agreement_id=agreement_id,
                details={"comment": data.get("comment", "")},
            )
        else:
            approvals = self.agreements.approval_count(agreement_id)
            if approvals >= agreement["required_approvals"]:
                shares = agreement["payload"]["shares"]
                # 双人复核通过的时刻即为实际生效时刻：生效前的时间旅行查询返回旧
                # 快照，生效后返回新快照，历史权属不会被提前改写。
                effective_storage = to_storage(self.clock.now())
                snapshot = self._make_effective(agreement, shares, effective_storage, now)
                result = self.agreements.get(agreement_id)
                result["snapshot"] = snapshot
            else:
                self.events.append(
                    agreement["subject_type"], agreement["subject_id"], "agreement.reviewed",
                    principal.user_id, now, agreement_id=agreement_id,
                    details={"decision": "approve", "approvals": approvals},
                )
                result = self.agreements.get(agreement_id)
        self.audit.record(
            principal,
            "ownership.agreement.review",
            "ownership_agreement",
            str(agreement_id),
            after={"state": self.agreements.get(agreement_id)["state"], "decision": data["decision"]},
        )
        return self.agreements.get(agreement_id) if data["decision"] == "reject" else result

    # ----- 协议与事件查询 -----

    def list_agreements(self, principal: Principal, subject_type: str, subject_id: int) -> list[dict[str, Any]]:
        principal.require("ownership.read")
        self._require_subject(subject_type, subject_id)
        return self.agreements.list_for_subject(subject_type, subject_id)

    def list_events(self, principal: Principal, subject_type: str, subject_id: int) -> dict[str, Any]:
        principal.require("ownership.read")
        self._require_subject(subject_type, subject_id)
        return {
            "events": self.events.list_for_subject(subject_type, subject_id),
            "chain_valid": self.events.verify_chain(subject_type, subject_id),
        }

    def list_references(self, principal: Principal, subject_type: str, subject_id: int) -> list[dict[str, Any]]:
        principal.require("ownership.read")
        self._require_subject(subject_type, subject_id)
        return self.references.list_for_subject(subject_type, subject_id)

    def resolve_reference(
        self, principal: Principal, ref_kind: str, ref_id: int
    ) -> dict[str, Any]:
        principal.require("ownership.read")
        reference = self.references.for_ref(ref_kind, ref_id)
        if reference is None:
            raise NotFoundError("该对象未引用任何权属快照")
        snapshot = self.snapshots.get(reference["snapshot_id"])
        return {"reference": reference, "snapshot": snapshot}

    # ----- 专利家族 -----

    def create_family(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("ownership.write")
        self.dossiers.get(data["root_dossier_id"])
        if self.families.by_code(data["family_code"]):
            raise ConflictError("专利家族编码已经存在")
        now = to_storage(self.clock.now())
        snapshot = self.snapshots.current("dossier", data["root_dossier_id"], now)
        if snapshot is None:
            raise ConflictError("根交底书尚无生效权属，无法建立专利家族")
        family = self.families.create(
            data["family_code"], data["root_dossier_id"], data["title"],
            principal.user_id, snapshot["id"], now,
        )
        self.references.pin(
            ref_kind="patent_family",
            ref_id=family["id"],
            ref_code=family["family_code"],
            subject_type="dossier",
            subject_id=data["root_dossier_id"],
            snapshot_id=snapshot["id"],
            now=now,
        )
        self.audit.record(principal, "ownership.family.create", "patent_family", str(family["id"]), after=family)
        return {**family, "ownership_snapshot": snapshot}

    def list_families(self, principal: Principal) -> list[dict[str, Any]]:
        principal.require("ownership.read")
        return self.families.list()

    # ----- 下游引用：交底版本 / 对外披露 -----

    def pin_reference(
        self, ref_kind: str, ref_id: int, ref_code: str, dossier_id: int, now: str
    ) -> dict[str, Any] | None:
        snapshot = self.snapshots.current("dossier", dossier_id, now)
        if snapshot is None:
            return None
        return self.references.pin(
            ref_kind=ref_kind,
            ref_id=ref_id,
            ref_code=ref_code,
            subject_type="dossier",
            subject_id=dossier_id,
            snapshot_id=snapshot["id"],
            now=now,
        )

    # ----- 内部辅助 -----

    def _make_effective(
        self, agreement: dict[str, Any], shares: list[dict[str, Any]], effective_at: str, now: str
    ) -> dict[str, Any]:
        snapshot = self.snapshots.insert(
            agreement["subject_type"], agreement["subject_id"], shares, effective_at, now, agreement["id"]
        )
        self.agreements.set_state(agreement["id"], "effective", now, effective_at=effective_at)
        self.events.append(
            agreement["subject_type"], agreement["subject_id"], "agreement.effective",
            agreement["requested_by"], now, agreement_id=agreement["id"],
            details={
                "change_kind": agreement["change_kind"],
                "supersedes_agreement_id": agreement["supersedes_agreement_id"],
                "snapshot_id": snapshot["id"],
                "snapshot_version": snapshot["version"],
                "shares": shares,
            },
        )
        if agreement["supersedes_agreement_id"]:
            self.events.append(
                agreement["subject_type"], agreement["subject_id"],
                f"agreement.{agreement['change_kind']}",
                agreement["requested_by"], now, agreement_id=agreement["id"],
                details={"supersedes_agreement_id": agreement["supersedes_agreement_id"]},
            )
        return snapshot

    def _validated_shares(self, raw_shares: list[dict[str, Any]]) -> list[dict[str, Any]]:
        shares: list[dict[str, Any]] = []
        names: set[str] = set()
        total = 0.0
        for raw in raw_shares:
            name = raw["inventor_name"].strip()
            if name in names:
                raise ValidationError(f"发明人重复：{name}")
            names.add(name)
            share = round(float(raw["share"]), 9)
            total = round(total + share, 9)
            effective = to_storage(from_storage(raw["effective_at"]))
            expires = to_storage(from_storage(raw["expires_at"])) if raw.get("expires_at") else None
            if expires and from_storage(expires) <= from_storage(effective):
                raise ValidationError(f"{name} 的份额失效时间必须晚于生效时间")
            unit = self.units.by_code(raw["unit_code"])
            if unit is None or not unit["is_active"]:
                raise ValidationError(f"权属单位不存在或已停用：{raw['unit_code']}")
            shares.append(
                {
                    "inventor_name": name,
                    "inventor_user_id": raw.get("inventor_user_id"),
                    "unit_code": unit["code"],
                    "share": share,
                    "effective_at": effective,
                    "expires_at": expires,
                }
            )
        if abs(total - 1.0) > TOTAL_TOLERANCE:
            raise ValidationError(f"发明人贡献份额总和必须等于 1，当前为 {total}")
        return shares

    def _validated_signers(
        self, principal: Principal, shares: list[dict[str, Any]], signer_user_ids: list[int]
    ) -> list[int]:
        signer_ids = list(dict.fromkeys(signer_user_ids))
        for user_id in signer_ids:
            if not self._active_user(user_id):
                raise ValidationError(f"签署人不是有效系统用户：{user_id}")
        linked = {share["inventor_user_id"] for share in shares if share.get("inventor_user_id")}
        if not linked:
            raise ValidationError("至少一名发明人需要绑定系统账户以校验签署人资格")
        signer_set = set(signer_ids)
        missing = linked - signer_set
        if missing:
            raise ValidationError(f"以下发明人尚未亲自签署协议：{sorted(missing)}")
        extra = signer_set - linked
        if extra:
            raise ValidationError(f"签署人不在协议发明人名单内：{sorted(extra)}")
        return signer_ids

    def _validate_change_kind(
        self, change_kind: str, previous: dict[str, Any] | None, supersedes_id: int | None
    ) -> None:
        if change_kind == "initial":
            if previous is not None:
                raise ConflictError("已存在权属快照，初始登记只能提交一次")
            if supersedes_id is not None:
                raise ValidationError("初始登记不能取代既有协议")
            return
        if previous is None:
            raise ValidationError("必须先完成权属初始登记，才能提交转让、补充或撤回协议")
        if change_kind in {"supplement", "withdrawal"} and supersedes_id is None:
            label = "补充" if change_kind == "supplement" else "撤回"
            raise ValidationError(f"{label}协议必须通过 supersedes_agreement_id 指向被{label}的原协议")
        if supersedes_id is not None:
            target = self.agreements.get(supersedes_id)
            if target["subject_type"] != previous["subject_type"] or target["subject_id"] != previous["subject_id"]:
                raise ValidationError("被取代协议不属于同一权属主体")
            if target["state"] != "effective":
                raise ConflictError("只能对已生效协议进行补充或撤回")

    def _is_cross_unit(
        self, change_kind: str, shares: list[dict[str, Any]], previous: dict[str, Any] | None
    ) -> bool:
        # 初始登记只是确立权属基线，即便涉及多家单位也不属于“跨单位转让”。
        if change_kind == "initial":
            return False
        unit_codes = {share["unit_code"] for share in shares}
        if len(unit_codes) > 1:
            return True
        if previous is None:
            return False
        previous_units = {item["unit_code"] for item in previous["shares"]}
        return previous_units != unit_codes

    def _require_subject(self, subject_type: str, subject_id: int) -> None:
        mapping = SUBJECT_TABLES.get(subject_type)
        if mapping is None:
            raise ValidationError(f"不支持的权属主体类型：{subject_type}")
        table, label = mapping
        row = self.connection.execute(f"SELECT id FROM {table} WHERE id=?", (subject_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"{label}不存在")

    def _active_user(self, user_id: int) -> bool:
        row = self.connection.execute(
            "SELECT status FROM users WHERE id=?", (user_id,)
        ).fetchone()
        return row is not None and row["status"] == "active"

    def _agreement_brief(self, agreement_id: int) -> dict[str, Any]:
        agreement = self.agreements.get(agreement_id)
        return {
            "id": agreement["id"],
            "agreement_code": agreement["agreement_code"],
            "change_kind": agreement["change_kind"],
            "state": agreement["state"],
        }
