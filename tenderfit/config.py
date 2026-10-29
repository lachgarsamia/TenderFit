from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://tenderfit:tenderfit@localhost:5432/tenderfit"
    fts_base_url: str = "https://www.find-tender.service.gov.uk/api/1.0"
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
    lot_cost_cap_usd: float = 0.50
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"


settings = Settings()
