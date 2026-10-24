from typing import Literal

from app.schemas.common import ApiModel


class HealthOut(ApiModel):
    status: Literal["ok", "degraded"]
    version: str
    database: Literal["ok", "unavailable", "not_configured"]


class LivenessOut(ApiModel):
    status: Literal["ok"]
    version: str
