"""
Seed skills for Maya's learning loop.
These are hand-crafted starting tactics derived from known good conversation patterns.
Once the agent accumulates real conversation data, new skills will be added automatically.
"""

SEED_SKILLS = [
    {
        "tags": ["passive_user", "short_reply", "no_name_yet"],
        "situation": "User gives vague or one-word reply, name not yet known",
        "approach": "Don't accept the vague reply. Pivot with 'No worries 👍' then ask name naturally.",
        "outcome": "engaged",
        "use_count": 5, "success_count": 4,
    },
    {
        "tags": ["first_message", "no_business_yet"],
        "situation": "User asks what AI agent does — first contact, no context yet",
        "approach": "One line on what it does (auto-reply, follow up). Then immediately ask about their business volume or type.",
        "outcome": "converted",
        "use_count": 8, "success_count": 6,
    },
    {
        "tags": ["product_seller", "high_volume", "pain_identified"],
        "situation": "User sells products, high WhatsApp volume, pain is manually handling messages",
        "approach": "Validate the pain ('sorang handle 60-80 mesej memang penat'), micro-pitch automation, then ask about setup preference (simple vs full).",
        "outcome": "converted",
        "use_count": 6, "success_count": 5,
    },
    {
        "tags": ["service_seller", "pain_identified"],
        "situation": "User offers services (cleaning, repair, etc.), pain is repeat questions or booking management",
        "approach": "Connect specifically to their service: 'Untuk bisnes [service], agent banyak bantu dengan appointment & reminder.' Then ask volume.",
        "outcome": "converted",
        "use_count": 4, "success_count": 3,
    },
    {
        "tags": ["flexible_timeline", "no_pain_yet"],
        "situation": "User says they are just exploring, no urgency mentioned",
        "approach": "Don't push. Keep collecting business type and pain. Say 'Sure, explore dulu 😊' then ask one more grounding question.",
        "outcome": "engaged",
        "use_count": 7, "success_count": 5,
    },
    {
        "tags": ["budget_sensitive", "no_name_yet"],
        "situation": "User asks about price early before giving any info",
        "approach": "Never quote prices. Say 'depends on setup — boleh tahu bisnes you dalam bidang apa dulu?' to redirect to qualification.",
        "outcome": "engaged",
        "use_count": 5, "success_count": 4,
    },
    {
        "tags": ["urgent_timeline", "pain_identified"],
        "situation": "User mentions they need it soon or have an event/deadline",
        "approach": "Match their urgency: 'Perfect — sempat setup sebelum [event].' Then move directly to contact collection.",
        "outcome": "converted",
        "use_count": 4, "success_count": 4,
    },
    {
        "tags": ["engaged_user", "business_known", "pain_identified"],
        "situation": "User is engaged, business type and pain already known",
        "approach": "Soft budget question: 'You prefer start simple dulu atau terus full automation?' — never ask RM directly.",
        "outcome": "converted",
        "use_count": 6, "success_count": 5,
    },
    {
        "tags": ["returning_user"],
        "situation": "User has messaged before, some facts already in profile",
        "approach": "Reference what's already known: 'Last time you mention [business_type] — masih sama ke?' Shows memory, builds trust.",
        "outcome": "engaged",
        "use_count": 3, "success_count": 3,
    },
    {
        "tags": ["passive_user", "flexible_timeline"],
        "situation": "User is not urgent, gives passive replies consistently",
        "approach": "Shift to a light curiosity question: 'Just curious — sekarang customer selalu WhatsApp you terus ke?' Keeps door open without pressure.",
        "outcome": "engaged",
        "use_count": 4, "success_count": 3,
    },
]
