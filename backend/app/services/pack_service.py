"""装袋两段式：先试算（只读，不写库）再提交（单事务，失败整体回滚）。"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.models import BagItem, DeliveryRoute, PackBag, RejectRecord, SubscriberStop
from app.services.pack_engine import pack_route, StopItem


class RouteNotFoundError(LookupError):
    """路线不存在。"""


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
    """试算结果：拟开袋与拟拒收，仅内存对象，未落库。"""

    route_id: int
    bags: list[PlannedBag] = field(default_factory=list)
    rejects: list[PlannedReject] = field(default_factory=list)


def build_pack_plan(db: Session, route_id: int) -> PackPlan:
    """试算：按路线限额与订户 seq 算出拟开袋与拟拒收。只读，不写库。"""
    route = db.get(DeliveryRoute, route_id)
    if not route:
        raise RouteNotFoundError("路线不存在")
    stops = db.scalars(
        select(SubscriberStop).where(SubscriberStop.route_id == route.id).order_by(SubscriberStop.seq)
    ).all()
    items = [StopItem(s.id, s.seq, s.weight_kg, s.volume_l, s.name) for s in stops]
    result = pack_route(items, route.max_weight_kg, route.max_volume_l)
    return PackPlan(
        route_id=route.id,
        bags=[
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
        ],
        rejects=[
            PlannedReject(stop_id=stop.stop_id, stop_name=stop.label, reason=reason)
            for stop, reason in result.rejects
        ],
    )


def commit_pack_plan(db: Session, plan: PackPlan) -> list[PackBag]:
    """提交：按计划写入袋明细与拒收。

    清旧账与写新账在同一个事务里；任何一步失败都整体回滚，
    不会留下半截袋或半截拒收。
    """
    try:
        old_bags = db.scalars(select(PackBag).where(PackBag.route_id == plan.route_id)).all()
        for b in old_bags:
            for it in list(b.items):
                db.delete(it)
            db.delete(b)
        old_rejects = db.scalars(
            select(RejectRecord).where(RejectRecord.route_id == plan.route_id)
        ).all()
        for r in old_rejects:
            db.delete(r)
        db.flush()

        out_bags: list[PackBag] = []
        for bag in plan.bags:
            row = PackBag(
                route_id=plan.route_id,
                bag_index=bag.bag_index,
                weight_kg=bag.weight_kg,
                volume_l=bag.volume_l,
            )
            db.add(row)
            db.flush()
            for it in bag.items:
                db.add(
                    BagItem(
                        bag_id=row.id,
                        stop_id=it.stop_id,
                        stop_name=it.stop_name,
                        weight_kg=it.weight_kg,
                        volume_l=it.volume_l,
                    )
                )
            out_bags.append(row)
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
    except Exception:
        db.rollback()
        raise
    return out_bags
