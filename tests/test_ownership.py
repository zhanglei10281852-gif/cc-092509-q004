from __future__ import annotations


def _make_user(client, admin, username, role_codes):
    response = client.post(
        "/api/users",
        headers=admin["headers"],
        json={
            "username": username,
            "password": "UserPass!234",
            "display_name": username,
            "role_codes": role_codes,
        },
    )
    assert response.status_code == 201, response.text
    user = response.json()
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "UserPass!234", "client_label": "tests"},
    )
    assert login.status_code == 200, login.text
    body = login.json()
    return {"id": user["id"], "headers": {"Authorization": f"Bearer {body['token']}"}}


def _bootstrap_dossier(client, admin):
    vault = client.post(
        "/api/dossiers/vaults",
        headers=admin["headers"],
        json={
            "code": "OWN-V1", "building": "档案楼", "room": "库一", "cabinet": "柜一",
            "shelf": "层一", "sensitivity": "normal", "capacity_units": 50,
        },
    ).json()
    batch = client.post(
        "/api/dossiers/batches",
        headers=admin["headers"],
        json={"intake_code": "OWN-BATCH", "project_code": "OWN", "expected_count": 1},
    ).json()
    dossier = client.post(
        "/api/dossiers",
        headers=admin["headers"],
        json={
            "dossier_code": "OWN-D1", "intake_id": batch["id"], "asset_type": "交底书",
            "quantity": 10, "unit": "份", "vault_id": vault["id"],
        },
    ).json()
    return dossier


def _create_units(client, admin):
    units = {}
    for code, name in (("UNIT-A", "甲研究院"), ("UNIT-B", "乙公司"), ("UNIT-C", "丙高校")):
        resp = client.post(
            "/api/ownership/units", headers=admin["headers"], json={"code": code, "name": name}
        )
        assert resp.status_code == 201, resp.text
        units[code] = resp.json()
    return units


def _initial_ownership(client, headers, dossier_id, inventor_user_id):
    payload = {
        "change_kind": "initial",
        "idempotency_key": "init-1",
        "effective_at": "2026-01-01T00:00:00+00:00",
        "shares": [
            {
                "inventor_name": "发明人甲",
                "inventor_user_id": inventor_user_id,
                "unit_code": "UNIT-A",
                "share": 0.6,
                "effective_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "inventor_name": "发明人乙",
                "unit_code": "UNIT-A",
                "share": 0.4,
                "effective_at": "2026-01-01T00:00:00+00:00",
            },
        ],
        "signer_user_ids": [inventor_user_id],
    }
    resp = client.post(f"/api/ownership/dossier/{dossier_id}/agreements", headers=headers, json=payload)
    assert resp.status_code == 201, resp.text
    return resp


def test_share_total_must_equal_one(client, admin):
    _create_units(client, admin)
    dossier = _bootstrap_dossier(client, admin)
    uid = admin["body"]["user"]["id"]
    resp = client.post(
        f"/api/ownership/dossier/{dossier['id']}/agreements",
        headers=admin["headers"],
        json={
            "change_kind": "initial",
            "idempotency_key": "bad-total",
            "effective_at": "2026-01-01T00:00:00+00:00",
            "shares": [
                {"inventor_name": "甲", "inventor_user_id": uid, "unit_code": "UNIT-A",
                 "share": 0.6, "effective_at": "2026-01-01T00:00:00+00:00"},
                {"inventor_name": "乙", "unit_code": "UNIT-A",
                 "share": 0.3, "effective_at": "2026-01-01T00:00:00+00:00"},
            ],
            "signer_user_ids": [uid],
        },
    )
    assert resp.status_code == 422
    assert "总和必须等于 1" in resp.json()["error"]["message"]


def test_signer_must_be_listed_inventor(client, admin):
    _create_units(client, admin)
    dossier = _bootstrap_dossier(client, admin)
    uid = admin["body"]["user"]["id"]
    other = _make_user(client, admin, "signerguy", ["dossier_manager"])
    resp = client.post(
        f"/api/ownership/dossier/{dossier['id']}/agreements",
        headers=admin["headers"],
        json={
            "change_kind": "initial",
            "idempotency_key": "bad-signer",
            "effective_at": "2026-01-01T00:00:00+00:00",
            "shares": [
                {"inventor_name": "甲", "inventor_user_id": uid, "unit_code": "UNIT-A",
                 "share": 1.0, "effective_at": "2026-01-01T00:00:00+00:00"},
            ],
            "signer_user_ids": [other["id"]],
        },
    )
    assert resp.status_code == 422
    assert "尚未亲自签署" in resp.json()["error"]["message"]


