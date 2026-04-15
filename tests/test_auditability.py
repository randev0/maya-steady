from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from database.dal import Database


class _AcquireContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireContext(self._conn)


@pytest.mark.asyncio
async def test_update_facts_records_state_transition_for_tracked_fields():
    user_id = uuid4()
    conversation_id = uuid4()

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"facts": {"qualification_stage": "qualifying", "lead_status": "active"}},
            {"facts": {"qualification_stage": "handoff", "lead_status": "handed_off"}},
        ]
    )
    conn.execute = AsyncMock()

    original_pool = Database.pool
    Database.pool = _FakePool(conn)
    try:
        await Database.update_facts(
            user_id=user_id,
            new_facts={"qualification_stage": "handoff", "lead_status": "handed_off"},
            changed_by="policy",
            conversation_id=conversation_id,
        )
    finally:
        Database.pool = original_pool

    executed_sql = " ".join(call.args[0] for call in conn.execute.await_args_list)
    assert "INSERT INTO state_transitions" in executed_sql
