from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="DOCTASK_", extra="ignore")

    database_url: str = "postgresql://doctask:doctask@localhost:5432/doctask"
    repository: str = "memory"

    # fake = deterministic offline model, gateway = OpenAI-compatible server
    llm: str = "fake"
    llm_base_url: str = "http://164.52.193.211:8001"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout: float = 60.0
    # Vision model used only as OCR fallback, for pages PyMuPDF could not read. Empty
    # means no fallback: an unreadable page fails the upload instead of being ingested
    # as an empty page that every rule would then judge on absent evidence.
    vlm_model: str = ""

    # `token:actor_id` pairs, comma separated. Reviewers are the humans allowed to
    # approve, reject, and override; services are the model-facing callers, which may
    # start runs and produce proposals but never decide them. Empty means nobody
    # authenticates: the gate fails closed rather than open.
    reviewer_tokens: str = ""
    service_tokens: str = ""

    # Where the MCP server says its tokens come from and what resource they are for.
    # Both are advertised in OAuth protected-resource metadata, so a client that gets a
    # 401 can find out where to authenticate.
    mcp_issuer_url: str = "http://localhost:8000"
    mcp_resource_server_url: str = "http://localhost:8000"

    classification_threshold: float = 0.70
    max_validation_repairs: int = 1

    # Evidence bounds per rule evaluation. Cost and prompt size scale with these, not with
    # the size of the corpus, and a truncated prompt is how a violation becomes a `pass`.
    rule_context_blocks: int = 12
    rule_context_chars: int = 8000


settings = Settings()
