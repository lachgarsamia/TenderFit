from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str | None = None
    database_url: str = "postgresql://tenderfit:tenderfit@localhost:5432/tenderfit"
    fts_base_url: str = "https://www.find-tender.service.gov.uk/api/1.0"
    import_enabled: bool = True  # off for the fictional demo, so it never contacts Find a Tender
    import_interval_hours: float = 4
    import_overlap_minutes: int = 60
    import_chunk_hours: int = 24
    status_fresh_hours: float = 24
    http_max_retries: int = 8
    document_dir: str = "data/documents"
    llm_model_extract: str = "claude-opus-5-5"
    llm_model_agent: str = "claude-opus-5-5"
    llm_effort: str = "medium"
    llm_record_path: str | None = None
    llm_timeout_s: float = 300
    demo_scripted_llm: bool = False  # the fictional demo's scripted answers instead of a real model; never for real tenders
    lot_cost_cap_usd: float = 0.50
    lot_token_cap: int = 400_000
    lot_time_cap_s: float = 180
    agent_enabled: bool = True
    agent_max_tool_calls: int = 8
    # The what-if agent (Mode B). Read at each check, never captured at import.
    simulation_agent_daily_usd: float = 5.00     # a supplier's agent spend over any rolling 24 hours
    simulation_agent_session_usd: float = 0.50   # reserved at admission; one session's cost cap
    simulation_agent_deadline_s: float = 150     # one session, from the moment the worker starts it
    simulation_agent_max_tokens: int = 4096      # output per agent call; bounds a call's worst-case cost
    simulation_agent_queue_max: int = 20         # queued agent jobs across all suppliers
    versionrag_enabled: bool = False
    retrieval_min_score: float | None = None  # no default: a threshold must come from an evaluation on real data
    corrective_max_attempts: int = 1
    max_upload_bytes: int = 25 * 1024 * 1024
    registration_enabled: bool = False  # open sign-up; off unless an operator turns it on
    session_hours: int = 12
    session_cookie_secure: bool = True  # set false only for plain http on your own machine
    login_max_failures: int = 10
    login_lockout_minutes: int = 15
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"


settings = Settings()
