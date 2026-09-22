"""Two-phase packing: a read-only trial run (plan) and an atomic commit.

The packing rule itself lives in :mod:`app.services.pack_engine` and is not
changed here. ``build_plan`` only reads stops and returns a plan without ever
flushing or adding rows; ``commit_plan`` writes bags, bag items and rejects in
a single transaction and rolls the whole route back on any failure, so an
interrupted commit never leaves half-written bags or rejects.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.models import BagItem, DeliveryRoute, PackBag, RejectRecord, SubscriberStop
from app.services.pack_engine import StopItem, pack_route


@dataclass(frozen=True)
class PlannedItem:
    stop_id: int
    stop_name: str
    weight_kg: float
    volume_l: float


@dataclass(frozen=True)
class PlannedBag:
    bag_index: int
    weight_kg: float
    volume_l: float
    items: list[PlannedItem] = field(default_factory=list)


@dataclass(frozen=True)
class PlannedReject:
    stop_id: int
    stop_name: str
    reason: str


@dataclass(frozen=True)
class PackPlan:
    route_id: int
    bags: list[PlannedBag]
    rejects: list[PlannedReject]


def build_plan(db: Session, route_id: int) -> PackPlan:
    """Trial run: compute intended bags and rejects without writing anything."""
    route = db.get(DeliveryRoute, route_id)
    if not route:
        raise LookupError("路线不存在")

    stops = db.scalars(
        select(SubscriberStop)
        .where(SubscriberStop.route_id == route_id)
        .order_by(SubscriberStop.seq)
    ).all()
    items = [StopItem(s.id, s.seq, s.weight_kg, s.volume_l, s.name) for s in stops]
    result = pack_route(items, route.max_weight_kg, route.max_volume_l)

    plan_bags = [
        PlannedBag(
            bag_index=bag.bag_index,
            weight_kg=round(bag.weight_kg, 3),
            volume_l=round(bag.volume_l, 3),
            items=[
                PlannedItem(
                    stop_id=it.stop_id,
                    stop_name=it.label,
                    weight_kg=it.weight_kg,
                    volume_l=it.volume_l,
                )
                for it in bag.items
            ],
        )
        for bag in result.bags
    ]
    plan_rejects = [
        PlannedReject(stop_id=stop.stop_id, stop_name=stop.label, reason=reason)
        for stop, reason in result.rejects
    ]
    return PackPlan(route_id=route_id, bags=plan_bags, rejects=plan_rejects)


def commit_plan(db: Session, plan: PackPlan) -> list[PackBag]:
    """Persist a plan atomically, replacing any previous pack for the route.

    On failure the transaction is rolled back so neither partial bags nor
    partial rejects remain. Returns the committed ``PackBag`` rows.
    """
    try:
        old_bags = db.scalars(
            select(PackBag).where(PackBag.route_id == plan.route_id)
        ).all()
        for bag in old_bags:
            for it in list(bag.items):
                db.delete(it)
            db.delete(bag)
        old_rej = db.scalars(
            select(RejectRecord).where(RejectRecord.route_id == plan.route_id)
        ).all()
        for rej in old_rej:
            db.delete(rej)
        db.flush()

        rows: list[PackBag] = []
        for planned in plan.bags:
            row = PackBag(
                route_id=plan.route_id,
                bag_index=planned.bag_index,
                weight_kg=planned.weight_kg,
                volume_l=planned.volume_l,
            )
            db.add(row)
            db.flush()
            for it in planned.items:
                db.add(
                    BagItem(
                        bag_id=row.id,
                        stop_id=it.stop_id,
                        stop_name=it.stop_name,
                        weight_kg=it.weight_kg,
                        volume_l=it.volume_l,
                    )
                )
            rows.append(row)
        for rej in plan.rejects:
            db.add(
                RejectRecord(
                    route_id=plan.route_id,
                    stop_id=rej.stop_id,
                    stop_name=rej.stop_name,
                    reason=rej.reason,
                )
            )
        db.commit()
        return rows
    except Exception:
        db.rollback()
        raise
