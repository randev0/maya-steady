-- Maya Steady Database Schema
-- Run this once to set up all tables

-- Users: leads interacting with the agent
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id TEXT NOT NULL UNIQUE,  -- FB/WA/test user ID
    channel TEXT NOT NULL DEFAULT 'test',  -- 'facebook', 'whatsapp', 'test'
    display_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Conversations: each session/chat thread
CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'completed', 'handoff', 'closed')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_message_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Messages: individual chat messages
CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Lead profiles: structured qualification facts (editable, auditable)
CREATE TABLE IF NOT EXISTS lead_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE UNIQUE,
    facts JSONB NOT NULL DEFAULT '{}',
    -- normalized lead-state remains in facts for compatibility-first rollout.
    -- canonical keys include:
    --   business_type, pain_point, message_volume_band, current_process,
    --   service_interest, budget_band, timeline_band, intent_stage,
    --   qualification_stage, lead_status, human_handoff_requested,
    --   handoff_reason, follow_up_stage, follow_up_count, next_follow_up_at, opt_out
    -- legacy aliases such as budget_range, timeline, current_tools, and
    -- message_volume are normalized in code on read/update to avoid a broad DB migration.
    score INTEGER NOT NULL DEFAULT 0,
    score_label TEXT NOT NULL DEFAULT 'unqualified'
        CHECK (score_label IN ('unqualified', 'low_priority', 'warm', 'qualified')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Facts audit log: every change to structured facts is recorded
CREATE TABLE IF NOT EXISTS facts_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    old_value JSONB,
    new_value JSONB,
    changed_by TEXT NOT NULL DEFAULT 'agent',  -- 'agent' | 'admin'
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- State transitions: normalized lead-state changes for auditability
CREATE TABLE IF NOT EXISTS state_transitions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
    field_name TEXT NOT NULL,
    old_value JSONB,
    new_value JSONB,
    changed_by TEXT NOT NULL DEFAULT 'system',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Tool outcomes: success/failure audit log for important tool executions
CREATE TABLE IF NOT EXISTS tool_outcomes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
    tool_name TEXT NOT NULL,
    success BOOLEAN NOT NULL,
    reason TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Handoff queue: active escalations for operator attention
CREATE TABLE IF NOT EXISTS handoff_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id UUID REFERENCES conversations(id),
    reason TEXT NOT NULL
        CHECK (reason IN ('high_score', 'user_request', 'urgency', 'frustration', 'out_of_scope')),
    priority TEXT NOT NULL DEFAULT 'medium'
        CHECK (priority IN ('high', 'medium', 'low')),
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'in_progress', 'resolved')),
    assigned_to TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);

-- Follow-up queue: scheduled re-engagement messages for inactive leads
CREATE TABLE IF NOT EXISTS follow_up_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
    external_id TEXT NOT NULL,           -- WhatsApp JID / FB sender ID for delivery
    channel TEXT NOT NULL DEFAULT 'whatsapp',
    follow_up_type TEXT NOT NULL
        CHECK (follow_up_type IN ('30min', 'few_hours', 'next_day')),
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sent', 'cancelled')),
    scheduled_at TIMESTAMPTZ NOT NULL,
    sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_follow_up_queue_status ON follow_up_queue(status, scheduled_at ASC);
CREATE INDEX IF NOT EXISTS idx_follow_up_queue_user_id ON follow_up_queue(user_id);

-- Maya Skills: learned conversation tactics (Hermes skill memory concept)
CREATE TABLE IF NOT EXISTS maya_skills (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    situation_tags TEXT[] NOT NULL DEFAULT '{}',
    situation_summary TEXT NOT NULL,
    approach TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('converted', 'engaged', 'neutral', 'rejected')),
    use_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);

-- Conversation Outcomes: tracks what happened per conversation (hermes-dojo performance loop)
CREATE TABLE IF NOT EXISTS conversation_outcomes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE UNIQUE,
    outcome TEXT NOT NULL CHECK (outcome IN ('converted', 'dropped', 'exploring', 'ongoing')),
    fields_collected TEXT[] NOT NULL DEFAULT '{}',
    drop_off_field TEXT,
    total_turns INTEGER NOT NULL DEFAULT 0,
    converted_at_turn INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Human takeover / pause support
ALTER TABLE users ADD COLUMN IF NOT EXISTS maya_paused BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS paused_at TIMESTAMPTZ;

-- Shadow message source: 'maya' (normal) | 'admin' (admin reply during pause) | 'customer_paused' (customer msg during pause) | 'catchup' (injected context block)
ALTER TABLE messages ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'maya';
ALTER TABLE follow_up_queue ADD COLUMN IF NOT EXISTS message_id UUID REFERENCES messages(id) ON DELETE SET NULL;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_users_external_id ON users(external_id);
CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations(user_id);
CREATE INDEX IF NOT EXISTS idx_conversations_status ON conversations(status);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(conversation_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_lead_profiles_user_id ON lead_profiles(user_id);
CREATE INDEX IF NOT EXISTS idx_lead_profiles_score ON lead_profiles(score DESC);
CREATE INDEX IF NOT EXISTS idx_handoff_queue_status ON handoff_queue(status, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_facts_audit_user_id ON facts_audit(user_id, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_state_transitions_user_id ON state_transitions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_outcomes_user_id ON tool_outcomes(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_maya_skills_tags ON maya_skills USING GIN(situation_tags);
CREATE INDEX IF NOT EXISTS idx_maya_skills_outcome ON maya_skills(outcome, use_count DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_outcomes_conv_id ON conversation_outcomes(conversation_id);
CREATE INDEX IF NOT EXISTS idx_conversation_outcomes_outcome ON conversation_outcomes(outcome);
