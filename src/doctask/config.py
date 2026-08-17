from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="DOCTASK_", extra="ignore")

    database_url: str = "postgresql://doctask:doctask@localhost:5432/doctask"
    repository: str = "memory"

    # fake = deterministic offline model, gateway = OpenAI-compatible server
    llm: str = "fake"
    # Any OpenAI-compatible /v1/chat/completions server. Only consulted when
    # `llm == "gateway"`; the default offline model needs no server at all.
    llm_base_url: str = "http://localhost:8001"
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

    # The credential the collection watcher presents. It has to resolve to one of
    # `service_tokens`: a background process that authenticated as a reviewer could
    # start runs recorded as a human act, so a reviewer token here is refused outright
    # (see `auth.require_service`). Empty means the watcher will not start.
    watcher_token: str = ""
    # Seconds between sweeps of every watched directory. A file is picked up on the
    # second consecutive poll that finds it unchanged, so a file lands roughly one to
    # two intervals after it finishes being written.
    watch_interval: float = 5.0
    # Refuse to read a file larger than this into memory. Recorded as an attempt like
    # any other failure, so an oversized drop does not get re-read every interval.
    watch_max_file_bytes: int = 64 * 1024 * 1024

    # Where the MCP server says its tokens come from and what resource they are for.
    # Both are advertised in OAuth protected-resource metadata, so a client that gets a
    # 401 can find out where to authenticate.
    mcp_issuer_url: str = "http://localhost:8000"
    mcp_resource_server_url: str = "http://localhost:8000"

    classification_threshold: float = 0.70
    max_validation_repairs: int = 1

    # A reviewer-readable, versioned $/million-token table. The cost report states this
    # file's own declared version alongside every number it produces, so a spend figure
    # can always be traced back to the prices that produced it -- never a constant a
    # call site made up on its own.
    price_table_path: str = str(_REPO_ROOT / "config" / "model_prices.json")

    # Evidence bounds per rule evaluation. Cost and prompt size scale with these, not with
    # the size of the corpus, and a truncated prompt is how a violation becomes a `pass`.
    rule_context_blocks: int = 12
    rule_context_chars: int = 8000


settings = Settings()
