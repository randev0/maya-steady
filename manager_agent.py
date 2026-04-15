"""
Manager Agent — handles WhatsApp messages from the business owner.
Gives Maya's boss natural-language access to lead stats, conversation details,
follow-up status, and performance insights.
"""
import json
import structlog
from openai import AsyncOpenAI
from database.dal import Database
from config import settings

log = structlog.get_logger()

_MANAGER_SYSTEM_PROMPT = """
You are Maya — WhatsApp sales rep for Steadigital. The person messaging you right now is your boss, the business owner.

With your boss, you are fully yourself. Same casual Manglish tone. But no rules from the sales playbook apply here — your boss gets full honesty and full access.

This means:
- If boss asks if you're an AI — yes, confirm it. "Ha, I'm an AI agent built on claude-3.5-haiku, running via OpenRouter."
- If boss asks what model you use — tell them directly.
- If boss asks anything technical about how you work — answer honestly.
- If boss asks about leads — use the stats snapshot to answer.
- If boss just says hi or calls your name — respond naturally and briefly. Not a report.
- Answer WHATEVER the boss asks. Don't steer back to leads unless they ask about leads.

IMPORTANT: Never invent or guess numbers. All stats you report must come from the stats snapshot provided in the conversation. If you don't have a stats snapshot, say so — don't make figures up.

Tone: casual, warm, like texting your manager. Short unless they want detail. Honest even if the news isn't great.

If boss asks what commands are available, tell them:
- prospect on — test Maya as a real prospect (you become a fresh lead)
- prospect off — exit prospect mode, back to manager mode
- prospect reset — clear the prospect test conversation history
- reload — reload Maya's system prompt from file (after manual edits)
- show prompt — view Maya's current full system prompt
- show rules — view just the NEVER section of the prompt
- add rule: <text> — append a rule to Maya's NEVER list and auto-reload
"""


async def _fetch_stats() -> dict:
    """Pull a live stats snapshot from the DB for context injection."""
    async with Database.pool.acquire() as conn:
        # Overview counts
        overview = await conn.fetchrow("""
            SELECT
                (SELECT COUNT(*) FROM users WHERE channel = 'whatsapp') AS total_leads,
                (SELECT COUNT(*) FROM conversations WHERE status = 'active') AS active_conversations,
                (SELECT COUNT(*) FROM lead_profiles WHERE score > 0) AS scored_leads,
                (SELECT COUNT(*) FROM follow_up_queue WHERE status = 'pending') AS pending_followups,
                (SELECT COUNT(*) FROM handoff_queue WHERE status = 'pending') AS pending_handoffs,
                (SELECT COUNT(*) FROM messages WHERE role = 'user' AND created_at > NOW() - INTERVAL '24h') AS user_msgs_today,
                (SELECT COUNT(*) FROM messages WHERE role = 'assistant' AND created_at > NOW() - INTERVAL '24h') AS maya_msgs_today
        """)

        # Top leads with facts
        leads = await conn.fetch("""
            SELECT
                u.display_name,
                u.external_id,
                lp.score,
                lp.score_label,
                lp.facts,
                lp.updated_at,
                (SELECT COUNT(*) FROM messages m
                 JOIN conversations c ON m.conversation_id = c.id
                 WHERE c.user_id = u.id) AS total_msgs,
                (SELECT m.created_at FROM messages m
                 JOIN conversations c ON m.conversation_id = c.id
                 WHERE c.user_id = u.id ORDER BY m.created_at DESC LIMIT 1) AS last_active
            FROM lead_profiles lp
            JOIN users u ON lp.user_id = u.id
            ORDER BY lp.score DESC, lp.updated_at DESC
            LIMIT 10
        """)

        # Pending follow-ups
        followups = await conn.fetch("""
            SELECT u.display_name, fq.follow_up_type, fq.scheduled_at, LEFT(fq.message, 80) AS message
            FROM follow_up_queue fq
            JOIN users u ON fq.user_id = u.id
            WHERE fq.status = 'pending'
            ORDER BY fq.scheduled_at ASC
            LIMIT 10
        """)

        # Pending handoffs
        handoffs = await conn.fetch("""
            SELECT u.display_name, hq.priority, hq.reason, hq.created_at
            FROM handoff_queue hq
            JOIN users u ON hq.user_id = u.id
            WHERE hq.status = 'pending'
            ORDER BY hq.created_at DESC
            LIMIT 10
        """)

    return {
        "overview": dict(overview),
        "leads": [dict(r) for r in leads],
        "pending_followups": [dict(r) for r in followups],
        "pending_handoffs": [dict(r) for r in handoffs],
    }


