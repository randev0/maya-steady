"""
Data Access Layer — all database operations for LeadQualBot.
Uses asyncpg for async PostgreSQL access.
"""
import json
import asyncpg
from typing import Optional, List
from uuid import UUID

from lead_state import normalize_facts, normalize_lead_state_update
from whatsapp_identity import normalize_external_id


def _parse_jsonb(value) -> dict:
    """Safely parse a JSONB value that may come back as dict or JSON string."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


class Database:
    pool: Optional[asyncpg.Pool] = None
    TRACKED_STATE_FIELDS = {
        "intent_stage",
        "qualification_stage",
        "lead_status",
        "human_handoff_requested",
        "handoff_reason",
        "follow_up_stage",
        "follow_up_count",
        "next_follow_up_at",
        "opt_out",
        "budget_band",
        "timeline_band",
        "message_volume_band",
        "current_process",
        "service_interest",
        "business_type",
        "pain_point",
    }

    @classmethod
    async def connect(cls, dsn: str) -> None:
        cls.pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)

    @classmethod
    async def disconnect(cls) -> None:
        if cls.pool:
            await cls.pool.close()
            cls.pool = None

    @classmethod
    async def apply_schema(cls, schema_path: str) -> None:
        with open(schema_path, "r") as f:
            schema_sql = f.read()
        async with cls.pool.acquire() as conn:
            await conn.execute(schema_sql)

    # ------------------------------------------------------------------ #
    # Users
    # ------------------------------------------------------------------ #

    @classmethod
    async def get_or_create_user(cls, external_id: str, channel: str = "test") -> dict:
        normalized_external_id = normalize_external_id(channel, external_id)
        if not normalized_external_id:
            raise ValueError("external_id is required")
        async with cls.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO users (external_id, channel)
                VALUES ($1, $2)
                ON CONFLICT (external_id) DO UPDATE
                    SET channel = users.channel
                RETURNING *
                """,
                normalized_external_id,
                channel,
            )
            return dict(row)

    @classmethod
    async def get_user_by_external_id(cls, external_id: str) -> Optional[dict]:
        normalized_whatsapp = normalize_external_id("whatsapp", external_id)
        async with cls.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE external_id = $1",
                normalized_whatsapp or external_id,
            )
            return dict(row) if row else None

    @classmethod
    async def get_user_by_id(cls, user_id: UUID) -> Optional[dict]:
        async with cls.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
            return dict(row) if row else None

    @classmethod
    async def update_user_display_name(cls, user_id: UUID, display_name: str) -> None:
        async with cls.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET display_name = $1, updated_at = NOW() WHERE id = $2",
                display_name, user_id,
            )

    # ------------------------------------------------------------------ #
    # Conversations
    # ------------------------------------------------------------------ #

    @classmethod
    async def get_active_conversation(cls, user_id: UUID) -> Optional[dict]:
        async with cls.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM conversations
                WHERE user_id = $1 AND status = 'active'
                ORDER BY started_at DESC LIMIT 1
                """,
                user_id,
            )
            return dict(row) if row else None

    @classmethod
    async def create_conversation(cls, user_id: UUID) -> dict:
        async with cls.pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO conversations (user_id) VALUES ($1) RETURNING *", user_id
            )
            return dict(row)

    @classmethod
    async def update_conversation_status(cls, conversation_id: UUID, status: str) -> None:
        async with cls.pool.acquire() as conn:
            await conn.execute(
                "UPDATE conversations SET status = $1 WHERE id = $2",
                status, conversation_id,
            )

    # ------------------------------------------------------------------ #
    # Messages
    # ------------------------------------------------------------------ #

    @classmethod
    async def store_message(cls, conversation_id: UUID, role: str, content: str, source: str = 'maya') -> dict:
        async with cls.pool.acquire() as conn:
            await conn.execute(
                "UPDATE conversations SET last_message_at = NOW() WHERE id = $1",
                conversation_id,
            )
            row = await conn.fetchrow(
                """
                INSERT INTO messages (conversation_id, role, content, source)
                VALUES ($1, $2, $3, $4) RETURNING *
                """,
                conversation_id, role, content, source,
            )
            return dict(row)

    @classmethod
    async def get_conversation_history(cls, conversation_id: UUID, limit: int = 20) -> List[dict]:
        """Return conversation history excluding shadow messages (admin/customer_paused)."""
        async with cls.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT role, content FROM messages
                WHERE conversation_id = $1
                  AND source NOT IN ('admin', 'customer_paused')
                ORDER BY created_at DESC LIMIT $2
                """,
                conversation_id, limit,
            )
            return [dict(r) for r in reversed(rows)]

    # ------------------------------------------------------------------ #
    # Lead Profiles & Structured Facts
    # ------------------------------------------------------------------ #

    @classmethod
    async def get_or_create_profile(cls, user_id: UUID) -> dict:
        async with cls.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM lead_profiles WHERE user_id = $1", user_id
            )
            if not row:
                row = await conn.fetchrow(
                    "INSERT INTO lead_profiles (user_id) VALUES ($1) RETURNING *", user_id
                )
            result = dict(row)
            result["facts"] = normalize_facts(_parse_jsonb(result["facts"]))
            return result

    @classmethod
    async def update_facts(
        cls,
        user_id: UUID,
        new_facts: dict,
        changed_by: str = "agent",
        conversation_id: Optional[UUID] = None,
    ) -> dict:
        async with cls.pool.acquire() as conn:
            current = await conn.fetchrow(
                "SELECT facts FROM lead_profiles WHERE user_id = $1", user_id
            )
            current_facts = normalize_facts(_parse_jsonb(current["facts"] if current else None))
            normalized_updates = normalize_lead_state_update(new_facts)
            passthrough_updates = {
                key: value for key, value in (new_facts or {}).items()
                if key not in normalized_updates
            }
            merged_updates = {**passthrough_updates, **normalized_updates}

            # Audit each changed field
            for key, value in merged_updates.items():
                old_value = current_facts.get(key)
                if old_value != value:
                    await conn.execute(
                        """
                        INSERT INTO facts_audit (user_id, field_name, old_value, new_value, changed_by)
                        VALUES ($1, $2, $3, $4, $5)
                        """,
                        user_id,
                        key,
                        json.dumps(old_value),
                        json.dumps(value),
                        changed_by,
                    )
                    if key in cls.TRACKED_STATE_FIELDS:
                        await conn.execute(
                            """
                            INSERT INTO state_transitions
                                (user_id, conversation_id, field_name, old_value, new_value, changed_by)
                            VALUES ($1, $2, $3, $4, $5, $6)
                            """,
                            user_id,
                            conversation_id,
                            key,
                            json.dumps(old_value),
                            json.dumps(value),
                            changed_by,
                        )

            row = await conn.fetchrow(
                """
                INSERT INTO lead_profiles (user_id, facts)
                VALUES ($1, $2::jsonb)
                ON CONFLICT (user_id) DO UPDATE
                    SET facts = lead_profiles.facts || $2::jsonb,
                        updated_at = NOW()
                RETURNING *
                """,
                user_id, json.dumps(merged_updates),
            )
            result = dict(row)
            result["facts"] = normalize_facts(_parse_jsonb(result["facts"]))
            return result

    @classmethod
    async def get_user_facts(cls, user_id: UUID) -> dict:
        """Return the current facts dict for a user, or {} if no profile yet."""
        async with cls.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT facts FROM lead_profiles WHERE user_id = $1", user_id
            )
            return normalize_facts(_parse_jsonb(row["facts"] if row else None))

    @classmethod
    async def count_user_messages_today(cls, user_id: UUID) -> int:
        """Count inbound (user-role) messages from this user since midnight UTC today."""
        async with cls.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS cnt
                FROM messages m
                JOIN conversations c ON m.conversation_id = c.id
                WHERE c.user_id = $1
                  AND m.role = 'user'
                  AND m.created_at >= CURRENT_DATE
                """,
                user_id,
            )
            return row["cnt"] if row else 0

    @classmethod
    async def update_lead_score(cls, user_id: UUID, score: int) -> None:
        if score >= 4:
            label = "qualified"
        elif score >= 2:
            label = "warm"
        elif score >= 1:
            label = "low_priority"
        else:
            label = "unqualified"
        async with cls.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE lead_profiles SET score = $1, score_label = $2, updated_at = NOW()
                WHERE user_id = $3
                """,
                score, label, user_id,
            )

    @classmethod
    async def get_facts_audit(cls, user_id: UUID) -> List[dict]:
        async with cls.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM facts_audit WHERE user_id = $1
                ORDER BY changed_at DESC LIMIT 50
                """,
                user_id,
            )
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Handoff Queue
    # ------------------------------------------------------------------ #

    @classmethod
    async def create_handoff(
        cls,
        user_id: UUID,
        conversation_id: Optional[UUID],
        reason: str,
        priority: str,
        notes: Optional[str] = None,
    ) -> dict:
        async with cls.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO handoff_queue (user_id, conversation_id, reason, priority, notes)
                VALUES ($1, $2, $3, $4, $5) RETURNING *
                """,
                user_id, conversation_id, reason, priority, notes,
            )
            if conversation_id:
                await conn.execute(
                    "UPDATE conversations SET status = 'handoff' WHERE id = $1",
                    conversation_id,
                )
            return dict(row)

    @classmethod
    async def update_handoff_status(cls, handoff_id: UUID, status: str, assigned_to: Optional[str] = None) -> None:
        async with cls.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE handoff_queue
                SET status = $1,
                    assigned_to = COALESCE($2, assigned_to),
                    resolved_at = CASE WHEN $1 = 'resolved' THEN NOW() ELSE NULL END
                WHERE id = $3
                """,
                status, assigned_to, handoff_id,
            )

    # ------------------------------------------------------------------ #
    # Follow-up Queue
    # ------------------------------------------------------------------ #

    @classmethod
    async def schedule_followup(
        cls,
        user_id: UUID,
        conversation_id: Optional[UUID],
        external_id: str,
        channel: str,
        follow_up_type: str,
        message: str,
        scheduled_at,
    ) -> dict:
        async with cls.pool.acquire() as conn:
            # Cancel any existing pending follow-ups of same type for this user
            await conn.execute(
                """
                UPDATE follow_up_queue
                SET status = 'cancelled'
                WHERE user_id = $1 AND follow_up_type = $2 AND status = 'pending'
                """,
                user_id, follow_up_type,
            )
            row = await conn.fetchrow(
                """
                INSERT INTO follow_up_queue
                    (user_id, conversation_id, external_id, channel, follow_up_type, message, scheduled_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
                """,
                user_id, conversation_id, external_id, channel,
                follow_up_type, message, scheduled_at,
            )
            return dict(row)

    @classmethod
    async def cancel_pending_followups(cls, user_id: UUID) -> None:
        """Cancel all pending follow-ups for a user (they messaged back)."""
        async with cls.pool.acquire() as conn:
            await conn.execute(
                "UPDATE follow_up_queue SET status = 'cancelled' WHERE user_id = $1 AND status = 'pending'",
                user_id,
            )

    @classmethod
    async def get_due_followups(cls) -> List[dict]:
        """Return all pending follow-ups that are due to be sent now."""
        async with cls.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM follow_up_queue
                WHERE status = 'pending' AND scheduled_at <= NOW()
                ORDER BY scheduled_at ASC
                LIMIT 50
                """,
            )
            return [dict(r) for r in rows]

    @classmethod
    async def mark_followup_sent(cls, followup_id: UUID) -> None:
        async with cls.pool.acquire() as conn:
            await conn.execute(
                "UPDATE follow_up_queue SET status = 'sent', sent_at = NOW() WHERE id = $1",
                followup_id,
            )

    @classmethod
    async def attach_followup_message(cls, followup_id: UUID, message_id: UUID) -> None:
        async with cls.pool.acquire() as conn:
            await conn.execute(
                "UPDATE follow_up_queue SET message_id = $1 WHERE id = $2",
                message_id, followup_id,
            )

    @classmethod
    async def record_tool_outcome(
        cls,
        tool_name: str,
        success: bool,
        reason: Optional[str] = None,
        details: Optional[dict] = None,
        user_id: Optional[UUID] = None,
        conversation_id: Optional[UUID] = None,
    ) -> None:
        async with cls.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tool_outcomes
                    (user_id, conversation_id, tool_name, success, reason, details)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                """,
                user_id,
                conversation_id,
                tool_name,
                success,
                reason,
                json.dumps(details or {}),
            )

    @classmethod
    async def get_lead_data(cls, user_id: UUID) -> dict:
        """Return full lead data: user info + facts + latest conversation."""
        async with cls.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT u.id, u.external_id, u.channel, u.display_name, u.created_at,
                       lp.facts, lp.score, lp.score_label,
                       c.id as conversation_id, c.status as conversation_status,
                       c.last_message_at
                FROM users u
                LEFT JOIN lead_profiles lp ON u.id = lp.user_id
                LEFT JOIN conversations c ON c.user_id = u.id AND c.status = 'active'
                WHERE u.id = $1
                ORDER BY c.last_message_at DESC NULLS LAST
                LIMIT 1
                """,
                user_id,
            )
            if not row:
                return {}
            result = dict(row)
            result["facts"] = normalize_facts(_parse_jsonb(result.get("facts")))
            return result

    @classmethod
    async def get_lead_state_snapshot(cls, user_id: UUID) -> dict:
        async with cls.pool.acquire() as conn:
            profile_row = await conn.fetchrow(
                """
                SELECT u.id, u.external_id, u.channel, u.display_name, u.maya_paused,
                       lp.facts, lp.score, lp.score_label,
                       c.id AS conversation_id, c.status AS conversation_status, c.last_message_at
                FROM users u
                LEFT JOIN lead_profiles lp ON u.id = lp.user_id
                LEFT JOIN conversations c ON c.user_id = u.id AND c.status IN ('active', 'handoff')
                WHERE u.id = $1
                ORDER BY c.last_message_at DESC NULLS LAST
                LIMIT 1
                """,
                user_id,
            )
            if not profile_row:
                return {}

            handoff_row = await conn.fetchrow(
                """
                SELECT id, reason, priority, status, notes, created_at
                FROM handoff_queue
                WHERE user_id = $1 AND status IN ('pending', 'in_progress')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                user_id,
            )
            followup_row = await conn.fetchrow(
                """
                SELECT id, follow_up_type, scheduled_at, status, message
                FROM follow_up_queue
                WHERE user_id = $1 AND status = 'pending'
                ORDER BY scheduled_at ASC
                LIMIT 1
                """,
                user_id,
            )

            result = dict(profile_row)
            result["facts"] = normalize_facts(_parse_jsonb(result.get("facts")))
            result["open_handoff"] = dict(handoff_row) if handoff_row else None
            result["pending_followup"] = dict(followup_row) if followup_row else None
            return result

    # ------------------------------------------------------------------ #
    # Maya Skills (Hermes-inspired learning loop)
    # ------------------------------------------------------------------ #

    @classmethod
    async def get_relevant_skills(cls, tags: list, limit: int = 3) -> List[dict]:
        """Retrieve skills matching the given situation tags, ranked by success rate."""
        async with cls.pool.acquire() as conn:
            if not tags:
                rows = await conn.fetch(
                    """
                    SELECT * FROM maya_skills
                    ORDER BY (success_count::float / GREATEST(use_count, 1)) DESC, use_count DESC
                    LIMIT $1
                    """,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT *,
                        (SELECT COUNT(*) FROM unnest(situation_tags) t WHERE t = ANY($1::text[]))
                        AS tag_match_count
                    FROM maya_skills
                    WHERE situation_tags && $1::text[]
                    ORDER BY tag_match_count DESC,
                             (success_count::float / GREATEST(use_count, 1)) DESC
                    LIMIT $2
                    """,
                    tags, limit,
                )
            return [dict(r) for r in rows]

    @classmethod
    async def increment_skill_use(cls, skill_id: UUID) -> None:
        async with cls.pool.acquire() as conn:
            await conn.execute(
                "UPDATE maya_skills SET use_count = use_count + 1, last_used_at = NOW() WHERE id = $1",
                skill_id,
            )

    @classmethod
    async def mark_skill_success(cls, skill_id: UUID) -> None:
        async with cls.pool.acquire() as conn:
            await conn.execute(
                "UPDATE maya_skills SET success_count = success_count + 1 WHERE id = $1",
                skill_id,
            )

    @classmethod
    async def seed_skills(cls, skills: List[dict]) -> None:
        """Insert seed skills if table is empty."""
        async with cls.pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM maya_skills")
            if count > 0:
                return
            for skill in skills:
                await conn.execute(
                    """
                    INSERT INTO maya_skills
                        (situation_tags, situation_summary, approach, outcome, use_count, success_count)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    skill["tags"], skill["situation"], skill["approach"],
                    skill["outcome"], skill.get("use_count", 1), skill.get("success_count", 1),
                )

    # ------------------------------------------------------------------ #
    # Conversation Outcomes (hermes-dojo performance tracking)
    # ------------------------------------------------------------------ #

    @classmethod
    async def upsert_conversation_outcome(
        cls,
        conversation_id: UUID,
        outcome: str,
        fields_collected: List[str],
        drop_off_field: Optional[str] = None,
        total_turns: int = 0,
        converted_at_turn: Optional[int] = None,
    ) -> dict:
        async with cls.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO conversation_outcomes
                    (conversation_id, outcome, fields_collected, drop_off_field,
                     total_turns, converted_at_turn)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (conversation_id) DO UPDATE
                    SET outcome            = EXCLUDED.outcome,
                        fields_collected   = EXCLUDED.fields_collected,
                        drop_off_field     = EXCLUDED.drop_off_field,
                        total_turns        = EXCLUDED.total_turns,
                        converted_at_turn  = EXCLUDED.converted_at_turn,
                        updated_at         = NOW()
                RETURNING *
                """,
                conversation_id, outcome, fields_collected, drop_off_field,
                total_turns, converted_at_turn,
            )
            return dict(row)

    @classmethod
    async def get_learning_stats(cls) -> dict:
        async with cls.pool.acquire() as conn:
            total_skills = await conn.fetchval("SELECT COUNT(*) FROM maya_skills")
            total_outcomes = await conn.fetchval("SELECT COUNT(*) FROM conversation_outcomes")
            converted = await conn.fetchval(
                "SELECT COUNT(*) FROM conversation_outcomes WHERE outcome = 'converted'"
            )
            avg_turns = await conn.fetchval(
                "SELECT AVG(total_turns) FROM conversation_outcomes WHERE outcome = 'converted'"
            )
            top_skills = await conn.fetch(
                """
                SELECT situation_summary, approach, use_count, success_count,
                       ROUND((success_count::numeric / GREATEST(use_count, 1)) * 100, 0) AS success_pct
                FROM maya_skills
                WHERE use_count > 0
                ORDER BY (success_count::float / GREATEST(use_count, 1)) DESC
                LIMIT 5
                """
            )
            return {
                "total_skills": total_skills,
                "total_outcomes_recorded": total_outcomes,
                "conversions": converted,
                "conversion_rate": round(converted / max(total_outcomes, 1) * 100, 1),
                "avg_turns_to_convert": round(float(avg_turns or 0), 1),
                "top_skills": [dict(r) for r in top_skills],
            }

    # ------------------------------------------------------------------ #
    # Human Takeover / Pause State
    # ------------------------------------------------------------------ #

    @classmethod
    async def get_pause_state(cls, user_id: UUID) -> dict:
        """Returns {'paused': bool, 'paused_at': datetime|None}."""
        async with cls.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT maya_paused, paused_at FROM users WHERE id = $1", user_id
            )
            if not row:
                return {"paused": False, "paused_at": None}
            return {"paused": row["maya_paused"], "paused_at": row["paused_at"]}

    @classmethod
    async def set_user_paused(cls, user_id: UUID, paused: bool) -> None:
        """
        Pause or unpause Maya for a user.
        Pausing sets paused_at; unpausing leaves paused_at intact for catch-up injection.
        """
        async with cls.pool.acquire() as conn:
            if paused:
                await conn.execute(
                    "UPDATE users SET maya_paused = TRUE, paused_at = NOW(), updated_at = NOW() WHERE id = $1",
                    user_id,
                )
            else:
                await conn.execute(
                    "UPDATE users SET maya_paused = FALSE, updated_at = NOW() WHERE id = $1",
                    user_id,
                )

    @classmethod
    async def clear_pause_history(cls, user_id: UUID) -> None:
        """Clear paused_at after catch-up has been injected into the conversation."""
        async with cls.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET paused_at = NULL, updated_at = NOW() WHERE id = $1",
                user_id,
            )

    @classmethod
    async def load_paused_external_ids(cls) -> List[str]:
        """Return external_ids of all currently paused users (used at startup)."""
        async with cls.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT external_id FROM users WHERE maya_paused = TRUE"
            )
            return [r["external_id"] for r in rows]

    # ------------------------------------------------------------------ #
    # Shadow Messages (captured during human takeover pause)
    # ------------------------------------------------------------------ #

    @classmethod
    async def get_shadow_messages(cls, conversation_id: UUID, since) -> List[dict]:
        """Return admin + customer shadow messages logged since the pause began."""
        async with cls.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT role, content, source, created_at FROM messages
                WHERE conversation_id = $1
                  AND source IN ('admin', 'customer_paused')
                  AND created_at >= $2
                ORDER BY created_at ASC
                """,
                conversation_id, since,
            )
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Dashboard Queries
    # ------------------------------------------------------------------ #

    @classmethod
    async def get_analytics(cls) -> dict:
        async with cls.pool.acquire() as conn:
            total_conv = await conn.fetchval("SELECT COUNT(*) FROM conversations")
            qualified = await conn.fetchval(
                "SELECT COUNT(*) FROM lead_profiles WHERE score_label = 'qualified'"
            )
            warm = await conn.fetchval(
                "SELECT COUNT(*) FROM lead_profiles WHERE score_label = 'warm'"
            )
            handoffs = await conn.fetchval(
                "SELECT COUNT(*) FROM handoff_queue WHERE status != 'resolved'"
            )
            active_conv = await conn.fetchval(
                "SELECT COUNT(*) FROM conversations WHERE status = 'active'"
            )
            return {
                "total_conversations": total_conv,
                "active_conversations": active_conv,
                "qualified_leads": qualified,
                "warm_leads": warm,
                "active_handoffs": handoffs,
            }

    @classmethod
    async def list_conversations(cls, limit: int = 50, offset: int = 0) -> List[dict]:
        async with cls.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.id, c.status, c.started_at, c.last_message_at,
                       u.external_id, u.channel, u.display_name,
                       lp.score, lp.score_label, lp.facts,
                       COUNT(m.id) AS message_count
                FROM conversations c
                JOIN users u ON c.user_id = u.id
                LEFT JOIN lead_profiles lp ON u.id = lp.user_id
                LEFT JOIN messages m ON c.id = m.conversation_id
                GROUP BY c.id, u.id, lp.id
                ORDER BY c.last_message_at DESC
                LIMIT $1 OFFSET $2
                """,
                limit, offset,
            )
            result = []
            for r in rows:
                d = dict(r)
                d["facts"] = _parse_jsonb(d["facts"])
                result.append(d)
            return result

    @classmethod
    async def delete_conversation(cls, conv_id: UUID) -> None:
        async with cls.pool.acquire() as conn:
            await conn.execute("DELETE FROM messages WHERE conversation_id = $1", conv_id)
            await conn.execute("DELETE FROM conversations WHERE id = $1", conv_id)

    @classmethod
    async def get_conversation_detail(cls, conv_id: UUID) -> Optional[dict]:
        async with cls.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT c.id, c.status, c.started_at, c.last_message_at,
                       u.id as user_id, u.external_id, u.channel, u.display_name,
                       lp.score, lp.score_label, lp.facts
                FROM conversations c
                JOIN users u ON c.user_id = u.id
                LEFT JOIN lead_profiles lp ON u.id = lp.user_id
                WHERE c.id = $1
                """,
                conv_id,
            )
            if not row:
                return None
            result = dict(row)
            result["facts"] = _parse_jsonb(result["facts"])
            msgs = await conn.fetch(
                """
                SELECT role, content, source, created_at FROM messages
                WHERE conversation_id = $1 ORDER BY created_at ASC
                """,
                conv_id,
            )
            result["messages"] = [dict(m) for m in msgs]
            return result

    @classmethod
    async def list_leads(cls, limit: int = 50, offset: int = 0) -> List[dict]:
        async with cls.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT lp.id, lp.score, lp.score_label, lp.facts, lp.updated_at,
                       u.id as user_id, u.external_id, u.channel, u.display_name, u.created_at
                FROM lead_profiles lp
                JOIN users u ON lp.user_id = u.id
                ORDER BY lp.score DESC, lp.updated_at DESC
                LIMIT $1 OFFSET $2
                """,
                limit, offset,
            )
            result = []
            for r in rows:
                d = dict(r)
                d["facts"] = _parse_jsonb(d["facts"])
                result.append(d)
            return result

    @classmethod
    async def list_handoffs(cls, status: str = "pending") -> List[dict]:
        async with cls.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT h.id, h.reason, h.priority, h.notes, h.status,
                       h.assigned_to, h.created_at, h.resolved_at,
                       h.conversation_id,
                       u.external_id, u.channel, u.display_name,
                       lp.score, lp.score_label, lp.facts
                FROM handoff_queue h
                JOIN users u ON h.user_id = u.id
                LEFT JOIN lead_profiles lp ON u.id = lp.user_id
                WHERE h.status = $1
                ORDER BY
                    CASE h.priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                    h.created_at ASC
                """,
                status,
            )
            result = []
            for r in rows:
                d = dict(r)
                d["facts"] = _parse_jsonb(d["facts"])
                result.append(d)
            return result