def test_share_validity_window_and_unknown_unit_rejected(client, admin):
    _create_units(client, admin)
    dossier = _bootstrap_dossier(client, admin)
    uid = admin["body"]["user"]["id"]
    bad_window = client.post(
        f"/api/ownership/dossier/{dossier['id']}/agreements",
        headers=admin["headers"],
        json={
            "change_kind": "initial",
            "idempotency_key": "bad-window",
            "effective_at": "2026-01-01T00:00:00+00:00",
            "shares": [
                {"inventor_name": "甲", "inventor_user_id": uid, "unit_code": "UNIT-A", "share": 1.0,
                 "effective_at": "2027-01-01T00:00:00+00:00", "expires_at": "2026-01-01T00:00:00+00:00"},
            ],
            "signer_user_ids": [uid],
        },
    )
    assert bad_window.status_code == 422
    assert "失效时间必须晚于生效时间" in bad_window.json()["error"]["message"]

    bad_unit = client.post(
        f"/api/ownership/dossier/{dossier['id']}/agreements",
        headers=admin["headers"],
        json={
            "change_kind": "initial",
            "idempotency_key": "bad-unit",
            "effective_at": "2026-01-01T00:00:00+00:00",
            "shares": [
                {"inventor_name": "甲", "inventor_user_id": uid, "unit_code": "UNIT-X", "share": 1.0,
                 "effective_at": "2026-01-01T00:00:00+00:00"},
            ],
            "signer_user_ids": [uid],
        },
    )
    assert bad_unit.status_code == 422
    assert "权属单位不存在" in bad_unit.json()["error"]["message"]


