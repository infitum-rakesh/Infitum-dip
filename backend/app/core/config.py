from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Data Intelligence Platform"
    app_env: str = "development"
    app_debug: bool = True

    secret_key: str = "dev-secret-change-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    database_url: str = "sqlite+aiosqlite:///./dip.db"

    allowed_origins: str = "http://localhost:5173,http://localhost:3000,http://localhost:8080"

    # Where the frontend actually lives — used to build real links inside
    # outbound emails (Seller-created-customer welcome email; later SSO
    # ACS/metadata URLs). Override via env in any real deployment.
    frontend_base_url: str = "http://localhost:5173"

    # Where the backend's /api/* routes are externally reachable from —
    # used to build the SAML SP entity ID / ACS / metadata URLs an IdP
    # admin configures (services/saml_service.py). Defaults to
    # frontend_base_url because the dev Vite server proxies /api to this
    # backend, making them the same effective origin; in a real deployment
    # behind a reverse proxy they're typically still the same origin, but
    # override independently if /api is ever split to its own host.
    backend_public_base_url: str = ""

    @property
    def api_public_base_url(self) -> str:
        return self.backend_public_base_url or self.frontend_base_url

    @property
    def origins_list(self) -> list[str]:
        raw = self.allowed_origins.strip()
        if raw == "*":
            return ["*"]
        origins = [o.strip() for o in raw.split(",") if o.strip()]
        if not origins:
            return [self.frontend_base_url]
        return origins

    first_superadmin_email: str = "admin@dip.local"
    first_superadmin_password: str = "Admin@1234!"

    # Seller Portal — the platform's own seeded Seller-tier admin, same
    # bootstrap pattern as first_superadmin_* above (see main.py's
    # _seed_seller_admin). Distinct account so "platform operator" and
    # "seller operator" are separately auditable even though today they're
    # run by the same team.
    # NOTE: deliberately NOT *.local — email_validator (2.x) rejects .local
    # as a reserved/special-use TLD, which also silently breaks login for
    # the pre-existing first_superadmin_email default above (admin@dip.local)
    # — a pre-existing bug discovered, not introduced, here; see Seller
    # Portal Phase 1 summary.
    first_seller_admin_email: str = "seller@platform-admin.com"
    first_seller_admin_password: str = "Seller@1234!"

    # Documents module
    upload_dir: str = "./uploads"
    max_upload_size_mb: int = 25

    # Review Queue module — auto-priority thresholds (confidence %), all
    # env-overridable so ops can tune without touching code.
    review_priority_critical_below: float = 60.0   # confidence < 60%  -> Critical
    review_priority_high_below: float = 80.0       # confidence < 80%  -> High
    review_priority_medium_below: float = 90.0     # confidence < 90%  -> Medium; else Low
    review_priority_age_escalation_hours: int = 24  # items older than this get bumped one level + count as Overdue
    review_business_rule_total_threshold: float = 20000.0  # Total above this always needs review

    # Schedules module — executes existing pipelines rather than a second
    # execution engine (see schedule_service module docstring); this
    # success rate is the only tunable for the simulated run outcome.
    schedule_run_success_rate: float = 0.9

    # Incidents module — SLA clocks, in hours, by severity. An open incident
    # (not RESOLVED/CLOSED) breaches SLA once it's been open longer than its
    # severity's window; computed on read, never stored (see incident_service.py).
    incident_sla_hours_critical: float = 4.0
    incident_sla_hours_high: float = 8.0
    incident_sla_hours_medium: float = 24.0
    incident_sla_hours_low: float = 72.0

    # ZIP Batch Upload module (Document Intelligence + Invoice Intelligence,
    # ZB1-ZB12) — one ZIP upload = one chunked upload session + one batch
    # run + many bounded-parallel per-file tasks, reusing the existing
    # per-document pipeline (never a second processing engine, never one
    # pipeline/scheduler-job per file). All limits below are real, enforced
    # values (zip_security_service.py/document_batch_service.py) — "never
    # extract unlimited data."
    zip_upload_enabled: bool = True
    max_zip_size_mb: int = 10240          # 10 GB — whole uploaded archive
    max_extracted_size_mb: int = 51200    # 50 GB — running total across all extracted entries
    max_files_per_zip: int = 10000
    max_single_file_size_mb: int = 5120   # 5 GB — per extracted entry
    max_zip_depth: int = 20               # directory nesting depth of an entry's path
    max_compression_ratio: int = 100      # uncompressed:compressed ratio above this is treated as a decompression bomb
    allow_nested_zip: bool = False        # a .zip-within-the-.zip is catalogued as NESTED_ARCHIVE_NOT_SUPPORTED unless true
    zip_retention_days: int = 30
    extracted_file_retention_days: int = 30
    auto_process_after_upload: bool = True
    upload_chunk_size_mb: int = 8
    upload_concurrency: int = 4           # concurrent chunk uploads the client may use per session
    batch_insert_size: int = 500          # DocumentBatchFile rows per bulk-insert during registration
    processing_batch_size: int = 50

    # Document-processing concurrency (ZB5) — three independent layers:
    # DOCUMENT_WORKER_COUNT bounds the existing process-wide
    # _background_processing_semaphore (manual/bulk upload, document
    # schedules, AND batch tasks all now share this one real cap);
    # OCR_CONCURRENCY/AI_CONCURRENCY are finer sub-limits acquired around
    # just the OCR stage and just the AI-extraction stage inside
    # process_document(), so "8 documents processing but only 4 concurrent
    # AI requests" is expressible. Fixed at process start (a Semaphore
    # can't be resized per-request) — deliberately NOT part of the per-org
    # runtime-editable settings blob (settings_service.py).
    document_worker_count: int = 4
    ocr_concurrency: int = 4
    ai_concurrency: int = 4

    # TALLY-B — bounds concurrent in-flight HTTP requests to a single
    # TallyPrime instance's local gateway during extraction (date-range
    # chunking fans out multiple requests; Tally's gateway is a single
    # local process with no documented concurrency guidance, so this stays
    # conservative). Fixed at process start, same rationale as ocr/ai
    # concurrency above.
    tally_max_concurrent_requests: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