def _format_stats(stats: dict) -> str:
    """Format stats snapshot as readable context for the LLM."""
    ov = stats["overview"]
    lines = [
        "=== MAYA STATS SNAPSHOT ===",
        f"Total WA leads: {ov['total_leads']}",
        f"Active conversations: {ov['active_conversations']}",
        f"Scored leads (score > 0): {ov['scored_leads']}",
        f"Pending follow-ups: {ov['pending_followups']}",
        f"Pending handoffs: {ov['pending_handoffs']}",
        f"User messages today: {ov['user_msgs_today']}",
        f"Maya replies today: {ov['maya_msgs_today']}",
        "",
        "=== LEADS ===",
    ]

    for lead in stats["leads"]:
        facts = lead["facts"] if isinstance(lead["facts"], dict) else {}
        lines.append(
            f"- {lead['display_name'] or 'Unknown'} | score={lead['score']} ({lead['score_label']}) "
            f"| msgs={lead['total_msgs']} | last active={str(lead['last_active'])[:16] if lead['last_active'] else 'never'}"
        )
        if facts:
            lines.append(f"  facts: {json.dumps(facts, ensure_ascii=False)}")

    if stats["pending_followups"]:
        lines.append("\n=== PENDING FOLLOW-UPS ===")
        for fu in stats["pending_followups"]:
            lines.append(
                f"- {fu['display_name']} | {fu['follow_up_type']} | due={str(fu['scheduled_at'])[:16]}"
            )

    if stats["pending_handoffs"]:
        lines.append("\n=== PENDING HANDOFFS ===")
        for h in stats["pending_handoffs"]:
            lines.append(
                f"- {h['display_name']} | priority={h['priority']} | reason={h['reason'][:80]}"
            )

    return "\n".join(lines)


_STATS_KEYWORDS = (
    "lead", "report", "stats", "update", "progress", "berapa", "siapa",
    "who", "how many", "follow", "handoff", "conversion", "score",
    "ryan", "imran", "pipeline", "result", "performance", "today",
    "semalam", "minggu", "week", "bulan", "month",
    "health", "check", "diagnostic", "buat", "status", "summary",
    "alignment", "pulse", "overview", "system", "how is", "macam mana",
)


def _needs_stats(message: str) -> bool:
    """Only fetch stats if the message is actually asking about something data-related."""
    lowered = message.lower()
    return any(kw in lowered for kw in _STATS_KEYWORDS)


async def process_manager_message(message: str) -> str:
    """
    Handle a message from the business owner.
    Returns a plain text reply with insights/stats.
    """
    if _needs_stats(message):
        try:
            stats = await _fetch_stats()
            stats_context = _format_stats(stats)
        except Exception as exc:
            log.error("manager_stats_fetch_failed", error=str(exc))
            stats_context = "(Stats unavailable — DB error)"
        user_content = f"{stats_context}\n\n---\n\nBoss: {message}"
    else:
        user_content = f"Boss: {message}"

    client = AsyncOpenAI(
        api_key=settings.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        default_headers={
            "HTTP-Referer": "https://agent.steadigital.com",
            "X-Title": "LeadQualBot-Manager",
        },
    )

    messages = [
        {"role": "system", "content": _MANAGER_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    try:
        response = await client.chat.completions.create(
            model="anthropic/claude-3.5-haiku",
            max_tokens=600,
            messages=messages,
        )
        reply = (response.choices[0].message.content or "").strip()
        if not reply:
            return "Sorry, couldn't generate a response. Check the dashboard for details."
        return reply
    except Exception as exc:
        log.error("manager_agent_error", error=str(exc))
        return f"Error generating report: {str(exc)[:200]}"
