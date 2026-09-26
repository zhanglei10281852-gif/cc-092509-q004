"""权属协作流程的数据访问层。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.errors import NotFoundError


def _row(row: sqlite3.Row | None, message: str) -> dict[str, Any]:
    if row is None:
        raise NotFoundError(message)
    return dict(row)


class OwnershipUnitRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, data: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO ownership_units(code,name,is_active,created_at,updated_at) VALUES(?,?,'1',?,?)",
            (data["code"], data["name"], now, now),
        )
        return self.get(cursor.lastrowid)

    def get(self, unit_id: int) -> dict[str, Any]:
        return _row(
            self.connection.execute("SELECT * FROM ownership_units WHERE id=?", (unit_id,)).fetchone(),
            "权属单位不存在",
        )

    def by_code(self, code: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM ownership_units WHERE code=?", (code,)).fetchone()
        return dict(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM ownership_units WHERE is_active=1 ORDER BY code"
        ).fetchall()]


class OwnershipSnapshotRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def get(self, snapshot_id: int) -> dict[str, Any]:
        row = _row(
            self.connection.execute("SELECT * FROM ownership_snapshots WHERE id=?", (snapshot_id,)).fetchone(),
            "权属快照不存在",
        )
        row["shares"] = json.loads(row.pop("shares_json"))
        return row

    def latest(self, subject_type: str, subject_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT * FROM ownership_snapshots
               WHERE subject_type=? AND subject_id=? ORDER BY version DESC LIMIT 1""",
            (subject_type, subject_id),
        ).fetchone()
        return self._hydrate(row)

    def effective_at(self, subject_type: str, subject_id: int, at: str) -> dict[str, Any] | None:
        """返回 at 时刻生效的快照：已生效且未在该时刻前被取代。"""
        row = self.connection.execute(
            """SELECT * FROM ownership_snapshots
               WHERE subject_type=? AND subject_id=? AND effective_at<=?
                 AND (superseded_at IS NULL OR superseded_at>?)
               ORDER BY version DESC LIMIT 1""",
            (subject_type, subject_id, at, at),
        ).fetchone()
        return self._hydrate(row)

    def current(self, subject_type: str, subject_id: int, at: str | None = None) -> dict[str, Any] | None:
        if at:
            return self.effective_at(subject_type, subject_id, at)
        return self.latest(subject_type, subject_id)

    def insert(
        self,
        subject_type: str,
        subject_id: int,
        shares: list[dict[str, Any]],
        effective_at: str,
        now: str,
        agreement_id: int | None,
    ) -> dict[str, Any]:
        latest = self.latest(subject_type, subject_id)
        version = (latest["version"] + 1) if latest else 1
        cursor = self.connection.execute(
            """INSERT INTO ownership_snapshots(
                   subject_type,subject_id,version,agreement_id,effective_at,superseded_at,
                   shares_json,created_at
               ) VALUES(?,?,?,?,?,NULL,?,?)""",
            (
                subject_type, subject_id, version, agreement_id, effective_at,
                json.dumps(shares, ensure_ascii=False), now,
            ),
        )
        if latest:
            self.connection.execute(
                "UPDATE ownership_snapshots SET superseded_at=? WHERE id=?",
                (effective_at, latest["id"]),
            )
        return self.get(cursor.lastrowid)

    def _hydrate(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["shares"] = json.loads(item.pop("shares_json"))
        return item


class AgreementRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def insert(
        self,
        *,
        subject_type: str,
        subject_id: int,
        change_kind: str,
        supersedes_id: int | None,
        agreement_code: str,
        idempotency_key: str,
        fingerprint: str,
        cross_unit: bool,
        required_approvals: int,
        requested_by: int,
        effective_at: str,
        note: str,
        shares: list[dict[str, Any]],
        signer_user_ids: list[int],
        now: str,
    ) -> dict[str, Any]:
        state = "pending_review" if cross_unit else "effective"
        cursor = self.connection.execute(
            """INSERT INTO ownership_agreements(
                   agreement_code,subject_type,subject_id,change_kind,supersedes_agreement_id,
                   idempotency_key,request_fingerprint,cross_unit,state,required_approvals,
                   effective_at,requested_by,note,payload_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                agreement_code, subject_type, subject_id, change_kind, supersedes_id,
                idempotency_key, fingerprint, int(cross_unit), state, required_approvals,
                effective_at if not cross_unit else None, requested_by, note,
                json.dumps(
                    {"shares": shares, "effective_at": effective_at, "signer_user_ids": signer_user_ids},
                    ensure_ascii=False,
                ),
                now, now,
            ),
        )
        agreement_id = cursor.lastrowid
        for user_id in signer_user_ids:
            self.connection.execute(
                "INSERT INTO ownership_agreement_signers(agreement_id,user_id,signer_role,signed_at) VALUES(?,?,?,?)",
                (agreement_id, user_id, "inventor", now),
            )
        return self.get(agreement_id)

    def get(self, agreement_id: int) -> dict[str, Any]:
        row = _row(
            self.connection.execute("SELECT * FROM ownership_agreements WHERE id=?", (agreement_id,)).fetchone(),
            "权属协议不存在",
        )
        row["payload"] = json.loads(row.pop("payload_json"))
        row["signers"] = [dict(item) for item in self.connection.execute(
            "SELECT * FROM ownership_agreement_signers WHERE agreement_id=? ORDER BY id", (agreement_id,)
        ).fetchall()]
        row["reviews"] = [dict(item) for item in self.connection.execute(
            "SELECT * FROM ownership_reviews WHERE agreement_id=? ORDER BY id", (agreement_id,)
        ).fetchall()]
        return row

    def by_idempotency(self, subject_type: str, subject_id: int, key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM ownership_agreements WHERE subject_type=? AND subject_id=? AND idempotency_key=?",
            (subject_type, subject_id, key),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item

    def list_for_subject(self, subject_type: str, subject_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM ownership_agreements WHERE subject_type=? AND subject_id=? ORDER BY id",
            (subject_type, subject_id),
        ).fetchall()]

    def set_state(self, agreement_id: int, state: str, now: str, *, effective_at: str | None = None) -> None:
        self.connection.execute(
            "UPDATE ownership_agreements SET state=?,effective_at=COALESCE(?,effective_at),updated_at=? WHERE id=?",
            (state, effective_at, now, agreement_id),
        )

    def add_review(self, agreement_id: int, reviewer_user_id: int, decision: str, comment: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO ownership_reviews(agreement_id,reviewer_user_id,decision,comment,decided_at) VALUES(?,?,?,?,?)",
            (agreement_id, reviewer_user_id, decision, comment, now),
        )

    def approval_count(self, agreement_id: int) -> int:
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM ownership_reviews WHERE agreement_id=? AND decision='approve'",
            (agreement_id,),
        ).fetchone()[0])


class OwnershipEventRepository:
    """按主体追加哈希链式事件，撤回/补充协议因此形成可校验的事件链。"""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def append(
        self,
        subject_type: str,
        subject_id: int,
        event_type: str,
        actor_user_id: int | None,
        now: str,
        *,
        agreement_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        last = self.connection.execute(
            "SELECT id,seq,chain_digest FROM ownership_events WHERE subject_type=? AND subject_id=? ORDER BY seq DESC LIMIT 1",
            (subject_type, subject_id),
        ).fetchone()
        seq = (last["seq"] + 1) if last else 1
        prev_id = last["id"] if last else None
        prev_digest = last["chain_digest"] if last else None
        digest = self._digest(
            seq, event_type, agreement_id, subject_type, subject_id, details or {}, prev_digest, now
        )
        cursor = self.connection.execute(
            """INSERT INTO ownership_events(
                   subject_type,subject_id,agreement_id,seq,event_type,actor_user_id,
                   details_json,prev_event_id,chain_digest,occurred_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                subject_type, subject_id, agreement_id, seq, event_type, actor_user_id,
                json.dumps(details or {}, ensure_ascii=False), prev_id, digest, now,
            ),
        )
        return self.get(cursor.lastrowid)

    def get(self, event_id: int) -> dict[str, Any]:
        row = _row(
            self.connection.execute("SELECT * FROM ownership_events WHERE id=?", (event_id,)).fetchone(),
            "权属事件不存在",
        )
        row["details"] = json.loads(row.pop("details_json"))
        return row

    def list_for_subject(self, subject_type: str, subject_id: int) -> list[dict[str, Any]]:
        result = []
        for row in self.connection.execute(
            "SELECT * FROM ownership_events WHERE subject_type=? AND subject_id=? ORDER BY seq",
            (subject_type, subject_id),
        ).fetchall():
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def verify_chain(self, subject_type: str, subject_id: int) -> bool:
        previous_digest = None
        for item in self.list_for_subject(subject_type, subject_id):
            expected = self._digest(
                item["seq"], item["event_type"], item["agreement_id"], subject_type, subject_id,
                item["details"], previous_digest, item["occurred_at"],
            )
            if item["chain_digest"] != expected:
                return False
            previous_digest = item["chain_digest"]
        return True

    @staticmethod
    def _digest(
        seq: int,
        event_type: str,
        agreement_id: int | None,
        subject_type: str,
        subject_id: int,
        details: dict[str, Any],
        previous_digest: str | None,
        occurred_at: str,
    ) -> str:
        import hashlib

        canonical = json.dumps(
            {
                "seq": seq,
                "event_type": event_type,
                "agreement_id": agreement_id,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "details": details,
                "prev": previous_digest,
                "occurred_at": occurred_at,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class OwnershipReferenceRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def pin(
        self,
        *,
        ref_kind: str,
        ref_id: int,
        ref_code: str,
        subject_type: str,
        subject_id: int,
        snapshot_id: int,
        now: str,
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO ownership_references(
                   ref_kind,ref_id,ref_code,subject_type,subject_id,snapshot_id,pinned_at,created_at
               ) VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(ref_kind,ref_id) DO UPDATE SET
                   ref_code=excluded.ref_code,
                   subject_type=excluded.subject_type,
                   subject_id=excluded.subject_id,
                   snapshot_id=excluded.snapshot_id,
                   pinned_at=excluded.pinned_at""",
            (ref_kind, ref_id, ref_code, subject_type, subject_id, snapshot_id, now, now),
        )
        return dict(self.connection.execute(
            "SELECT * FROM ownership_references WHERE id=?", (cursor.lastrowid,)
        ).fetchone())

    def for_ref(self, ref_kind: str, ref_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM ownership_references WHERE ref_kind=? AND ref_id=?", (ref_kind, ref_id)
        ).fetchone()
        return dict(row) if row else None

    def list_for_subject(self, subject_type: str, subject_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM ownership_references WHERE subject_type=? AND subject_id=? ORDER BY id",
            (subject_type, subject_id),
        ).fetchall()]


class PatentFamilyRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(
        self, family_code: str, root_dossier_id: int, title: str, created_by: int,
        snapshot_id: int | None, now: str,
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO patent_families(
                   family_code,root_dossier_id,title,created_by,ownership_snapshot_id,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (family_code, root_dossier_id, title, created_by, snapshot_id, now),
        )
        return self.get(cursor.lastrowid)

    def get(self, family_id: int) -> dict[str, Any]:
        return _row(
            self.connection.execute("SELECT * FROM patent_families WHERE id=?", (family_id,)).fetchone(),
            "专利家族不存在",
        )

    def by_code(self, family_code: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM patent_families WHERE family_code=?", (family_code,)).fetchone()
        return dict(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            """SELECT f.*,d.dossier_code AS root_dossier_code
               FROM patent_families f JOIN dossiers d ON d.id=f.root_dossier_id ORDER BY f.id"""
        ).fetchall()]
