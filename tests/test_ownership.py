from __future__ import annotations

import hashlib
import json

T0 = "2020-01-01T00:00:00+00:00"
T0_LATER = "2020-06-01T00:00:00+00:00"
T1 = "2021-01-01T00:00:00+00:00"
T1_LATER = "2021-06-01T00:00:00+00:00"
ENTRY_UNTIL = "2120-01-01T00:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"
FUTURE_LATER = "2099-06-01T00:00:00+00:00"
SIGNED = "2020-01-01T00:00:00+00:00"


def _login(client, username, password="Review!23456"):
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password, "client_label": "tests"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _create_user(client, admin, username, roles):
    response = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": "Review!23456", "display_name": username, "role_codes": roles},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _approver(client, admin, username):
    _create_user(client, admin, username, ["approver"])
    return {"headers": _login(client, username)}


def _bootstrap_dossier(client, admin):
    vault = client.post(
        "/api/dossiers/vaults",
        headers=admin["headers"],
        json={
            "code": "OWN-01",
            "building": "档案楼",
            "room": "常温库",
            "cabinet": "一号柜",
            "shelf": "一层",
            "sensitivity": "normal",
            "capacity_units": 50,
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
            "dossier_code": "OWN-DOSSIER",
            "intake_id": batch["id"],
            "asset_type": "专利交底书",
            "quantity": 1,
            "unit": "份",
            "vault_id": vault["id"],
        },
    ).json()
    return dossier


def _bootstrap_world(client, admin):
    admin_id = admin["body"]["user"]["id"]
    units = {}
    for key, code, name in (("a", "UNIT-A", "甲研究院"), ("b", "UNIT-B", "乙公司")):
        response = client.post(
            "/api/ownership/units",
            headers=admin["headers"],
            json={"unit_code": code, "name": name},
        )
        assert response.status_code == 201, response.text
        units[key] = response.json()
    inventors = {}
    users = {"admin": admin_id}
    for key, code, name, home in (("x", "INV-X", "张三", "a"), ("y", "INV-Y", "李四", "a"), ("z", "INV-Z", "王五", "b")):
        response = client.post(
            "/api/ownership/inventors",
            headers=admin["headers"],
            json={"inventor_code": code, "display_name": name, "home_unit_id": units[home]["id"]},
        )
        assert response.status_code == 201, response.text
        inventors[key] = response.json()
        users[key] = _create_user(client, admin, f"inventor.{key}", [])
    for key in ("a", "b"):
        response = client.post(
            f"/api/ownership/units/{units[key]['id']}/signers",
            headers=admin["headers"],
            json={"user_id": admin_id, "title": "法务代表", "valid_from": T0},
        )
        assert response.status_code == 201, response.text
    return {"units": units, "inventors": inventors, "users": users}


def _entries(world, *specs):
    return [
        {
            "inventor_id": world["inventors"][inventor]["id"],
            "unit_id": world["units"][unit]["id"],
            "share_percent": share,
            "valid_from": T0,
            "valid_until": ENTRY_UNTIL,
        }
        for inventor, unit, share in specs
    ]


def _signatures(world, inventor_keys, unit_keys, signed_at=SIGNED):
    signatures = [
        {
            "user_id": world["users"][key],
            "role": "inventor",
            "inventor_id": world["inventors"][key]["id"],
            "signed_at": signed_at,
        }
        for key in inventor_keys
    ]
    signatures += [
        {
            "user_id": world["users"]["admin"],
            "role": "unit_representative",
            "unit_id": world["units"][key]["id"],
            "signed_at": signed_at,
        }
        for key in unit_keys
    ]
    return signatures


def _submit(client, admin, dossier_id, kind="baseline", **overrides):
    payload = {
        "kind": kind,
        "effective_from": T0,
        "reason": "权属登记",
        "entries": [],
        "signatures": [],
    }
    payload.update(overrides)
    return client.post(
        f"/api/ownership/dossiers/{dossier_id}/agreements",
        headers=admin["headers"],
        json=payload,
    )


