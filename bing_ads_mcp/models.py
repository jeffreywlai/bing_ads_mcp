"""Small public input models; Microsoft remains the authority on API business rules."""

import re
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def identifier(value: object) -> str:
    """Keep 64-bit IDs exact across JSON clients; never coerce floats or booleans."""
    if type(value) not in (str, int) or not re.fullmatch(r"[1-9][0-9]{0,18}", str(value)):
        raise ValueError("Use a positive decimal ID, preferably as a string.")
    if int(value) > 2**63 - 1:
        raise ValueError("ID exceeds Microsoft's signed 64-bit range.")
    return str(value)


Id = Annotated[str, BeforeValidator(identifier), Field(pattern=r"^[1-9][0-9]{0,18}$")]
Amount = Annotated[float, Field(gt=0, allow_inf_nan=False, strict=True)]
Name = Annotated[str, Field(min_length=1, max_length=128, pattern=r"\S")]
Status = Literal["Active", "Paused"]
CampaignType = Literal[
    "Search", "Shopping", "Audience", "Hotel", "PerformanceMax", "App", "ObjectiveBased"
]
PageSize = Annotated[int, Field(ge=1, le=500, strict=True)]
Offset = Annotated[int, Field(ge=0, strict=True)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scope(Input):
    """The manager and advertiser account. IDs are required on every scoped call."""

    customer_id: Id
    account_id: Id


class KeywordChange(Input):
    id: Id
    status: Status | None = None
    bid: Amount | None = None

    @model_validator(mode="after")
    def has_change(self):
        if self.status is None and self.bid is None:
            raise ValueError("Set status or bid for every keyword change.")
        return self


class ReportSpec(Input):
    report_type: Literal["account", "campaign", "ad_group", "ad", "keyword", "search_query"]
    start_date: date
    end_date: date
    # Microsoft enum name, not an IANA zone. Required: never silently assume a zone.
    time_zone: Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9]+$")]
    aggregation: Literal["Summary", "Daily"] = "Daily"
    columns: Annotated[list[str], Field(min_length=1, max_length=100)] | None = None
    return_only_complete_data: bool = True

    @model_validator(mode="after")
    def validate_window(self):
        if self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date (both are inclusive).")
        if self.columns is not None:
            if len(set(self.columns)) != len(self.columns):
                raise ValueError("Report columns must be unique.")
            if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", x) for x in self.columns):
                raise ValueError("Use Microsoft's report column names, such as Spend.")
            if self.aggregation != "Summary" and "TimePeriod" not in self.columns:
                raise ValueError("Daily reports require TimePeriod to preserve daily granularity.")
            if self.aggregation == "Summary" and "TimePeriod" in self.columns:
                raise ValueError("Omit TimePeriod when requesting Summary aggregation.")
        return self
