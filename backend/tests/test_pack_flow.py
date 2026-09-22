"""Two-phase packing flow tests: read-only preview, atomic commit."""

import pytest
from sqlalchemy import func, select

from app.models.models import BagItem, PackBag, RejectRecord
from app.services.pack_service import build_plan, commit_plan


def _count(db, model, route_id: int) -> int:
    return db.scalar(
        select(func.count())
        .select_from(model)
        .where(model.route_id == route_id)
    )


def _db_snapshot(db, route_id: int):
    bags = db.scalars(
        select(PackBag)
        .where(PackBag.route_id == route_id)
        .order_by(PackBag.bag_index)
    ).all()
    bag_data = []
    item_total = 0
    for bag in bags:
        items = db.scalars(
            select(BagItem).where(BagItem.bag_id == bag.id).order_by(BagItem.id)
        ).all()
        item_total += len(items)
        bag_data.append(
            {
                "bag_index": bag.bag_index,
                "weight_kg": bag.weight_kg,
                "volume_l": bag.volume_l,
                "stop_ids": [i.stop_id for i in items],
            }
        )
    rejects = db.scalars(
        select(RejectRecord)
        .where(RejectRecord.route_id == route_id)
        .order_by(RejectRecord.id)
    ).all()
    reject_data = [(r.stop_id, r.reason) for r in rejects]
    return bag_data, item_total, reject_data


def _plan_snapshot(plan):
    bag_data = [
        {
            "bag_index": b.bag_index,
            "weight_kg": b.weight_kg,
            "volume_l": b.volume_l,
            "stop_ids": [i.stop_id for i in b.items],
        }
        for b in plan.bags
    ]
    item_total = sum(len(b["stop_ids"]) for b in bag_data)
    reject_data = [(r.stop_id, r.reason) for r in plan.rejects]
    return bag_data, item_total, reject_data


def test_preview_writes_no_bags_or_rejects(client, db):
    # Seeded route 1 has no pack results yet; preview must keep it that way.
    assert _count(db, PackBag, 1) == 0
    assert _count(db, RejectRecord, 1) == 0

    resp = client.post("/api/pack/preview", json={"route_id": 1})
    assert resp.status_code == 200
    plan = resp.json()
    assert plan["route_id"] == 1
    assert plan["bags"], "preview should still report the intended bags"

    db.expire_all()
    assert _count(db, PackBag, 1) == 0
    assert db.scalar(select(func.count()).select_from(BagItem)) == 0
    assert _count(db, RejectRecord, 1) == 0


def test_commit_writes_bags_and_rejects_matching_plan(client, db):
    plan_resp = client.post("/api/pack/preview", json={"route_id": 1})
    assert plan_resp.status_code == 200
    payload = plan_resp.json()

    resp = client.post("/api/pack", json={"route_id": 1})
    assert resp.status_code == 200
    committed = resp.json()
    assert [b["bag_index"] for b in committed] == [b["bag_index"] for b in payload["bags"]]
    for actual, planned in zip(committed, payload["bags"]):
        assert actual["weight_kg"] == planned["weight_kg"]
        assert actual["volume_l"] == planned["volume_l"]
        assert [i["stop_id"] for i in actual["items"]] == [
            i["stop_id"] for i in planned["items"]
        ]

    # The persisted rows must match the trial plan exactly.
    db.expire_all()
    plan = build_plan(db, 1)
    assert _db_snapshot(db, 1) == _plan_snapshot(plan)

    bag_data, item_total, reject_data = _db_snapshot(db, 1)
    assert [b["stop_ids"] for b in bag_data] == [[1, 2, 3], [5]]
    assert item_total == 4
    assert len(reject_data) == 1
    assert reject_data[0][0] == 4
    assert "超重" in reject_data[0][1]


def test_interrupted_commit_leaves_zero_bags(db, monkeypatch):
    route_id = 1
    plan = build_plan(db, route_id)
    assert plan.bags and plan.rejects

    # Fail right after the first new bag has been flushed: a half bag is
    # sitting inside the transaction when the outage hits.
    real_flush = db.flush
    flushes = {"n": 0}

    def fail_mid_commit(*args, **kwargs):
        result = real_flush(*args, **kwargs)
        flushes["n"] += 1
        if flushes["n"] == 2:  # 1 = old-data cleanup, 2 = first bag insert
            raise RuntimeError("simulated outage mid-commit")
        return result

    monkeypatch.setattr(db, "flush", fail_mid_commit)
    with pytest.raises(RuntimeError, match="simulated outage"):
        commit_plan(db, plan)
    monkeypatch.undo()

    assert _count(db, PackBag, route_id) == 0
    assert db.scalar(select(func.count()).select_from(BagItem)) == 0
    assert _count(db, RejectRecord, route_id) == 0

    # Session is still usable: a later full commit succeeds.
    commit_plan(db, build_plan(db, route_id))
    assert _count(db, PackBag, route_id) == len(plan.bags)
    assert _count(db, RejectRecord, route_id) == len(plan.rejects)


def test_pack_endpoint_rolls_back_when_commit_fails(client, db, monkeypatch):
    # The single external entry point must also leave no half pack behind.
    from fastapi.testclient import TestClient

    import app.api.router as router_mod

    real_commit = router_mod.commit_plan

    def crashing_commit(session, plan):
        real_flush = session.flush
        flushes = {"n": 0}

        def fail_mid_commit(*args, **kwargs):
            result = real_flush(*args, **kwargs)
            flushes["n"] += 1
            if flushes["n"] == 2:  # after the first bag insert is flushed
                raise RuntimeError("boom during commit")
            return result

        session.flush = fail_mid_commit
        try:
            return real_commit(session, plan)
        finally:
            session.flush = real_flush

    monkeypatch.setattr(router_mod, "commit_plan", crashing_commit)
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as quiet:
        resp = quiet.post("/api/pack", json={"route_id": 1})
    assert resp.status_code == 500

    # The request session rolled back; verify on the shared in-memory database
    # that the route shows zero rows of any kind.
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(PackBag)) == 0
    assert db.scalar(select(func.count()).select_from(BagItem)) == 0
    assert db.scalar(select(func.count()).select_from(RejectRecord)) == 0
