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
    memory_context_char_budget: int = Field(default=120_000, gt=0)

    # Uploaded documents and retrieval (RAG). Vectors are 768-dimensional (migration 002).
    embedding_model: str = "gemini-embedding-001"
    rag_top_k: int = Field(default=5, ge=1, le=20)
    # Measured with gemini-embedding-001: relevant passages score about 0.59-0.69,
    # unrelated questions about 0.47-0.50 (see logs/rag_pgvector_setup_and_test.log).
    rag_min_similarity: float = Field(default=0.55, ge=0.0, le=1.0)
    document_max_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    document_max_chunks: int = Field(default=0, ge=0)  # 0 = no limit; long files are embedded in the background
    embedding_batch_size: int = Field(default=100, ge=1, le=100)  # chunks per Gemini embedding request
    # Run the background embedding worker in this instance. With several instances, enable it
    # in one only; the others accept uploads and leave them queued for it.
    ingestion_worker: bool = True
    document_chunk_chars: int = Field(default=800, ge=200, le=6000)
    document_chunk_overlap: int = Field(default=150, ge=0, le=1000)

    # Held Backend API (the terminal client's accounts are in users/user<N>.json)
    held_api_base_url: str = ""

    # Database & JWT Settings
    database_url: str = ""
    database_pooler_url: str = ""
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