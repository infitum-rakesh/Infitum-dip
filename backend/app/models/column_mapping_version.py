"""
ColumnMappingVersion — CM1: real, immutable version history for
ColumnMapping (Column Mapping <-> Pipeline Builder integration build).

Mirrors models/pipeline.py's Pipeline/PipelineVersion split exactly:
ColumnMapping (models/column_mapping.py) becomes a lightweight header row
(name, source/target refs used for list-page filtering, and two pointers
-- latest_version / current_published_version), while every real saved
mapping definition lives here as an immutable snapshot row. Editing a
mapping NEVER overwrites an existing version -- see
services/mapping_service.py::create_new_version(), which always INSERTs
version_number = mapping.latest_version + 1, exactly like
services/pipeline_service.py::save_version() does for pipelines.

Each version snapshots its own source/target object refs (not just the
parent's) so a historical version stays fully self-describing even if a
future feature ever lets someone repoint the header's source/target --
the same reasoning PipelineVersion.pipeline_definition being fully
self-contained already established for pipelines.

`mappings` keeps the exact list[dict] row shape documented in
models/column_mapping.py's module docstring, with one addition: each row
may now carry "is_key": bool (business/natural key -- feeds SCD1/SCD2
business-key resolution, see execution/mapping_execution.py).

Lifecycle: DRAFT -> VALIDATED -> PUBLISHED -> RETIRED (spec section 13).
Only a PUBLISHED version may be attached to a production pipeline node
(enforced server-side in execution/mapping_execution.py::resolve_mapping_version(),
never just filtered client-side) or newly started by the standalone Run
Load path (see models/target_load_run.py's mapping_version column).
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class ColumnMappingVersion(TenantBase):
    __tablename__ = "column_mapping_versions"
    __table_args__ = (UniqueConstraint("mapping_id", "version_number", name="uq_column_mapping_version"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    mapping_id: Mapped[int] = mapped_column(ForeignKey("column_mappings.id"), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)  # DRAFT | VALIDATED | PUBLISHED | RETIRED

    mappings: Mapped[list] = mapped_column(JSON, default=list)  # list[dict] -- see column_mapping.py docstring for row shape (+ is_key)

    # Snapshot of the source/target object this version was built against --
    # self-contained even if the parent header row's refs later change.
    source_connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False)
    source_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_object: Mapped[str | None] = mapped_column(String(200), nullable=True)
    target_connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False)
    target_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    target_object: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Runtime policy for execution/mapping_execution.py::apply_mapping_to_dataframe()
    unmapped_column_policy: Mapped[str] = mapped_column(String(20), default="IGNORE")  # IGNORE | ERROR | WARNING (spec section 26)
    batch_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    on_conversion_failure: Mapped[str] = mapped_column(String(20), default="REJECT_RECORD")  # FAIL_PIPELINE | REJECT_RECORD | SET_NULL | USE_DEFAULT

    change_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # persisted mapper.validate_mapping() output

    # Snapshot of how many columns the source/target objects had AT THE TIME
    # this version was created (live-fetched once, at creation) — the "then"
    # baseline api/mapping.py's staleness route diffs a live re-fetch
    # against, to answer "MAPPING OUT OF DATE" (spec section 19/20) without
    # re-deriving history from nothing. NULL on versions created before this
    # column existed (CM1's backfilled pre-versioning row) — staleness-by-
    # count is simply skipped for those, falling back to the live drift
    # issues validate_mapping() already detects by column name.
    source_column_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_column_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<ColumnMappingVersion mapping={self.mapping_id} v{self.version_number} {self.status}>"
