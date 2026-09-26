"""发明人权属协作流程。

法务准备专利申请时，发明人名单、贡献比例和单位归属会在签署转让协议前后
发生变化。该模块以"协议 + 快照 + 链式事件"记录权属演变：

- 每份协议登记多名发明人的贡献份额、归属单位与有效期，提交时校验份额总和
  必须等于 100%，并核验发明人签署与单位授权代表的签署资格；
- 涉及跨单位转让的协议进入双人复核，复核通过后才允许生效；
- 协议生效时生成不可变的权属快照，生效前后的查询分别得到当时的权属状态；
- 同一幂等键重复提交不会产生两次转让，撤回与补充协议都会留下链式事件；
- 后续交底版本、专利家族与对外披露通过引用记录钉住当时有效的权属快照。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from datetime import timedelta
from typing import Any

from app.archives.repository import ApprovalRepository, DossierRepository
from app.archives.validation import parse_timestamp, require_code
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.services.audit import AuditService

SHARE_TOTAL = 100.0
SHARE_TOLERANCE = 1e-6
GENESIS_CHAIN = "GENESIS"
REVIEWED_KINDS = ("assignment", "supplement")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class OwnershipRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    # 单位、发明人与授权签署人
    def create_unit(self, data: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO ownership_units(unit_code,name,created_at,updated_at) VALUES(?,?,?,?)",
            (data["unit_code"], data["name"], now, now),
        )
        return self.require_unit(cursor.lastrowid)

    def unit(self, unit_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM ownership_units WHERE id=?", (unit_id,)).fetchone()
        return dict(row) if row else None

    def require_unit(self, unit_id: int) -> dict[str, Any]:
        unit = self.unit(unit_id)
        if unit is None:
            raise NotFoundError("权属单位不存在")
        return unit

    def unit_by_code(self, unit_code: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM ownership_units WHERE unit_code=?", (unit_code,)).fetchone()
        return dict(row) if row else None

    def list_units(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM ownership_units ORDER BY unit_code").fetchall()
        return [dict(row) for row in rows]

    def create_inventor(self, data: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO inventors(inventor_code,display_name,home_unit_id,created_at,updated_at) VALUES(?,?,?,?,?)",
            (data["inventor_code"], data["display_name"], data.get("home_unit_id"), now, now),
        )
        return self.require_inventor(cursor.lastrowid)

    def inventor(self, inventor_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM inventors WHERE id=?", (inventor_id,)).fetchone()
        return dict(row) if row else None

    def require_inventor(self, inventor_id: int) -> dict[str, Any]:
        inventor = self.inventor(inventor_id)
        if inventor is None:
            raise NotFoundError("发明人不存在")
        return inventor

    def inventor_by_code(self, inventor_code: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM inventors WHERE inventor_code=?", (inventor_code,)).fetchone()
        return dict(row) if row else None

    def list_inventors(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM inventors ORDER BY inventor_code").fetchall()
        return [dict(row) for row in rows]

    def create_signer(self, unit_id: int, data: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO ownership_unit_signers(unit_id,user_id,title,valid_from,valid_until,created_at)
               VALUES(?,?,?,?,?,?)""",
            (unit_id, data["user_id"], data.get("title", ""), data["valid_from"], data.get("valid_until"), now),
        )
        return dict(
            self.connection.execute("SELECT * FROM ownership_unit_signers WHERE id=?", (cursor.lastrowid,)).fetchone()
        )

    def signer_record(self, unit_id: int, user_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM ownership_unit_signers WHERE unit_id=? AND user_id=?",
            (unit_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def list_signers(self, unit_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT s.*,u.username,u.display_name
               FROM ownership_unit_signers s JOIN users u ON u.id=s.user_id
               WHERE s.unit_id=? ORDER BY s.id""",
            (unit_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def signer_authorized(self, unit_id: int, user_id: int, at: str) -> bool:
        row = self.connection.execute(
            """SELECT 1 FROM ownership_unit_signers
               WHERE unit_id=? AND user_id=? AND valid_from<=? AND (valid_until IS NULL OR valid_until>?) LIMIT 1""",
            (unit_id, user_id, at, at),
        ).fetchone()
        return row is not None

    # 权属协议
    def insert_agreement(self, values: dict[str, Any], submitted_by: int, now: str) -> int:
        cursor = self.connection.execute(
            """INSERT INTO ownership_agreements(
                   agreement_code,dossier_id,kind,parent_agreement_id,idempotency_key,payload_digest,
                   state,effective_from,effective_until,approval_request_id,reason,note,
                   submitted_by,submitted_at,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                values["agreement_code"], values["dossier_id"], values["kind"], values.get("parent_agreement_id"),
                values.get("idempotency_key"), values["payload_digest"], values["state"], values["effective_from"],
                values.get("effective_until"), values.get("approval_request_id"), values.get("reason", ""),
                values.get("note", ""), submitted_by, now, now, now,
            ),
        )
        return int(cursor.lastrowid)

    def agreement(self, agreement_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM ownership_agreements WHERE id=?", (agreement_id,)).fetchone()
        if row is None:
            raise NotFoundError("权属协议不存在")
        return dict(row)

    def agreement_by_code(self, agreement_code: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM ownership_agreements WHERE agreement_code=?", (agreement_code,)
        ).fetchone()
        return dict(row) if row else None

    def agreement_by_key(self, dossier_id: int, idempotency_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM ownership_agreements WHERE dossier_id=? AND idempotency_key=?",
            (dossier_id, idempotency_key),
        ).fetchone()
        return dict(row) if row else None

    def open_agreement(self, dossier_id: int, now: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT * FROM ownership_agreements
               WHERE dossier_id=? AND state IN ('pending_review','approved')
                 AND (effective_until IS NULL OR effective_until>?)
               ORDER BY id DESC LIMIT 1""",
            (dossier_id, now),
        ).fetchone()
        return dict(row) if row else None

    def has_live_agreement(self, dossier_id: int, now: str) -> bool:
        row = self.connection.execute(
            """SELECT 1 FROM ownership_agreements
               WHERE dossier_id=? AND state IN ('pending_review','approved','effective')
                 AND (effective_until IS NULL OR effective_until>?) LIMIT 1""",
            (dossier_id, now),
        ).fetchone()
        return row is not None

    def pending_withdrawal(self, target_agreement_id: int) -> bool:
        row = self.connection.execute(
            """SELECT 1 FROM ownership_agreements
               WHERE parent_agreement_id=? AND kind='withdrawal' AND state IN ('pending_review','approved') LIMIT 1""",
            (target_agreement_id,),
        ).fetchone()
        return row is not None

    def list_agreements(self, dossier_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM ownership_agreements WHERE dossier_id=? ORDER BY id",
            (dossier_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def attach_request(self, agreement_id: int, request_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE ownership_agreements SET approval_request_id=?,updated_at=? WHERE id=?",
            (request_id, now, agreement_id),
        )

    def due_agreements(self, dossier_id: int, now: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT * FROM ownership_agreements
               WHERE dossier_id=? AND state='approved' AND kind<>'withdrawal' AND effective_from<=?
                 AND (effective_until IS NULL OR effective_until>?)
               ORDER BY effective_from,id""",
            (dossier_id, now, now),
        ).fetchall()
        return [dict(row) for row in rows]

    def active_agreement_at(self, dossier_id: int, at: str) -> dict[str, Any] | None:
        """按链式事件重放 at 时刻处于生效状态的协议。

        生效事件入栈、撤回事件将目标协议移出栈，栈顶即当时有效的权属：撤回会
        回退到上一份仍有效的协议，而已发生的历史时点不受后续协议影响。已到生效
        时间但尚未激活的预约协议作为投影叠加在栈顶。
        """
        rows = self.connection.execute(
            """SELECT e.agreement_id,e.event_type,a.kind
               FROM ownership_events e JOIN ownership_agreements a ON a.id=e.agreement_id
               WHERE e.dossier_id=? AND e.occurred_at<=?
                 AND e.event_type IN ('agreement.effective','agreement.withdrawn')
               ORDER BY e.occurred_at,e.seq""",
            (dossier_id, at),
        ).fetchall()
        stack: list[int] = []
        for row in rows:
            if row["event_type"] == "agreement.effective":
                if row["kind"] != "withdrawal":
                    stack.append(int(row["agreement_id"]))
            else:
                stack = [item for item in stack if item != row["agreement_id"]]
        scheduled = self.connection.execute(
            """SELECT id FROM ownership_agreements
               WHERE dossier_id=? AND state='approved' AND kind<>'withdrawal' AND effective_from<=?
               ORDER BY effective_from,id""",
            (dossier_id, at),
        ).fetchall()
        stack.extend(int(row["id"]) for row in scheduled)
        if not stack:
            return None
        row = self.connection.execute("SELECT * FROM ownership_agreements WHERE id=?", (stack[-1],)).fetchone()
        return dict(row) if row else None

    # 协议条目与签署
    def insert_entry(self, agreement_id: int, entry: dict[str, Any], now: str) -> None:
        self.connection.execute(
            """INSERT INTO ownership_agreement_entries(agreement_id,inventor_id,unit_id,share_percent,valid_from,valid_until,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                agreement_id, entry["inventor_id"], entry["unit_id"], entry["share_percent"],
                entry["valid_from"], entry["valid_until"], now,
            ),
        )

    def entries(self, agreement_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT e.*,i.inventor_code,i.display_name,u.unit_code,u.name AS unit_name
               FROM ownership_agreement_entries e
               JOIN inventors i ON i.id=e.inventor_id
               JOIN ownership_units u ON u.id=e.unit_id
               WHERE e.agreement_id=? ORDER BY e.share_percent DESC, e.inventor_id""",
            (agreement_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def entries_at(self, agreement_id: int, at: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT e.*,i.inventor_code,i.display_name,u.unit_code,u.name AS unit_name
               FROM ownership_agreement_entries e
               JOIN inventors i ON i.id=e.inventor_id
               JOIN ownership_units u ON u.id=e.unit_id
               WHERE e.agreement_id=? AND e.valid_from<=? AND e.valid_until>?
               ORDER BY e.share_percent DESC, e.inventor_id""",
            (agreement_id, at, at),
        ).fetchall()
        return [dict(row) for row in rows]

    def entry_units(self, agreement_id: int) -> set[int]:
        rows = self.connection.execute(
            "SELECT DISTINCT unit_id FROM ownership_agreement_entries WHERE agreement_id=?",
            (agreement_id,),
        ).fetchall()
        return {int(row[0]) for row in rows}

    def insert_signature(self, agreement_id: int, signature: dict[str, Any], now: str) -> None:
        self.connection.execute(
            """INSERT INTO ownership_agreement_signatures(agreement_id,user_id,role,inventor_id,unit_id,signed_at,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                agreement_id, signature["user_id"], signature["role"], signature.get("inventor_id"),
                signature.get("unit_id"), signature["signed_at"], now,
            ),
        )

    def signatures(self, agreement_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT s.*,u.username,u.display_name
               FROM ownership_agreement_signatures s JOIN users u ON u.id=s.user_id
               WHERE s.agreement_id=? ORDER BY s.id""",
            (agreement_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # 权属快照
    def insert_snapshot(self, dossier_id: int, agreement_id: int, seq: int, entry: dict[str, Any], now: str) -> None:
        self.connection.execute(
            """INSERT INTO ownership_snapshots(dossier_id,agreement_id,seq,inventor_id,unit_id,share_percent,valid_from,valid_until,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                dossier_id, agreement_id, seq, entry["inventor_id"], entry["unit_id"],
                entry["share_percent"], entry["valid_from"], entry["valid_until"], now,
            ),
        )

    def has_snapshot(self, agreement_id: int) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM ownership_snapshots WHERE agreement_id=? LIMIT 1", (agreement_id,)
        ).fetchone()
        return row is not None

    def snapshot_rows(self, agreement_id: int, at: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT s.id,s.agreement_id,s.inventor_id,i.inventor_code,i.display_name,
                      s.unit_id,u.unit_code,u.name AS unit_name,s.share_percent,s.valid_from,s.valid_until
               FROM ownership_snapshots s
               JOIN inventors i ON i.id=s.inventor_id
               JOIN ownership_units u ON u.id=s.unit_id
               WHERE s.agreement_id=? AND s.valid_from<=? AND s.valid_until>?
               ORDER BY s.share_percent DESC, s.inventor_id""",
            (agreement_id, at, at),
        ).fetchall()
        return [dict(row) for row in rows]

    def snapshot_rows_all(self, agreement_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT s.id,s.agreement_id,s.inventor_id,i.inventor_code,i.display_name,
                      s.unit_id,u.unit_code,u.name AS unit_name,s.share_percent,s.valid_from,s.valid_until
               FROM ownership_snapshots s
               JOIN inventors i ON i.id=s.inventor_id
               JOIN ownership_units u ON u.id=s.unit_id
               WHERE s.agreement_id=? ORDER BY s.seq""",
            (agreement_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # 链式事件
    def last_event(self, dossier_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT seq,chain_digest FROM ownership_events WHERE dossier_id=? ORDER BY seq DESC LIMIT 1",
            (dossier_id,),
        ).fetchone()
        return dict(row) if row else None

    def insert_event(self, values: dict[str, Any]) -> None:
        self.connection.execute(
            """INSERT INTO ownership_events(dossier_id,agreement_id,seq,event_type,actor_user_id,payload_json,event_digest,chain_digest,occurred_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                values["dossier_id"], values.get("agreement_id"), values["seq"], values["event_type"],
                values.get("actor_user_id"), _canonical(values["payload"]), values["event_digest"],
                values["chain_digest"], values["occurred_at"],
            ),
        )

    def events(self, dossier_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM ownership_events WHERE dossier_id=? ORDER BY seq",
            (dossier_id,),
        ).fetchall()
        return [self._present_event(row) for row in rows]

    def events_for_agreement(self, agreement_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM ownership_events WHERE agreement_id=? ORDER BY seq",
            (agreement_id,),
        ).fetchall()
        return [self._present_event(row) for row in rows]

    @staticmethod
    def _present_event(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item

    # 权属引用
    def insert_reference(self, dossier_id: int, data: dict[str, Any], agreement_id: int, pinned_by: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO ownership_references(dossier_id,ref_type,ref_code,agreement_id,pinned_by,note,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (dossier_id, data["ref_type"], data["ref_code"], agreement_id, pinned_by, data.get("note", ""), now),
        )
        return dict(
            self.connection.execute("SELECT * FROM ownership_references WHERE id=?", (cursor.lastrowid,)).fetchone()
        )

    def reference(self, dossier_id: int, ref_type: str, ref_code: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM ownership_references WHERE dossier_id=? AND ref_type=? AND ref_code=?",
            (dossier_id, ref_type, ref_code),
        ).fetchone()
        return dict(row) if row else None

    def list_references(self, dossier_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT r.*,a.agreement_code,a.kind AS agreement_kind,a.effective_from,a.effective_until
               FROM ownership_references r JOIN ownership_agreements a ON a.id=r.agreement_id
               WHERE r.dossier_id=? ORDER BY r.id""",
            (dossier_id,),
        ).fetchall()
        return [dict(row) for row in rows]


class OwnershipService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repo = OwnershipRepository(connection)
        self.dossiers = DossierRepository(connection)
        self.approvals = ApprovalRepository(connection)
        self.audit = AuditService(connection, self.clock)

    # 登记单位、发明人与授权签署人
    def create_unit(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("ownership.manage")
        unit_code = require_code(data["unit_code"], "单位编码")
        if self.repo.unit_by_code(unit_code):
            raise ConflictError("单位编码已经存在")
        now = to_storage(self.clock.now())
        unit = self.repo.create_unit({"unit_code": unit_code, "name": data["name"].strip()}, now)
        self.audit.record(principal, "ownership.unit.create", "ownership_unit", str(unit["id"]), after=unit)
        return unit

    def list_units(self, principal: Principal) -> list[dict[str, Any]]:
        principal.require("ownership.read")
        return self.repo.list_units()

    def create_inventor(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("ownership.manage")
        inventor_code = require_code(data["inventor_code"], "发明人编码")
        if self.repo.inventor_by_code(inventor_code):
            raise ConflictError("发明人编码已经存在")
        if data.get("home_unit_id") is not None:
            self.repo.require_unit(data["home_unit_id"])
        now = to_storage(self.clock.now())
        inventor = self.repo.create_inventor(
            {"inventor_code": inventor_code, "display_name": data["display_name"].strip(), "home_unit_id": data.get("home_unit_id")},
            now,
        )
        self.audit.record(principal, "ownership.inventor.create", "inventor", str(inventor["id"]), after=inventor)
        return inventor

    def list_inventors(self, principal: Principal) -> list[dict[str, Any]]:
        principal.require("ownership.read")
        return self.repo.list_inventors()

    def create_signer(self, principal: Principal, unit_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("ownership.manage")
        self.repo.require_unit(unit_id)
        self._active_user(data["user_id"])
        valid_from = to_storage(parse_timestamp(data["valid_from"], "授权开始时间"))
        valid_until = None
        if data.get("valid_until"):
            valid_until = to_storage(parse_timestamp(data["valid_until"], "授权失效时间"))
            if valid_until <= valid_from:
                raise ValidationError("授权失效时间必须晚于授权开始时间")
        if self.repo.signer_record(unit_id, data["user_id"]):
            raise ConflictError("该用户已获得此单位的签署授权")
        now = to_storage(self.clock.now())
        signer = self.repo.create_signer(
            unit_id,
            {"user_id": data["user_id"], "title": data.get("title", ""), "valid_from": valid_from, "valid_until": valid_until},
            now,
        )
        self.audit.record(principal, "ownership.signer.create", "ownership_unit", str(unit_id), after=signer)
        return signer

    def list_signers(self, principal: Principal, unit_id: int) -> list[dict[str, Any]]:
        principal.require("ownership.read")
        self.repo.require_unit(unit_id)
        return self.repo.list_signers(unit_id)

    # 协议提交
    def submit_agreement(self, principal: Principal, dossier_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("ownership.manage")
        self.dossiers.get(dossier_id)
        kind = data["kind"]
        now = to_storage(self.clock.now())
        self._activate_due(dossier_id, now)
        effective_from = to_storage(parse_timestamp(data["effective_from"], "生效时间"))
        effective_until = None
        if data.get("effective_until"):
            effective_until = to_storage(parse_timestamp(data["effective_until"], "失效时间"))
            if effective_until <= effective_from:
                raise ValidationError("协议失效时间必须晚于生效时间")
        entries = self._validate_entries(data["entries"])
        signatures = self._validate_signatures(data["signatures"], entries, now)
        parent = self._resolve_parent(dossier_id, kind, data.get("parent_agreement_id"), now)
        if parent is not None and effective_from < parent["effective_from"]:
            raise ValidationError("变更协议的生效时间不能早于当前权属协议的生效时间")
        digest = self._payload_digest(kind, effective_from, effective_until, data["reason"], entries, signatures)
        idempotency_key = data.get("idempotency_key")
        if idempotency_key:
            existing = self.repo.agreement_by_key(dossier_id, idempotency_key)
            if existing:
                if existing["payload_digest"] != digest:
                    raise ConflictError("同一幂等键不能用于不同的协议内容")
                return self._present_agreement(existing, replayed=True)
        if kind == "baseline":
            if self.repo.has_live_agreement(dossier_id, now):
                raise ConflictError("初始权属协议只能建立一次，后续请使用转让或补充协议")
        else:
            if parent is None:
                raise ValidationError("尚未建立有效权属，请先提交初始权属协议")
            if self.repo.open_agreement(dossier_id, now):
                raise ConflictError("存在尚未终结的权属协议，请先完成复核或撤回")
        agreement_code = require_code(data["agreement_code"], "协议编号") if data.get("agreement_code") else f"OWN-{uuid.uuid4().hex[:12].upper()}"
        if self.repo.agreement_by_code(agreement_code):
            raise ConflictError("协议编号已经存在")
        parent_units = self.repo.entry_units(parent["id"]) if parent else set()
        new_units = {entry["unit_id"] for entry in entries}
        cross_unit = parent is not None and self._is_cross_unit(parent["id"], parent_units, entries)
        requires_review = kind in REVIEWED_KINDS and cross_unit
        agreement_id = self.repo.insert_agreement(
            {
                "agreement_code": agreement_code,
                "dossier_id": dossier_id,
                "kind": kind,
                "parent_agreement_id": parent["id"] if parent else None,
                "idempotency_key": idempotency_key,
                "payload_digest": digest,
                "state": "pending_review" if requires_review else "approved",
                "effective_from": effective_from,
                "effective_until": effective_until,
                "reason": data["reason"],
                "note": data.get("note", ""),
            },
            principal.user_id,
            now,
        )
        for entry in entries:
            self.repo.insert_entry(agreement_id, entry, now)
        for signature in signatures:
            self.repo.insert_signature(agreement_id, signature, now)
        if requires_review:
            request = self.approvals.create(
                {
                    "action_type": "ownership_transfer",
                    "resource_type": "ownership_agreement",
                    "resource_id": agreement_id,
                    "payload": {
                        "dossier_id": dossier_id,
                        "agreement_code": agreement_code,
                        "kind": kind,
                        "from_units": sorted(parent_units),
                        "to_units": sorted(new_units),
                        "reason": data["reason"],
                    },
                    "expires_at": to_storage(self.clock.now() + timedelta(days=3)),
                },
                principal.user_id,
                f"APR-{uuid.uuid4().hex[:12].upper()}",
                now,
            )
            self.repo.attach_request(agreement_id, request["id"], now)
        self._append_event(
            dossier_id,
            agreement_id,
            "agreement.submitted",
            {
                "agreement_code": agreement_code,
                "kind": kind,
                "parent_agreement_id": parent["id"] if parent else None,
                "requires_review": requires_review,
                "cross_unit": cross_unit,
            },
            now,
            principal.user_id,
        )
        agreement = self.repo.agreement(agreement_id)
        if not requires_review and effective_from <= now:
            agreement = self._activate(agreement, now, principal.user_id)
        self.audit.record(
            principal,
            "ownership.agreement.submit",
            "ownership_agreement",
            str(agreement_id),
            after=agreement,
            metadata={"kind": kind, "requires_review": requires_review},
        )
        return self._present_agreement(self.repo.agreement(agreement_id), replayed=False)

    # 双人复核
    def decide(self, principal: Principal, agreement_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("approvals.decide")
        agreement = self.repo.agreement(agreement_id)
        if agreement["state"] != "pending_review":
            raise ConflictError("协议不在双人复核状态")
        request_id = agreement["approval_request_id"]
        if self.connection.execute(
            "SELECT 1 FROM approval_decisions WHERE request_id=? AND approver_user_id=?",
            (request_id, principal.user_id),
        ).fetchone():
            raise ConflictError("审批人已提交过决定")
        now = to_storage(self.clock.now())
        request = self.approvals.decide(request_id, principal.user_id, data["decision"], data.get("comment", ""), now)
        if request["state"] == "rejected":
            self.connection.execute(
                "UPDATE ownership_agreements SET state='rejected',decided_at=?,updated_at=? WHERE id=?",
                (now, now, agreement_id),
            )
            self._append_event(
                agreement["dossier_id"], agreement_id, "agreement.rejected",
                {"agreement_code": agreement["agreement_code"]}, now, principal.user_id,
            )
        elif request["state"] == "approved":
            self.connection.execute(
                "UPDATE ownership_agreements SET state='approved',decided_at=?,updated_at=? WHERE id=?",
                (now, now, agreement_id),
            )
            self._append_event(
                agreement["dossier_id"], agreement_id, "agreement.approved",
                {"agreement_code": agreement["agreement_code"]}, now, principal.user_id,
            )
            agreement = self.repo.agreement(agreement_id)
            if agreement["kind"] == "withdrawal":
                self._execute_withdrawal(agreement, now, principal.user_id)
            elif agreement["effective_from"] <= now:
                self._activate(agreement, now, principal.user_id)
        self.audit.record(
            principal,
            "ownership.agreement.decide",
            "ownership_agreement",
            str(agreement_id),
            metadata={"decision": data["decision"], "request_state": request["state"]},
        )
        return self._present_agreement(self.repo.agreement(agreement_id))

    # 生效
    def activate(self, principal: Principal, agreement_id: int) -> dict[str, Any]:
        principal.require("ownership.manage")
        agreement = self.repo.agreement(agreement_id)
        now = to_storage(self.clock.now())
        self._activate_due(agreement["dossier_id"], now)
        agreement = self.repo.agreement(agreement_id)
        if agreement["state"] == "effective":
            raise ConflictError("协议已经生效")
        if agreement["kind"] == "withdrawal":
            raise ValidationError("撤回协议在复核通过后自动生效")
        if agreement["state"] != "approved":
            raise ConflictError("协议当前不能生效")
        if agreement["effective_from"] > now:
            raise ConflictError("协议尚未到生效时间")
        agreement = self._activate(agreement, now, principal.user_id)
        self.audit.record(principal, "ownership.agreement.activate", "ownership_agreement", str(agreement_id), after=agreement)
        return self._present_agreement(agreement)

    # 撤回
    def withdraw(self, principal: Principal, agreement_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("ownership.manage")
        target = self.repo.agreement(agreement_id)
        now = to_storage(self.clock.now())
        digest = self._withdrawal_digest(target, data["reason"])
        idempotency_key = data.get("idempotency_key")
        if idempotency_key:
            existing = self.repo.agreement_by_key(target["dossier_id"], idempotency_key)
            if existing:
                if existing["payload_digest"] != digest:
                    raise ConflictError("同一幂等键不能用于不同的撤回请求")
                return self._present_agreement(existing, replayed=True)
        if target["state"] in {"rejected", "withdrawn"}:
            raise ConflictError("协议已终结，不能撤回")
        if self.repo.pending_withdrawal(agreement_id):
            raise ConflictError("该协议已有进行中的撤回流程")
        agreement_code = require_code(data["agreement_code"], "协议编号") if data.get("agreement_code") else f"WDR-{uuid.uuid4().hex[:12].upper()}"
        if self.repo.agreement_by_code(agreement_code):
            raise ConflictError("协议编号已经存在")
        requires_review = target["state"] == "effective"
        withdrawal_id = self.repo.insert_agreement(
            {
                "agreement_code": agreement_code,
                "dossier_id": target["dossier_id"],
                "kind": "withdrawal",
                "parent_agreement_id": target["id"],
                "idempotency_key": idempotency_key,
                "payload_digest": digest,
                "state": "pending_review" if requires_review else "effective",
                "effective_from": now,
                "reason": data["reason"],
            },
            principal.user_id,
            now,
        )
        self._append_event(
            target["dossier_id"],
            withdrawal_id,
            "agreement.submitted",
            {"agreement_code": agreement_code, "kind": "withdrawal", "parent_agreement_id": target["id"], "requires_review": requires_review},
            now,
            principal.user_id,
        )
        if requires_review:
            request = self.approvals.create(
                {
                    "action_type": "ownership_transfer",
                    "resource_type": "ownership_agreement",
                    "resource_id": withdrawal_id,
                    "payload": {
                        "dossier_id": target["dossier_id"],
                        "agreement_code": agreement_code,
                        "kind": "withdrawal",
                        "withdrawal_of": target["agreement_code"],
                        "reason": data["reason"],
                    },
                    "expires_at": to_storage(self.clock.now() + timedelta(days=3)),
                },
                principal.user_id,
                f"APR-{uuid.uuid4().hex[:12].upper()}",
                now,
            )
            self.repo.attach_request(withdrawal_id, request["id"], now)
        else:
            self.connection.execute(
                "UPDATE ownership_agreements SET state='withdrawn',decided_at=?,updated_at=? WHERE id=?",
                (now, now, target["id"]),
            )
            if target["approval_request_id"]:
                self.connection.execute(
                    "UPDATE approval_requests SET state='cancelled',version=version+1,updated_at=? WHERE id=? AND state='pending'",
                    (now, target["approval_request_id"]),
                )
            self._append_event(
                target["dossier_id"], target["id"], "agreement.withdrawn",
                {"agreement_code": target["agreement_code"], "withdrawal_agreement_id": withdrawal_id}, now, principal.user_id,
            )
            self._append_event(
                target["dossier_id"], withdrawal_id, "agreement.effective",
                {"agreement_code": agreement_code, "kind": "withdrawal"}, now, principal.user_id,
            )
        self.audit.record(
            principal,
            "ownership.agreement.withdraw",
            "ownership_agreement",
            str(target["id"]),
            metadata={"withdrawal_agreement_id": withdrawal_id, "requires_review": requires_review},
        )
        return self._present_agreement(self.repo.agreement(withdrawal_id), replayed=False)

    # 查询
    def agreement_detail(self, principal: Principal, agreement_id: int) -> dict[str, Any]:
        principal.require("ownership.read")
        agreement = self.repo.agreement(agreement_id)
        result = self._present_agreement(agreement)
        result["snapshot"] = self.repo.snapshot_rows_all(agreement_id)
        result["events"] = self.repo.events_for_agreement(agreement_id)
        return result

    def list_agreements(self, principal: Principal, dossier_id: int) -> list[dict[str, Any]]:
        principal.require("ownership.read")
        self.dossiers.get(dossier_id)
        return self.repo.list_agreements(dossier_id)

    def snapshot_current(self, principal: Principal, dossier_id: int) -> dict[str, Any]:
        principal.require("ownership.read")
        self.dossiers.get(dossier_id)
        now = to_storage(self.clock.now())
        self._activate_due(dossier_id, now)
        return self._present_snapshot(dossier_id, now, self._agreement_as_of(dossier_id, now))

    def snapshot_as_of(self, principal: Principal, dossier_id: int, at_raw: str) -> dict[str, Any]:
        principal.require("ownership.read")
        self.dossiers.get(dossier_id)
        at = to_storage(parse_timestamp(at_raw, "查询时间"))
        now = to_storage(self.clock.now())
        self._activate_due(dossier_id, now)
        return self._present_snapshot(dossier_id, at, self._agreement_as_of(dossier_id, at))

    def list_events(self, principal: Principal, dossier_id: int) -> list[dict[str, Any]]:
        principal.require("ownership.read")
        self.dossiers.get(dossier_id)
        return self.repo.events(dossier_id)

    # 引用钉住
    def pin_reference(self, principal: Principal, dossier_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("ownership.manage")
        self.dossiers.get(dossier_id)
        now = to_storage(self.clock.now())
        self._activate_due(dossier_id, now)
        if data.get("agreement_id"):
            agreement = self.repo.agreement(data["agreement_id"])
            if agreement["dossier_id"] != dossier_id:
                raise ValidationError("协议不属于该档案")
            if agreement["kind"] == "withdrawal" or agreement["state"] != "effective":
                raise ConflictError("只能引用已生效的权属协议")
        else:
            at = to_storage(parse_timestamp(data["as_of"], "引用时间")) if data.get("as_of") else now
            if at > now:
                raise ValidationError("引用时间不能晚于当前时间")
            agreement = self._agreement_as_of(dossier_id, at)
            if agreement is None:
                raise ConflictError("指定时间没有生效中的权属协议")
        existing = self.repo.reference(dossier_id, data["ref_type"], data["ref_code"])
        if existing:
            if existing["agreement_id"] != agreement["id"]:
                raise ConflictError("该引用已绑定其他权属协议")
            return {**existing, "replayed": True}
        reference = self.repo.insert_reference(dossier_id, data, agreement["id"], principal.user_id, now)
        self._append_event(
            dossier_id,
            agreement["id"],
            "reference.pinned",
            {"ref_type": data["ref_type"], "ref_code": data["ref_code"], "agreement_code": agreement["agreement_code"]},
            now,
            principal.user_id,
        )
        self.audit.record(
            principal,
            "ownership.reference.pin",
            "dossier",
            str(dossier_id),
            after=reference,
            metadata={"agreement_id": agreement["id"], "ref_type": data["ref_type"]},
        )
        return {**reference, "replayed": False}

    def list_references(self, principal: Principal, dossier_id: int) -> list[dict[str, Any]]:
        principal.require("ownership.read")
        self.dossiers.get(dossier_id)
        return self.repo.list_references(dossier_id)

    # 内部：校验
    def _is_cross_unit(self, parent_id: int, parent_units: set[int], entries: list[dict[str, Any]]) -> bool:
        """识别跨单位转让：单位集合变化，或发明人在既有单位之间迁移。"""
        new_units = {entry["unit_id"] for entry in entries}
        if new_units != parent_units:
            return True
        previous = {entry["inventor_id"]: entry["unit_id"] for entry in self.repo.entries(parent_id)}
        return any(
            entry["inventor_id"] in previous and previous[entry["inventor_id"]] != entry["unit_id"]
            for entry in entries
        )

    def _active_user(self, user_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT id,username,display_name,status FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("签署人用户不存在")
        user = dict(row)
        if user["status"] != "active":
            raise ValidationError("签署人账号不可用")
        return user

    def _validate_entries(self, raw_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[int] = set()
        shares: list[float] = []
        normalized: list[dict[str, Any]] = []
        for item in raw_entries:
            inventor_id = item["inventor_id"]
            if inventor_id in seen:
                raise ValidationError("同一发明人不能在协议中重复登记")
            seen.add(inventor_id)
            inventor = self.repo.inventor(inventor_id)
            if inventor is None or not inventor["is_active"]:
                raise ValidationError(f"发明人不存在或已停用：{inventor_id}")
            unit = self.repo.unit(item["unit_id"])
            if unit is None or not unit["is_active"]:
                raise ValidationError(f"权属单位不存在或已停用：{item['unit_id']}")
            valid_from = to_storage(parse_timestamp(item["valid_from"], "份额生效时间"))
            valid_until = to_storage(parse_timestamp(item["valid_until"], "份额失效时间"))
            if valid_until <= valid_from:
                raise ValidationError("份额失效时间必须晚于份额生效时间")
            share = float(item["share_percent"])
            shares.append(share)
            normalized.append(
                {
                    "inventor_id": inventor_id,
                    "unit_id": item["unit_id"],
                    "share_percent": share,
                    "valid_from": valid_from,
                    "valid_until": valid_until,
                }
            )
        if abs(math.fsum(shares) - SHARE_TOTAL) > SHARE_TOLERANCE:
            raise ValidationError("发明人贡献份额总和必须等于 100%")
        return normalized

    def _validate_signatures(
        self, raw_signatures: list[dict[str, Any]], entries: list[dict[str, Any]], now: str
    ) -> list[dict[str, Any]]:
        entry_inventors = {entry["inventor_id"] for entry in entries}
        entry_units = {entry["unit_id"] for entry in entries}
        signed_inventors: set[int] = set()
        signed_units: set[int] = set()
        seen_signers: set[tuple[int, str, int]] = set()
        normalized: list[dict[str, Any]] = []
        for signature in raw_signatures:
            target_id = signature["inventor_id"] if signature["role"] == "inventor" else signature["unit_id"]
            key = (signature["user_id"], signature["role"], target_id)
            if key in seen_signers:
                raise ValidationError("同一签署人不能以同一角色重复签署")
            seen_signers.add(key)
            self._active_user(signature["user_id"])
            signed_at = to_storage(parse_timestamp(signature["signed_at"], "签署时间"))
            if signed_at > now:
                raise ValidationError("签署时间不能晚于当前时间")
            if signature["role"] == "inventor":
                inventor_id = signature["inventor_id"]
                if inventor_id not in entry_inventors:
                    raise ValidationError("发明人签署与协议条目不匹配")
                if inventor_id in signed_inventors:
                    raise ValidationError("同一发明人不能重复签署")
                signed_inventors.add(inventor_id)
                normalized.append(
                    {"user_id": signature["user_id"], "role": "inventor", "inventor_id": inventor_id, "unit_id": None, "signed_at": signed_at}
                )
            else:
                unit_id = signature["unit_id"]
                if unit_id not in entry_units:
                    raise ValidationError("单位代表签署的单位不在协议条目内")
                if not self.repo.signer_authorized(unit_id, signature["user_id"], signed_at):
                    raise ValidationError("签署人未获得该单位的签署授权或授权已过期")
                signed_units.add(unit_id)
                normalized.append(
                    {"user_id": signature["user_id"], "role": "unit_representative", "inventor_id": None, "unit_id": unit_id, "signed_at": signed_at}
                )
        missing_inventors = entry_inventors - signed_inventors
        if missing_inventors:
            raise ValidationError(f"以下发明人缺少签署：{sorted(missing_inventors)}")
        missing_units = entry_units - signed_units
        if missing_units:
            raise ValidationError(f"以下单位缺少授权代表签署：{sorted(missing_units)}")
        return normalized

    def _resolve_parent(
        self, dossier_id: int, kind: str, parent_agreement_id: int | None, now: str
    ) -> dict[str, Any] | None:
        if kind == "baseline":
            if parent_agreement_id is not None:
                raise ValidationError("初始权属协议不能指定父协议")
            return None
        if parent_agreement_id is not None:
            parent = self.repo.agreement(parent_agreement_id)
            if parent["dossier_id"] != dossier_id:
                raise ValidationError("父协议不属于该档案")
            if parent["state"] != "effective":
                raise ConflictError("父协议尚未生效，不能作为变更基础")
            return parent
        return self._agreement_as_of(dossier_id, now)

    def _agreement_as_of(self, dossier_id: int, at: str) -> dict[str, Any] | None:
        """解析 at 时刻有效的协议：链式事件重放 + 协议有效期窗口。"""
        agreement = self.repo.active_agreement_at(dossier_id, at)
        if agreement is None:
            return None
        if agreement["effective_until"] and agreement["effective_until"] <= at:
            return None
        return agreement

    # 内部：摘要与事件
    @staticmethod
    def _payload_digest(
        kind: str,
        effective_from: str,
        effective_until: str | None,
        reason: str,
        entries: list[dict[str, Any]],
        signatures: list[dict[str, Any]],
    ) -> str:
        payload = {
            "kind": kind,
            "effective_from": effective_from,
            "effective_until": effective_until,
            "reason": reason,
            "entries": sorted(entries, key=lambda item: item["inventor_id"]),
            "signatures": sorted(
                signatures,
                key=lambda item: (item["user_id"], item["role"], item.get("inventor_id") or 0, item.get("unit_id") or 0),
            ),
        }
        return _digest_text(_canonical(payload))

    @staticmethod
    def _withdrawal_digest(target: dict[str, Any], reason: str) -> str:
        return _digest_text(_canonical({"kind": "withdrawal", "target_agreement_id": target["id"], "reason": reason}))

    def _append_event(
        self,
        dossier_id: int,
        agreement_id: int | None,
        event_type: str,
        payload: dict[str, Any],
        now: str,
        actor_user_id: int | None,
    ) -> None:
        last = self.repo.last_event(dossier_id)
        seq = last["seq"] + 1 if last else 1
        previous = last["chain_digest"] if last else GENESIS_CHAIN
        event_digest = _digest_text(
            _canonical(
                {
                    "agreement_id": agreement_id,
                    "dossier_id": dossier_id,
                    "event_type": event_type,
                    "occurred_at": now,
                    "payload": payload,
                    "seq": seq,
                }
            )
        )
        self.repo.insert_event(
            {
                "dossier_id": dossier_id,
                "agreement_id": agreement_id,
                "seq": seq,
                "event_type": event_type,
                "actor_user_id": actor_user_id,
                "payload": payload,
                "event_digest": event_digest,
                "chain_digest": _digest_text(f"{previous}:{event_digest}"),
                "occurred_at": now,
            }
        )

    # 内部：状态推进
    def _activate(self, agreement: dict[str, Any], now: str, actor_user_id: int | None) -> dict[str, Any]:
        updated = self.connection.execute(
            """UPDATE ownership_agreements SET state='effective',decided_at=COALESCE(decided_at,?),updated_at=?
               WHERE id=? AND state='approved'""",
            (now, now, agreement["id"]),
        )
        if updated.rowcount != 1:
            raise ConflictError("协议状态已变化，无法生效")
        for seq, entry in enumerate(self.repo.entries(agreement["id"]), start=1):
            self.repo.insert_snapshot(agreement["dossier_id"], agreement["id"], seq, entry, now)
        # 生效事件落在协议约定的法律生效时刻，提交时间仍由 submitted 事件与审计保留
        self._append_event(
            agreement["dossier_id"],
            agreement["id"],
            "agreement.effective",
            {"agreement_code": agreement["agreement_code"], "effective_from": agreement["effective_from"]},
            agreement["effective_from"],
            actor_user_id,
        )
        if agreement["approval_request_id"]:
            self.connection.execute(
                "UPDATE approval_requests SET state='executed',version=version+1,updated_at=? WHERE id=? AND state='approved'",
                (now, agreement["approval_request_id"]),
            )
        return self.repo.agreement(agreement["id"])

    def _activate_due(self, dossier_id: int, now: str) -> None:
        for agreement in self.repo.due_agreements(dossier_id, now):
            self._activate(agreement, now, None)

    def _execute_withdrawal(self, withdrawal: dict[str, Any], now: str, actor_user_id: int | None) -> None:
        target = self.repo.agreement(withdrawal["parent_agreement_id"])
        if target["state"] != "effective":
            raise ConflictError("被撤回的协议已不在生效状态")
        self.connection.execute(
            "UPDATE ownership_agreements SET state='withdrawn',decided_at=?,updated_at=? WHERE id=?",
            (now, now, target["id"]),
        )
        updated = self.connection.execute(
            """UPDATE ownership_agreements SET state='effective',effective_from=?,decided_at=?,updated_at=?
               WHERE id=? AND state IN ('approved','pending_review')""",
            (now, now, now, withdrawal["id"]),
        )
        if updated.rowcount != 1:
            raise ConflictError("撤回协议状态已变化")
        self._append_event(
            target["dossier_id"], target["id"], "agreement.withdrawn",
            {"agreement_code": target["agreement_code"], "withdrawal_agreement_id": withdrawal["id"]}, now, actor_user_id,
        )
        self._append_event(
            target["dossier_id"], withdrawal["id"], "agreement.effective",
            {"agreement_code": withdrawal["agreement_code"], "kind": "withdrawal"}, now, actor_user_id,
        )
        if withdrawal["approval_request_id"]:
            self.connection.execute(
                "UPDATE approval_requests SET state='executed',version=version+1,updated_at=? WHERE id=? AND state='approved'",
                (now, withdrawal["approval_request_id"]),
            )

    # 内部：展示
    def _present_agreement(self, agreement: dict[str, Any], replayed: bool = False) -> dict[str, Any]:
        return {
            **agreement,
            "entries": self.repo.entries(agreement["id"]),
            "signatures": self.repo.signatures(agreement["id"]),
            "replayed": replayed,
        }

    def _present_snapshot(self, dossier_id: int, at: str, agreement: dict[str, Any] | None) -> dict[str, Any]:
        if agreement is None:
            return {
                "dossier_id": dossier_id,
                "as_of": at,
                "state": "none",
                "agreement": None,
                "entries": [],
                "share_total": 0.0,
                "complete": False,
            }
        if self.repo.has_snapshot(agreement["id"]):
            rows = self.repo.snapshot_rows(agreement["id"], at)
        else:
            rows = self.repo.entries_at(agreement["id"], at)
        total = math.fsum(row["share_percent"] for row in rows)
        return {
            "dossier_id": dossier_id,
            "as_of": at,
            "state": "effective",
            "agreement": {
                key: agreement[key]
                for key in ("id", "agreement_code", "kind", "state", "effective_from", "effective_until", "parent_agreement_id")
            },
            "entries": rows,
            "share_total": round(total, 6),
            "complete": abs(total - SHARE_TOTAL) <= SHARE_TOLERANCE,
        }
