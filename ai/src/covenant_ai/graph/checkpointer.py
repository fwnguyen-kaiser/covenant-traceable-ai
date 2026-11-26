"""Durable PostgreSQL LangGraph checkpointer factory with explicit msgpack allowlist."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from covenant_ai.providers.db_schema import normalize_db_url

COVENANT_MSGPACK_ALLOWLIST: tuple[tuple[str, str], ...] = (
    ("covenant_ai.domain.models", "SourceType"),
    ("covenant_ai.domain.models", "FinancialDatapoint"),
    ("covenant_ai.domain.models", "AdjustmentCategory"),
    ("covenant_ai.domain.models", "AdjustmentLineItem"),
    ("covenant_ai.domain.models", "EbitdaAdjustmentSchedule"),
    ("covenant_ai.domain.models", "CompanyProfile"),
    ("covenant_ai.domain.models", "PeerFinancials"),
    ("covenant_ai.domain.business_profile", "BusinessDimension"),
    ("covenant_ai.domain.business_profile", "BusinessProfile"),
)


def build_checkpointer_serde() -> JsonPlusSerializer:
    """Construct strict-compatible serializer allowing Covenant domain models."""
    return JsonPlusSerializer(allowed_msgpack_modules=COVENANT_MSGPACK_ALLOWLIST)


@contextmanager
def open_postgres_checkpointer(
    db_url: str | None = None,
    *,
    min_size: int = 1,
    max_size: int = 5,
    setup: bool = True,
) -> Iterator[PostgresSaver]:
    """Yield an initialized PostgresSaver backed by a psycopg3 ConnectionPool."""
    conn_str = normalize_db_url(db_url)
    with ConnectionPool(
        conninfo=conn_str,
        min_size=min_size,
        max_size=max_size,
        kwargs={"autocommit": True, "row_factory": dict_row},
    ) as pool:
        saver = PostgresSaver(pool, serde=build_checkpointer_serde())
        if setup:
            saver.setup()
        yield saver
