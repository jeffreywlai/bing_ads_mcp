"""MCP inputs; provider request bodies retain Microsoft's native JSON shape."""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def identifier(value: object) -> str:
    if type(value) not in (str, int) or not re.fullmatch(r"[1-9][0-9]{0,18}", str(value)):
        raise ValueError("Use a positive decimal ID, preferably as a string.")
    if int(value) > 2**63 - 1:
        raise ValueError("ID exceeds Microsoft's signed 64-bit range.")
    return str(value)


Id = Annotated[str, BeforeValidator(identifier), Field(pattern=r"^[1-9][0-9]{0,18}$")]
PageSize = Annotated[int, Field(ge=1, le=500, strict=True)]
Service = Literal["campaign", "ad_insight", "reporting", "bulk", "customer", "billing"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scope(Input):
    """Explicit manager/customer and advertiser account IDs; never global mutable state."""

    customer_id: Id
    account_id: Id


class EntityQuery(Input):
    """AND filters use Microsoft's IsEqualTo/Contains/etc. Performance requires a timezone."""

    filters: dict[str, dict] = Field(default_factory=dict)
    sort_by: str | None = None
    descending: bool = False
    page_size: PageSize = 100
    next_page_token: str | None = None
    performance_time: dict | None = None
    return_only_complete_data: bool = True
    performance_report_ids: dict[str, str] = Field(default_factory=dict)
