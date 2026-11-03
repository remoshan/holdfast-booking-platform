"""Base classes and field types shared by the request and response schemas."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints


class RequestSchema(BaseModel):
    # Unknown fields are rejected, so a typo in a field name is reported instead of ignored.
    model_config = ConfigDict(extra="forbid")


class ResponseSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PeriodResponse(ResponseSchema):
    start: datetime
    end: datetime


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
