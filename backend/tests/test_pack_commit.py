"""两段式装袋：试算不写库；提交与计划一致；提交中断整体回滚。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.router import api_router
from app.database import Base, get_db
from app.models.models import (
    BagItem,
    DeliveryRoute,
    PackBag,
    RejectRecord,
    SubscriberStop,
)
from app.services.pack_service import build_pack_plan, commit_pack_plan


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    route = DeliveryRoute(name="测试线", max_weight_kg=4.0, max_volume_l=10.0)
    session.add(route)
    session.flush()
    session.add_all(
        [
            SubscriberStop(route_id=route.id, seq=1, name="甲", weight_kg=2.0, volume_l=3.0),
            SubscriberStop(route_id=route.id, seq=2, name="乙", weight_kg=2.5, volume_l=3.0),
            SubscriberStop(route_id=route.id, seq=3, name="丙", weight_kg=1.0, volume_l=1.0),
            SubscriberStop(route_id=route.id, seq=4, name="超大件", weight_kg=9.0, volume_l=1.0),
        ]
    )
    session.commit()
    yield session, route.id
    session.close()


@pytest.fixture()
def client(db):
    session, _ = db

    def override_get_db():
        yield session

    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def _count(session, model) -> int:
    return session.scalar(select(func.count(model.id)))


def test_plan_endpoint_writes_nothing(client, db):
    """试算后库中无新袋（也无新拒收）。"""
    session, route_id = db
    resp = client.post("/api/pack/plan", json={"route_id": route_id})
    assert resp.status_code == 200
    plan = resp.json()
    assert len(plan["bags"]) == 2
    assert [it["stop_name"] for b in plan["bags"] for it in b["items"]] == ["甲", "乙", "丙"]
    assert [r["stop_name"] for r in plan["rejects"]] == ["超大件"]

    assert _count(session, PackBag) == 0
    assert _count(session, BagItem) == 0
    assert _count(session, RejectRecord) == 0


def test_plan_unknown_route_404(client):
    resp = client.post("/api/pack/plan", json={"route_id": 9999})
    assert resp.status_code == 404


def test_commit_matches_plan(client, db):
    """提交后库中袋与拒收和试算计划一致。"""
    session, route_id = db
    plan = client.post("/api/pack/plan", json={"route_id": route_id}).json()

    resp = client.post("/api/pack", json={"route_id": route_id})
    assert resp.status_code == 200
    assert len(resp.json()) == len(plan["bags"])

    bags = session.scalars(
        select(PackBag).where(PackBag.route_id == route_id).order_by(PackBag.bag_index)
    ).all()
    assert len(bags) == len(plan["bags"])
    for row, planned in zip(bags, plan["bags"]):
        assert row.bag_index == planned["bag_index"]
        assert row.weight_kg == pytest.approx(planned["weight_kg"])
        assert row.volume_l == pytest.approx(planned["volume_l"])
        items = session.scalars(select(BagItem).where(BagItem.bag_id == row.id)).all()
        assert [i.stop_id for i in items] == [i["stop_id"] for i in planned["items"]]
        assert [i.stop_name for i in items] == [i["stop_name"] for i in planned["items"]]

    rejects = session.scalars(
        select(RejectRecord).where(RejectRecord.route_id == route_id)
    ).all()
    assert len(rejects) == len(plan["rejects"])
    assert {(r.stop_id, r.stop_name, r.reason) for r in rejects} == {
        (r["stop_id"], r["stop_name"], r["reason"]) for r in plan["rejects"]
    }


def test_commit_interruption_leaves_no_partial_rows(db, monkeypatch):
    """模拟提交中断：袋表、袋明细、拒收表全部为零，不留半截。"""
    session, route_id = db
    plan = build_pack_plan(session, route_id)

    def boom():
        raise RuntimeError("模拟提交中断")

    monkeypatch.setattr(session, "commit", boom)
    with pytest.raises(RuntimeError, match="模拟提交中断"):
        commit_pack_plan(session, plan)
    monkeypatch.undo()

    assert _count(session, PackBag) == 0
    assert _count(session, BagItem) == 0
    assert _count(session, RejectRecord) == 0


def test_failed_commit_keeps_previous_pack(db, monkeypatch):
    """已有旧装袋结果时提交中断：旧袋旧拒收原样保留，不剩半截。"""
    session, route_id = db
    commit_pack_plan(session, build_pack_plan(session, route_id))
    old_bag_ids = session.scalars(select(PackBag.id)).all()
    old_reject_ids = session.scalars(select(RejectRecord.id)).all()
    assert old_bag_ids and old_reject_ids

    def boom():
        raise RuntimeError("模拟提交中断")

    monkeypatch.setattr(session, "commit", boom)
    with pytest.raises(RuntimeError, match="模拟提交中断"):
        commit_pack_plan(session, build_pack_plan(session, route_id))
    monkeypatch.undo()

    assert session.scalars(select(PackBag.id).order_by(PackBag.id)).all() == old_bag_ids
    assert session.scalars(select(RejectRecord.id).order_by(RejectRecord.id)).all() == old_reject_ids