def test_cross_unit_rejection_blocks_effectiveness(client, admin):
    _create_units(client, admin)
    dossier = _bootstrap_dossier(client, admin)
    uid = admin["body"]["user"]["id"]
    _initial_ownership(client, admin["headers"], dossier["id"], uid)
    reviewer = _make_user(client, admin, "blocker", ["approver"])

    transfer = client.post(
        f"/api/ownership/dossier/{dossier['id']}/agreements",
        headers=admin["headers"],
        json={
            "change_kind": "transfer",
            "idempotency_key": "xfer-rej",
            "effective_at": "2026-12-31T00:00:00+00:00",
            "shares": [
                {"inventor_name": "发明人甲", "inventor_user_id": uid, "unit_code": "UNIT-B",
                 "share": 1.0, "effective_at": "2026-12-31T00:00:00+00:00"},
            ],
            "signer_user_ids": [uid],
        },
    )
    rejected = client.post(
        f"/api/ownership/agreements/{transfer.json()['id']}/reviews",
        headers=reviewer["headers"],
        json={"decision": "reject", "comment": "材料不全"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["state"] == "rejected"
    current = client.get(f"/api/ownership/dossier/{dossier['id']}/current", headers=admin["headers"])
    assert current.json()["version"] == 1


def test_same_unit_change_takes_immediate_effect_with_snapshot(client, admin):
    _create_units(client, admin)
    dossier = _bootstrap_dossier(client, admin)
    uid = admin["body"]["user"]["id"]
    initial = _initial_ownership(client, admin["headers"], dossier["id"], uid)
    assert initial.json()["state"] == "effective"

    current = client.get(f"/api/ownership/dossier/{dossier['id']}/current", headers=admin["headers"])
    assert current.status_code == 200
    assert current.json()["version"] == 1
    assert abs(sum(s["share"] for s in current.json()["shares"]) - 1.0) < 1e-9


def test_cross_unit_transfer_requires_dual_review(client, admin):
    _create_units(client, admin)
    dossier = _bootstrap_dossier(client, admin)
    uid = admin["body"]["user"]["id"]
    _initial_ownership(client, admin["headers"], dossier["id"], uid)

    reviewer_one = _make_user(client, admin, "reviewer1", ["approver"])
    reviewer_two = _make_user(client, admin, "reviewer2", ["approver"])

    transfer = client.post(
        f"/api/ownership/dossier/{dossier['id']}/agreements",
        headers=admin["headers"],
        json={
            "change_kind": "transfer",
            "idempotency_key": "xfer-1",
            "effective_at": "2026-12-31T00:00:00+00:00",
            "shares": [
                {"inventor_name": "发明人甲", "inventor_user_id": uid, "unit_code": "UNIT-B",
                 "share": 1.0, "effective_at": "2026-12-31T00:00:00+00:00"},
            ],
            "signer_user_ids": [uid],
        },
    )
    assert transfer.status_code == 201, transfer.text
    agreement = transfer.json()
    assert agreement["state"] == "pending_review"
    assert agreement["cross_unit"] == 1

    # 生效前查询仍是旧快照
    before = client.get(
        f"/api/ownership/dossier/{dossier['id']}/current?at=2026-06-01T00:00:00%2B00:00",
        headers=admin["headers"],
    )
    assert before.json()["version"] == 1
    assert before.json()["shares"][0]["unit_code"] == "UNIT-A"

    # 申请人不能自审
    own = client.post(
        f"/api/ownership/agreements/{agreement['id']}/reviews",
        headers=admin["headers"],
        json={"decision": "approve"},
    )
    assert own.status_code == 422

    # 单人复核后仍不生效
    first = client.post(
        f"/api/ownership/agreements/{agreement['id']}/reviews",
        headers=reviewer_one["headers"],
        json={"decision": "approve"},
    )
    assert first.status_code == 200
    assert first.json()["state"] == "pending_review"

    # 第二名独立复核人通过后生效
    second = client.post(
        f"/api/ownership/agreements/{agreement['id']}/reviews",
        headers=reviewer_two["headers"],
        json={"decision": "approve"},
    )
    assert second.status_code == 200
    assert second.json()["state"] == "effective"

    after = client.get(f"/api/ownership/dossier/{dossier['id']}/current", headers=admin["headers"])
    assert after.json()["version"] == 2
    assert after.json()["shares"][0]["unit_code"] == "UNIT-B"

    # 新协议生效后，历史时刻查询仍返回当时的旧快照（历史权属不被改写）
    historical = client.get(
        f"/api/ownership/dossier/{dossier['id']}/current?at=2026-06-01T00:00:00%2B00:00",
        headers=admin["headers"],
    )
    assert historical.json()["version"] == 1
    assert historical.json()["shares"][0]["unit_code"] == "UNIT-A"


def test_duplicate_agreement_submission_does_not_transfer_twice(client, admin):
    _create_units(client, admin)
    dossier = _bootstrap_dossier(client, admin)
    uid = admin["body"]["user"]["id"]
    _initial_ownership(client, admin["headers"], dossier["id"], uid)

    payload = {
        "change_kind": "transfer",
        "idempotency_key": "xfer-dup",
        "effective_at": "2026-12-31T00:00:00+00:00",
        "shares": [
            {"inventor_name": "发明人甲", "inventor_user_id": uid, "unit_code": "UNIT-C",
             "share": 1.0, "effective_at": "2026-12-31T00:00:00+00:00"},
        ],
        "signer_user_ids": [uid],
    }
    first = client.post(
        f"/api/ownership/dossier/{dossier['id']}/agreements", headers=admin["headers"], json=payload
    )
    second = client.post(
        f"/api/ownership/dossier/{dossier['id']}/agreements", headers=admin["headers"], json=payload
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["replayed"] is True

    agreements = client.get(
        f"/api/ownership/dossier/{dossier['id']}/agreements", headers=admin["headers"]
    ).json()
    assert len(agreements) == 2  # 初始 + 一次转让，重复提交未新增


def test_supplement_and_withdrawal_leave_chained_events(client, admin):
    _create_units(client, admin)
    dossier = _bootstrap_dossier(client, admin)
    uid = admin["body"]["user"]["id"]
    initial = _initial_ownership(client, admin["headers"], dossier["id"], uid)
    initial_agreement_id = initial.json()["id"]

    supplement = client.post(
        f"/api/ownership/dossier/{dossier['id']}/agreements",
        headers=admin["headers"],
        json={
            "change_kind": "supplement",
            "supersedes_agreement_id": initial_agreement_id,
            "idempotency_key": "supp-1",
            "effective_at": "2027-01-01T00:00:00+00:00",
            "shares": [
                {"inventor_name": "发明人甲", "inventor_user_id": uid, "unit_code": "UNIT-A",
                 "share": 0.7, "effective_at": "2027-01-01T00:00:00+00:00"},
                {"inventor_name": "发明人乙", "unit_code": "UNIT-A",
                 "share": 0.3, "effective_at": "2027-01-01T00:00:00+00:00"},
            ],
            "signer_user_ids": [uid],
        },
    )
    assert supplement.status_code == 201, supplement.text
    sup_id = supplement.json()["id"]

    withdraw = client.post(
        f"/api/ownership/dossier/{dossier['id']}/agreements",
        headers=admin["headers"],
        json={
            "change_kind": "withdrawal",
            "supersedes_agreement_id": sup_id,
            "idempotency_key": "wd-1",
            "effective_at": "2028-01-01T00:00:00+00:00",
            "shares": [
                {"inventor_name": "发明人甲", "inventor_user_id": uid, "unit_code": "UNIT-A",
                 "share": 0.6, "effective_at": "2028-01-01T00:00:00+00:00"},
                {"inventor_name": "发明人乙", "unit_code": "UNIT-A",
                 "share": 0.4, "effective_at": "2028-01-01T00:00:00+00:00"},
            ],
            "signer_user_ids": [uid],
        },
    )
    assert withdraw.status_code == 201, withdraw.text

    events = client.get(f"/api/ownership/dossier/{dossier['id']}/events", headers=admin["headers"]).json()
    assert events["chain_valid"] is True
    kinds = [e["event_type"] for e in events["events"]]
    assert "agreement.supplement" in kinds
    assert "agreement.withdrawal" in kinds
    seqs = [e["seq"] for e in events["events"]]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))


def test_downstream_versions_and_disclosures_pin_effective_snapshot(client, admin):
    _create_units(client, admin)
    dossier = _bootstrap_dossier(client, admin)
    uid = admin["body"]["user"]["id"]
    initial = _initial_ownership(client, admin["headers"], dossier["id"], uid)
    initial_snapshot = initial.json()["snapshot"]["id"]

    # 交底版本（受控副本）引用当时有效权属
    issue = client.post(
        f"/api/dossiers/{dossier['id']}/issue_copys",
        headers=admin["headers"],
        json={
            "requested_quantity": 1,
            "loss_quantity": 0,
            "children": [{"dossier_code": "OWN-D1-V2", "quantity": 1}],
        },
    )
    assert issue.status_code == 201, issue.text
    child_id = issue.json()["children"][0]["id"]
    version_ref = client.get(
        f"/api/ownership/references/disclosure_version/{child_id}", headers=admin["headers"]
    )
    assert version_ref.status_code == 200
    assert version_ref.json()["snapshot"]["id"] == initial_snapshot

    refs = client.get(f"/api/ownership/dossier/{dossier['id']}/references", headers=admin["headers"]).json()
    assert any(r["ref_kind"] == "disclosure_version" and r["snapshot_id"] == initial_snapshot for r in refs)


def test_disclosure_replay_and_family_pin_snapshot(client, admin):
    _create_units(client, admin)
    dossier = _bootstrap_dossier(client, admin)
    uid = admin["body"]["user"]["id"]
    initial = _initial_ownership(client, admin["headers"], dossier["id"], uid)
    initial_snapshot = initial.json()["snapshot"]["id"]

    payload = {"recipient_code": "PARTNER-Y", "quantity": 0.5, "idempotency_key": "ext-2", "note": "合作"}
    first = client.post(
        f"/api/dossiers/{dossier['id']}/disclosures", headers=admin["headers"], json=payload
    )
    assert first.status_code == 201, first.text
    record_id = first.json()["record"]["id"]
    second = client.post(
        f"/api/dossiers/{dossier['id']}/disclosures", headers=admin["headers"], json=payload
    )
    assert second.json()["replayed"] is True
    assert second.json()["record"]["id"] == record_id

    ref = client.get(
        f"/api/ownership/references/external_disclosure/{record_id}", headers=admin["headers"]
    )
    assert ref.status_code == 200
    assert ref.json()["snapshot"]["id"] == initial_snapshot

    # 专利家族引用根交底书当时有效权属
    family = client.post(
        "/api/ownership/families",
        headers=admin["headers"],
        json={"family_code": "FAM-01", "root_dossier_id": dossier["id"], "title": "核心专利族"},
    )
    assert family.status_code == 201, family.text
    assert family.json()["ownership_snapshot_id"] == initial_snapshot
    listed = client.get("/api/ownership/families", headers=admin["headers"]).json()
    assert any(f["family_code"] == "FAM-01" for f in listed)
