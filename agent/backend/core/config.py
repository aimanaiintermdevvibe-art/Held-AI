from functools import lru_cache
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Held AI Agent API"
    
    # Gemini Configuration
    gemini_api_key: str = ""
    gemini_agent1_api_key: str = ""
    gemini_agent2_api_key: str = ""
    gemini_agent3_api_key: str = ""
    gemini_agent4_api_key: str = ""
    
    gemini_agent1_model: str = "gemini-3.5-flash-lite"
    gemini_agent2_model: str = "gemini-3.5-flash-lite"
    gemini_agent3_model: str = "gemini-3.5-flash-lite"
    gemini_agent4_model: str = "gemini-3.5-flash-lite"
    
    gemini_agent1_thinking_level: str = "low"
    gemini_agent2_thinking_level: str = "low"
    gemini_agent3_thinking_level: str = "medium"
    gemini_agent4_thinking_level: str = "low"
    
    gemini_timeout_seconds: float = Field(default=45, gt=0, le=45)
    agentic_run_timeout_seconds: float = Field(default=240, gt=0, le=600)
    agentic_stream_chunk_size: int = Field(default=24, gt=0, le=500)
    agentic_stream_delay_seconds: float = Field(default=0.03, ge=0.0, le=1.0)
    # Upper limit on one Gemini answer, thinking included. Replies use far less; it only stops
    # a runaway answer from running up the bill.
    gemini_max_output_tokens: int = Field(default=8192, ge=1024, le=65536)
    # Testing aid: show each reply's token use in /run, /stream and the messages API. It is
    # stored with the reply either way.
    show_token_usage: bool = True
    # Local testing only: serve Swagger at /docs (and the schema at /openapi.json). Keep it off in
    # production, so the API's map is not public.
    show_api_docs: bool = False

    # Held Backend API (user profiles and grief notes)
    held_api_base_url: str = ""

    # The Held app's PostgreSQL database ("ChatSession", "ChatMessage", "User", "GriefNote")
    database_url: str = ""
    # Key that verifies Held access tokens locally; without it, tokens are confirmed with Held.
    jwt_secret: str = ""

    model_config = SettingsConfigDict(
        env_file=(
            Path(__file__).resolve().parents[3] / ".env",
            Path(__file__).resolve().parents[2] / ".env",
        ),
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()