def _baseline(client, admin, dossier_id, world):
    response = _submit(
        client,
        admin,
        dossier_id,
        entries=_entries(world, ("x", "a", 60), ("y", "a", 40)),
        signatures=_signatures(world, ["x", "y"], ["a"]),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "effective"
    return body


def _current(client, admin, dossier_id):
    response = client.get(f"/api/ownership/dossiers/{dossier_id}/ownership", headers=admin["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def _as_of(client, admin, dossier_id, at):
    response = client.get(
        f"/api/ownership/dossiers/{dossier_id}/ownership/as-of",
        headers=admin["headers"],
        params={"at": at},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _shares(snapshot):
    return {entry["inventor_code"]: (entry["share_percent"], entry["unit_code"]) for entry in snapshot["entries"]}


def test_baseline_establishes_current_snapshot(client, admin):
    dossier = _bootstrap_dossier(client, admin)
    world = _bootstrap_world(client, admin)
    agreement = _baseline(client, admin, dossier["id"], world)
    assert agreement["replayed"] is False
    assert len(agreement["entries"]) == 2
    assert len(agreement["signatures"]) == 3
    current = _current(client, admin, dossier["id"])
    assert current["state"] == "effective"
    assert current["complete"] is True
    assert current["share_total"] == 100.0
    assert current["agreement"]["id"] == agreement["id"]
    assert _shares(current) == {"INV-X": (60.0, "UNIT-A"), "INV-Y": (40.0, "UNIT-A")}
    detail = client.get(f"/api/ownership/agreements/{agreement['id']}", headers=admin["headers"])
    assert detail.status_code == 200
    assert len(detail.json()["snapshot"]) == 2
    assert [event["event_type"] for event in detail.json()["events"]] == ["agreement.submitted", "agreement.effective"]


def test_share_total_and_entry_validation(client, admin):
    dossier = _bootstrap_dossier(client, admin)
    world = _bootstrap_world(client, admin)
    bad_total = _submit(
        client,
        admin,
        dossier["id"],
        entries=_entries(world, ("x", "a", 60), ("y", "a", 30)),
        signatures=_signatures(world, ["x", "y"], ["a"]),
    )
    assert bad_total.status_code == 422
    assert "份额" in bad_total.json()["error"]["message"]
    duplicated = _submit(
        client,
        admin,
        dossier["id"],
        entries=_entries(world, ("x", "a", 50), ("x", "a", 50)),
        signatures=_signatures(world, ["x"], ["a"]),
    )
    assert duplicated.status_code == 422
    bad_window = _entries(world, ("x", "a", 100))
    bad_window[0]["valid_until"] = T0
    invalid_window = _submit(client, admin, dossier["id"], entries=bad_window, signatures=_signatures(world, ["x"], ["a"]))
    assert invalid_window.status_code == 422
    unknown_inventor = _entries(world, ("x", "a", 100))
    unknown_inventor[0]["inventor_id"] = 99999
    missing_inventor = _submit(client, admin, dossier["id"], entries=unknown_inventor, signatures=_signatures(world, ["x"], ["a"]))
    assert missing_inventor.status_code == 422


def test_signer_qualification_enforced(client, admin):
    dossier = _bootstrap_dossier(client, admin)
    world = _bootstrap_world(client, admin)
    missing_inventor_signature = _submit(
        client,
        admin,
        dossier["id"],
        entries=_entries(world, ("x", "a", 60), ("y", "a", 40)),
        signatures=_signatures(world, ["x"], ["a"]),
    )
    assert missing_inventor_signature.status_code == 422
    assert "签署" in missing_inventor_signature.json()["error"]["message"]
    unauthorized = _signatures(world, ["x", "y"], [])
    unauthorized.append(
        {
            "user_id": world["users"]["z"],
            "role": "unit_representative",
            "unit_id": world["units"]["a"]["id"],
            "signed_at": SIGNED,
        }
    )
    not_authorized = _submit(
        client,
        admin,
        dossier["id"],
        entries=_entries(world, ("x", "a", 60), ("y", "a", 40)),
        signatures=unauthorized,
    )
    assert not_authorized.status_code == 422
    assert "授权" in not_authorized.json()["error"]["message"]
    expired = client.post(
        f"/api/ownership/units/{world['units']['b']['id']}/signers",
        headers=admin["headers"],
        json={"user_id": world["users"]["z"], "title": "已离任代表", "valid_from": T0, "valid_until": T0_LATER},
    )
    assert expired.status_code == 201
    expired_signature = _signatures(world, ["x", "z"], [])
    expired_signature.append(
        {
            "user_id": world["users"]["z"],
            "role": "unit_representative",
            "unit_id": world["units"]["b"]["id"],
            "signed_at": T1,
        }
    )
    expired_grant = _submit(
        client,
        admin,
        dossier["id"],
        entries=_entries(world, ("x", "b", 50), ("z", "b", 50)),
        signatures=expired_signature,
    )
    assert expired_grant.status_code == 422
    assert "授权" in expired_grant.json()["error"]["message"]


def test_cross_unit_assignment_requires_dual_review(client, admin):
    dossier = _bootstrap_dossier(client, admin)
    world = _bootstrap_world(client, admin)
    baseline = _baseline(client, admin, dossier["id"], world)
    approver_one = _approver(client, admin, "approver.one")
    approver_two = _approver(client, admin, "approver.two")
    response = _submit(
        client,
        admin,
        dossier["id"],
        kind="assignment",
        effective_from=T1,
        reason="整体转让给乙公司",
        entries=_entries(world, ("x", "b", 50), ("z", "b", 50)),
        signatures=_signatures(world, ["x", "z"], ["b"]),
        idempotency_key="TR-001",
    )
    assert response.status_code == 201, response.text
    assignment = response.json()
    assert assignment["state"] == "pending_review"
    assert assignment["approval_request_id"]
    assert _current(client, admin, dossier["id"])["agreement"]["id"] == baseline["id"]
    own_decision = client.post(
        f"/api/ownership/agreements/{assignment['id']}/decisions",
        headers=admin["headers"],
        json={"decision": "approve"},
    )
    assert own_decision.status_code == 422
    first = client.post(
        f"/api/ownership/agreements/{assignment['id']}/decisions",
        headers=approver_one["headers"],
        json={"decision": "approve"},
    )
    assert first.status_code == 200
    assert first.json()["state"] == "pending_review"
    repeated = client.post(
        f"/api/ownership/agreements/{assignment['id']}/decisions",
        headers=approver_one["headers"],
        json={"decision": "approve"},
    )
    assert repeated.status_code == 409
    second = client.post(
        f"/api/ownership/agreements/{assignment['id']}/decisions",
        headers=approver_two["headers"],
        json={"decision": "approve"},
    )
    assert second.status_code == 200
    assert second.json()["state"] == "effective"
    current = _current(client, admin, dossier["id"])
    assert current["agreement"]["id"] == assignment["id"]
    assert _shares(current) == {"INV-X": (50.0, "UNIT-B"), "INV-Z": (50.0, "UNIT-B")}
    before = _as_of(client, admin, dossier["id"], T0_LATER)
    assert before["agreement"]["id"] == baseline["id"]
    assert _shares(before) == {"INV-X": (60.0, "UNIT-A"), "INV-Y": (40.0, "UNIT-A")}


def test_same_unit_assignment_skips_review(client, admin):
    dossier = _bootstrap_dossier(client, admin)
    world = _bootstrap_world(client, admin)
    _baseline(client, admin, dossier["id"], world)
    response = _submit(
        client,
        admin,
        dossier["id"],
        kind="assignment",
        effective_from=T1,
        reason="单位内部份额调整",
        entries=_entries(world, ("x", "a", 70), ("y", "a", 30)),
        signatures=_signatures(world, ["x", "y"], ["a"]),
    )
    assert response.status_code == 201, response.text
    assert response.json()["state"] == "effective"
    assert _shares(_current(client, admin, dossier["id"])) == {"INV-X": (70.0, "UNIT-A"), "INV-Y": (30.0, "UNIT-A")}


def test_inventor_moving_between_existing_units_triggers_review(client, admin):
    dossier = _bootstrap_dossier(client, admin)
    world = _bootstrap_world(client, admin)
    response = _submit(
        client,
        admin,
        dossier["id"],
        entries=_entries(world, ("x", "a", 60), ("z", "b", 40)),
        signatures=_signatures(world, ["x", "z"], ["a", "b"]),
    )
    assert response.status_code == 201, response.text
    moved = _submit(
        client,
        admin,
        dossier["id"],
        kind="assignment",
        effective_from=T1,
        reason="张三成果归属调整至乙公司",
        entries=_entries(world, ("x", "b", 60), ("z", "b", 40)),
        signatures=_signatures(world, ["x", "z"], ["b"]),
    )
    assert moved.status_code == 201, moved.text
    assert moved.json()["state"] == "pending_review"


def test_scheduled_agreement_and_activation_window(client, admin):
    dossier = _bootstrap_dossier(client, admin)
    world = _bootstrap_world(client, admin)
    baseline = _baseline(client, admin, dossier["id"], world)
    response = _submit(
        client,
        admin,
        dossier["id"],
        kind="assignment",
        effective_from=FUTURE,
        reason="预约未来生效的份额调整",
        entries=_entries(world, ("x", "a", 80), ("y", "a", 20)),
        signatures=_signatures(world, ["x", "y"], ["a"]),
    )
    assert response.status_code == 201, response.text
    scheduled = response.json()
    assert scheduled["state"] == "approved"
    assert _current(client, admin, dossier["id"])["agreement"]["id"] == baseline["id"]
    early = client.post(f"/api/ownership/agreements/{scheduled['id']}/activate", headers=admin["headers"])
    assert early.status_code == 409
    projected = _as_of(client, admin, dossier["id"], FUTURE_LATER)
    assert projected["agreement"]["id"] == scheduled["id"]
    assert _shares(projected) == {"INV-X": (80.0, "UNIT-A"), "INV-Y": (20.0, "UNIT-A")}


def test_idempotent_resubmission_never_transfers_twice(client, admin):
    dossier = _bootstrap_dossier(client, admin)
    world = _bootstrap_world(client, admin)
    _baseline(client, admin, dossier["id"], world)
    payload = {
        "kind": "assignment",
        "effective_from": T1,
        "reason": "份额调整",
        "entries": _entries(world, ("x", "a", 70), ("y", "a", 30)),
        "signatures": _signatures(world, ["x", "y"], ["a"]),
        "idempotency_key": "TR-REPLAY",
    }
    first = _submit(client, admin, dossier["id"], **payload)
    assert first.status_code == 201, first.text
    second = _submit(client, admin, dossier["id"], **payload)
    assert second.status_code == 201
    assert second.json()["replayed"] is True
    assert second.json()["id"] == first.json()["id"]
    agreements = client.get(f"/api/ownership/dossiers/{dossier['id']}/agreements", headers=admin["headers"]).json()
    assert len([item for item in agreements if item["kind"] != "withdrawal"]) == 2
    current = _current(client, admin, dossier["id"])
    assert _shares(current) == {"INV-X": (70.0, "UNIT-A"), "INV-Y": (30.0, "UNIT-A")}
    changed = dict(payload, entries=_entries(world, ("x", "a", 55), ("y", "a", 45)))
    conflict = _submit(client, admin, dossier["id"], **changed)
    assert conflict.status_code == 409


def test_withdrawal_of_pending_agreement_is_immediate(client, admin):
    dossier = _bootstrap_dossier(client, admin)
    world = _bootstrap_world(client, admin)
    baseline = _baseline(client, admin, dossier["id"], world)
    scheduled = _submit(
        client,
        admin,
        dossier["id"],
        kind="assignment",
        effective_from=FUTURE,
        reason="预约调整",
        entries=_entries(world, ("x", "a", 80), ("y", "a", 20)),
        signatures=_signatures(world, ["x", "y"], ["a"]),
    ).json()
    withdrawal = client.post(
        f"/api/ownership/agreements/{scheduled['id']}/withdraw",
        headers=admin["headers"],
        json={"reason": "签署前发现份额有误", "idempotency_key": "WD-001"},
    )
    assert withdrawal.status_code == 201, withdrawal.text
    assert withdrawal.json()["kind"] == "withdrawal"
    assert withdrawal.json()["state"] == "effective"
    replay = client.post(
        f"/api/ownership/agreements/{scheduled['id']}/withdraw",
        headers=admin["headers"],
        json={"reason": "签署前发现份额有误", "idempotency_key": "WD-001"},
    )
    assert replay.status_code == 201
    assert replay.json()["replayed"] is True
    detail = client.get(f"/api/ownership/agreements/{scheduled['id']}", headers=admin["headers"]).json()
    assert detail["state"] == "withdrawn"
    assert _current(client, admin, dossier["id"])["agreement"]["id"] == baseline["id"]
    again = client.post(
        f"/api/ownership/agreements/{scheduled['id']}/withdraw",
        headers=admin["headers"],
        json={"reason": "重复撤回"},
    )
    assert again.status_code == 409


def test_withdrawal_of_effective_agreement_requires_dual_review_and_reverts(client, admin):
    dossier = _bootstrap_dossier(client, admin)
    world = _bootstrap_world(client, admin)
    baseline = _baseline(client, admin, dossier["id"], world)
    assignment = _submit(
        client,
        admin,
        dossier["id"],
        kind="assignment",
        effective_from=T1,
        reason="份额调整",
        entries=_entries(world, ("x", "a", 70), ("y", "a", 30)),
        signatures=_signatures(world, ["x", "y"], ["a"]),
    ).json()
    assert assignment["state"] == "effective"
    approver_one = _approver(client, admin, "approver.one")
    approver_two = _approver(client, admin, "approver.two")
    withdrawal = client.post(
        f"/api/ownership/agreements/{assignment['id']}/withdraw",
        headers=admin["headers"],
        json={"reason": "转让协议被法务撤回"},
    )
    assert withdrawal.status_code == 201, withdrawal.text
    withdrawal_id = withdrawal.json()["id"]
    assert withdrawal.json()["state"] == "pending_review"
    assert _current(client, admin, dossier["id"])["agreement"]["id"] == assignment["id"]
    for approver in (approver_one, approver_two):
        decision = client.post(
            f"/api/ownership/agreements/{withdrawal_id}/decisions",
            headers=approver["headers"],
            json={"decision": "approve"},
        )
        assert decision.status_code == 200, decision.text
    detail = client.get(f"/api/ownership/agreements/{assignment['id']}", headers=admin["headers"]).json()
    assert detail["state"] == "withdrawn"
    current = _current(client, admin, dossier["id"])
    assert current["agreement"]["id"] == baseline["id"]
    assert _shares(current) == {"INV-X": (60.0, "UNIT-A"), "INV-Y": (40.0, "UNIT-A")}
    # 撤回生效前的历史时点仍返回当时有效的协议，不被撤回悄悄改写
    during = _as_of(client, admin, dossier["id"], T1_LATER)
    assert during["agreement"]["id"] == assignment["id"]
    assert _shares(during) == {"INV-X": (70.0, "UNIT-A"), "INV-Y": (30.0, "UNIT-A")}


def test_supplement_chain_and_historical_snapshots(client, admin):
    dossier = _bootstrap_dossier(client, admin)
    world = _bootstrap_world(client, admin)
    baseline = _baseline(client, admin, dossier["id"], world)
    supplement = _submit(
        client,
        admin,
        dossier["id"],
        kind="supplement",
        parent_agreement_id=baseline["id"],
        effective_from=T1,
        reason="补充协议：李四贡献上调",
        entries=_entries(world, ("x", "a", 50), ("y", "a", 50)),
        signatures=_signatures(world, ["x", "y"], ["a"]),
    )
    assert supplement.status_code == 201, supplement.text
    assert supplement.json()["state"] == "effective"
    before = _as_of(client, admin, dossier["id"], T0_LATER)
    assert before["agreement"]["id"] == baseline["id"]
    assert _shares(before) == {"INV-X": (60.0, "UNIT-A"), "INV-Y": (40.0, "UNIT-A")}
    after = _as_of(client, admin, dossier["id"], T1_LATER)
    assert after["agreement"]["id"] == supplement.json()["id"]
    assert _shares(after) == {"INV-X": (50.0, "UNIT-A"), "INV-Y": (50.0, "UNIT-A")}
    events = client.get(f"/api/ownership/dossiers/{dossier['id']}/ownership/events", headers=admin["headers"]).json()
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    previous = "GENESIS"
    for event in events:
        canonical = json.dumps(
            {
                "agreement_id": event["agreement_id"],
                "dossier_id": event["dossier_id"],
                "event_type": event["event_type"],
                "occurred_at": event["occurred_at"],
                "payload": event["payload"],
                "seq": event["seq"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        assert event["event_digest"] == digest
        assert event["chain_digest"] == hashlib.sha256(f"{previous}:{digest}".encode("utf-8")).hexdigest()
        previous = event["chain_digest"]


def test_references_pin_the_effective_snapshot(client, admin):
    dossier = _bootstrap_dossier(client, admin)
    world = _bootstrap_world(client, admin)
    baseline = _baseline(client, admin, dossier["id"], world)
    supplement = _submit(
        client,
        admin,
        dossier["id"],
        kind="supplement",
        effective_from=T1,
        reason="补充协议",
        entries=_entries(world, ("x", "a", 50), ("y", "a", 50)),
        signatures=_signatures(world, ["x", "y"], ["a"]),
    ).json()
    version = client.post(
        f"/api/ownership/dossiers/{dossier['id']}/references",
        headers=admin["headers"],
        json={"ref_type": "disclosure_version", "ref_code": "DV-2020-01", "as_of": T0_LATER},
    )
    assert version.status_code == 201, version.text
    assert version.json()["agreement_id"] == baseline["id"]
    family = client.post(
        f"/api/ownership/dossiers/{dossier['id']}/references",
        headers=admin["headers"],
        json={"ref_type": "patent_family", "ref_code": "PF-CN-01"},
    )
    assert family.status_code == 201, family.text
    assert family.json()["agreement_id"] == supplement["id"]
    disclosure = client.post(
        f"/api/ownership/dossiers/{dossier['id']}/references",
        headers=admin["headers"],
        json={"ref_type": "external_disclosure", "ref_code": "EXT-2021-01", "as_of": T1_LATER},
    )
    assert disclosure.status_code == 201
    assert disclosure.json()["agreement_id"] == supplement["id"]
    replay = client.post(
        f"/api/ownership/dossiers/{dossier['id']}/references",
        headers=admin["headers"],
        json={"ref_type": "disclosure_version", "ref_code": "DV-2020-01", "as_of": T0_LATER},
    )
    assert replay.status_code == 201
    assert replay.json()["replayed"] is True
    conflict = client.post(
        f"/api/ownership/dossiers/{dossier['id']}/references",
        headers=admin["headers"],
        json={"ref_type": "disclosure_version", "ref_code": "DV-2020-01", "as_of": T1_LATER},
    )
    assert conflict.status_code == 409
    references = client.get(f"/api/ownership/dossiers/{dossier['id']}/references", headers=admin["headers"]).json()
    assert {(item["ref_type"], item["ref_code"]): item["agreement_id"] for item in references} == {
        ("disclosure_version", "DV-2020-01"): baseline["id"],
        ("patent_family", "PF-CN-01"): supplement["id"],
        ("external_disclosure", "EXT-2021-01"): supplement["id"],
    }
    # 后续协议生效后，已钉住的引用仍指向当时的协议与快照
    _submit(
        client,
        admin,
        dossier["id"],
        kind="assignment",
        effective_from="2022-01-01T00:00:00+00:00",
        reason="再次调整",
        entries=_entries(world, ("x", "a", 90), ("y", "a", 10)),
        signatures=_signatures(world, ["x", "y"], ["a"]),
    )
    references = client.get(f"/api/ownership/dossiers/{dossier['id']}/references", headers=admin["headers"]).json()
    pinned = {item["ref_code"]: item["agreement_id"] for item in references}
    assert pinned["DV-2020-01"] == baseline["id"]
    detail = client.get(f"/api/ownership/agreements/{baseline['id']}", headers=admin["headers"]).json()
    assert {row["inventor_code"]: row["share_percent"] for row in detail["snapshot"]} == {"INV-X": 60.0, "INV-Y": 40.0}


def test_rejection_keeps_current_ownership(client, admin):
    dossier = _bootstrap_dossier(client, admin)
    world = _bootstrap_world(client, admin)
    baseline = _baseline(client, admin, dossier["id"], world)
    approver_one = _approver(client, admin, "approver.one")
    assignment = _submit(
        client,
        admin,
        dossier["id"],
        kind="assignment",
        effective_from=T1,
        reason="跨单位转让",
        entries=_entries(world, ("x", "b", 50), ("z", "b", 50)),
        signatures=_signatures(world, ["x", "z"], ["b"]),
    ).json()
    assert assignment["state"] == "pending_review"
    rejected = client.post(
        f"/api/ownership/agreements/{assignment['id']}/decisions",
        headers=approver_one["headers"],
        json={"decision": "reject", "comment": "份额与合同不符"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["state"] == "rejected"
    assert _current(client, admin, dossier["id"])["agreement"]["id"] == baseline["id"]
    closed = client.post(
        f"/api/ownership/agreements/{assignment['id']}/decisions",
        headers=approver_one["headers"],
        json={"decision": "approve"},
    )
    assert closed.status_code == 409


def test_agreement_submission_guards(client, admin):
    dossier = _bootstrap_dossier(client, admin)
    world = _bootstrap_world(client, admin)
    not_baseline = _submit(
        client,
        admin,
        dossier["id"],
        kind="assignment",
        entries=_entries(world, ("x", "a", 100)),
        signatures=_signatures(world, ["x"], ["a"]),
    )
    assert not_baseline.status_code == 422
    baseline = _baseline(client, admin, dossier["id"], world)
    second_baseline = _submit(
        client,
        admin,
        dossier["id"],
        entries=_entries(world, ("x", "a", 100)),
        signatures=_signatures(world, ["x"], ["a"]),
    )
    assert second_baseline.status_code == 409
    bad_window = _submit(
        client,
        admin,
        dossier["id"],
        kind="assignment",
        effective_from=T1,
        effective_until=T0,
        entries=_entries(world, ("x", "a", 100)),
        signatures=_signatures(world, ["x"], ["a"]),
    )
    assert bad_window.status_code == 422
    scheduled = _submit(
        client,
        admin,
        dossier["id"],
        kind="assignment",
        effective_from=FUTURE,
        entries=_entries(world, ("x", "a", 100)),
        signatures=_signatures(world, ["x"], ["a"]),
    )
    assert scheduled.status_code == 201
    blocked = _submit(
        client,
        admin,
        dossier["id"],
        kind="assignment",
        effective_from=FUTURE,
        entries=_entries(world, ("x", "a", 90), ("y", "a", 10)),
        signatures=_signatures(world, ["x", "y"], ["a"]),
    )
    assert blocked.status_code == 409
    withdrawn = client.post(
        f"/api/ownership/agreements/{scheduled.json()['id']}/withdraw",
        headers=admin["headers"],
        json={"reason": "撤回预约协议"},
    )
    assert withdrawn.status_code == 201
    unblocked = _submit(
        client,
        admin,
        dossier["id"],
        kind="assignment",
        effective_from=T1,
        entries=_entries(world, ("x", "a", 90), ("y", "a", 10)),
        signatures=_signatures(world, ["x", "y"], ["a"]),
    )
    assert unblocked.status_code == 201
    assert unblocked.json()["state"] == "effective"
    assert _current(client, admin, dossier["id"])["agreement"]["id"] != baseline["id"]


def test_agreement_validity_period_lapses(client, admin):
    dossier = _bootstrap_dossier(client, admin)
    world = _bootstrap_world(client, admin)
    limited = _submit(
        client,
        admin,
        dossier["id"],
        effective_until=T1,
        entries=_entries(world, ("x", "a", 100)),
        signatures=_signatures(world, ["x"], ["a"]),
    )
    assert limited.status_code == 201, limited.text
    within = _as_of(client, admin, dossier["id"], T0_LATER)
    assert within["state"] == "effective"
    assert _shares(within) == {"INV-X": (100.0, "UNIT-A")}
    lapsed = _current(client, admin, dossier["id"])
    assert lapsed["state"] == "none"
    assert lapsed["entries"] == []
    renewed = _submit(
        client,
        admin,
        dossier["id"],
        entries=_entries(world, ("x", "a", 60), ("y", "a", 40)),
        signatures=_signatures(world, ["x", "y"], ["a"]),
    )
    assert renewed.status_code == 201, renewed.text
    assert renewed.json()["state"] == "effective"
    assert _current(client, admin, dossier["id"])["complete"] is True


def test_ownership_permissions_enforced(client, admin):
    dossier = _bootstrap_dossier(client, admin)
    world = _bootstrap_world(client, admin)
    _baseline(client, admin, dossier["id"], world)
    outsider = _create_user(client, admin, "outsider", [])
    outsider_headers = {"Authorization": f"Bearer {client.post('/api/auth/login', json={'username': 'outsider', 'password': 'Review!23456', 'client_label': 'tests'}).json()['token']}"}
    read_denied = client.get(f"/api/ownership/dossiers/{dossier['id']}/ownership", headers=outsider_headers)
    assert read_denied.status_code == 403
    write_denied = client.post(
        "/api/ownership/units",
        headers=outsider_headers,
        json={"unit_code": "UNIT-C", "name": "丙公司"},
    )
    assert write_denied.status_code == 403
    decide_denied = client.post(
        "/api/ownership/agreements/1/decisions",
        headers=outsider_headers,
        json={"decision": "approve"},
    )
    assert decide_denied.status_code == 403
