from enum import StrEnum

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    NEED_MORE_PEERS = "need_more_peers"
    PERIOD_MISMATCH = "period_mismatch"
    MISSING_FIELD = "missing_field"
    PROVIDER_FAILED = "provider_failed"
    INVALID_INPUT = "invalid_input"

class ToolError(BaseModel):
    code: ErrorCode
    message: str
    retryable: bool = False


class ToolResult(BaseModel):
    ok: bool
    data: dict | None = None
    error: ToolError | None = None
    datapoint_ids: list[str] = Field(default_factory=list)