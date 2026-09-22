from datetime import datetime
from pydantic import BaseModel


class RouteOut(BaseModel):
    id: int
    name: str
    max_weight_kg: float
    max_volume_l: float
    model_config = {"from_attributes": True}


class StopOut(BaseModel):
    id: int
    route_id: int
    seq: int
    name: str
    weight_kg: float
    volume_l: float
    model_config = {"from_attributes": True}


class BagItemOut(BaseModel):
    stop_id: int
    stop_name: str
    weight_kg: float
    volume_l: float


class BagOut(BaseModel):
    id: int
    route_id: int
    bag_index: int
    weight_kg: float
    volume_l: float
    items: list[BagItemOut] = []
    model_config = {"from_attributes": True}


class RejectOut(BaseModel):
    id: int
    route_id: int
    stop_id: int
    stop_name: str
    reason: str
    created_at: datetime
    model_config = {"from_attributes": True}


class PackRequest(BaseModel):
    route_id: int


class PlannedItemOut(BaseModel):
    stop_id: int
    stop_name: str
    weight_kg: float
    volume_l: float


class PlannedBagOut(BaseModel):
    bag_index: int
    weight_kg: float
    volume_l: float
    items: list[PlannedItemOut] = []


class PlannedRejectOut(BaseModel):
    stop_id: int
    stop_name: str
    reason: str


class PackPlanOut(BaseModel):
    """试算结果：拟开袋与拟拒收，仅供核对，未写库。"""

    route_id: int
    bags: list[PlannedBagOut] = []
    rejects: list[PlannedRejectOut] = []


class WeightOut(BaseModel):
    bag_id: int
    bag_index: int
    route_id: int
    weight_kg: float
    volume_l: float
    fill_weight_pct: float
    fill_volume_pct: float
