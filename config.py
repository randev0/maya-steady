import os
from pathlib import Path
from typing import List, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


_ENV_FILE = Path(__file__).with_name(".env")
_SAFE_ENV_FILE = str(_ENV_FILE) if _ENV_FILE.is_file() and os.access(_ENV_FILE, os.R_OK) else None


class Settings(BaseSettings):
    # LLM
    llm_provider: str = "ollama"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:1.5b"
    llm_timeout_seconds: float = 20.0
    llm_max_retries: int = 2
    openrouter_api_key: str = ""
    agent_model: str = ""  # empty = use model from .openclaw/openclaw.json

    # Database
    database_url: str = "postgresql://selfops:password@localhost:5432/leadqualbot"

    # Honcho (optional conversational memory layer)
    honcho_api_key: Optional[str] = None
    honcho_app_name: str = "leadqualbot"

    # Facebook Messenger
    fb_page_access_token: Optional[str] = None
    fb_verify_token: str = "leadqualbot_verify_2024"
    fb_app_secret: Optional[str] = None

    # WhatsApp Cloud API (Meta)
    wa_phone_number_id: Optional[str] = None      # From Meta App Dashboard
    wa_access_token: Optional[str] = None          # Permanent system user token
    wa_verify_token: str = "maya_wa_verify_2024"   # You choose this, set same in Meta dashboard

    # Trial promotion
    trial_daily_message_limit: int = 20  # max user messages per day during trial

    # Manager access — WhatsApp sender_id of the business owner
    # Format: "601XXXXXXXX" or "601XXXXXXXX@lid" — whatever shows up as sender_id in logs
    manager_wa_id: Optional[str] = None

    # Admin WhatsApp numbers notified on handoff — bare numbers without @c.us
    admin_wa_numbers: List[str] = ["60172711775", "60175660603"]

    # Public base URL of the dashboard (used to build links in WA notifications)
    dashboard_url: str = "https://agent.steadigital.com"
    pause_action_secret: Optional[str] = None

    # App
    app_host: str = "0.0.0.0"
    app_port: int = 8080
    debug: bool = False
    admin_api_token: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    wa_gateway_base_url: str = "http://127.0.0.1:3001"
    followup_dispatch_interval_seconds: int = 60

    # Webhook export (optional: push qualified leads to external system)
    lead_export_webhook_url: Optional[str] = None

    model_config = SettingsConfigDict(env_file=_SAFE_ENV_FILE, extra="ignore")


settings = Settings()
