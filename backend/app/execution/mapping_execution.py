"""
mapping_execution — real application of a PUBLISHED ColumnMappingVersion
to an in-flight pipeline DataFrame (CM6: Column Mapping <-> Pipeline
Builder integration, spec sections 21-23 "most important requirement" —
the mapping must actually be used when reading/loading data, not a
cosmetic end-of-pipeline rename).

Kept as its own module, out of the ~1300-line pipeline_job_executor.py,
per this build's "do not put everything in one file" instruction.
pipeline_job_executor.py's dispatch chain calls this module's functions
from a thin `_run_column_mapping()` wrapper — session/logging plumbing
stays there, the real mapping logic lives here.

Deliberately raises its OWN MappingExecutionError rather than importing
pipeline_job_executor.NodeExecutionError — pipeline_job_executor.py
imports this module, so importing back from it would be a circular
import. The `_run_column_mapping()` wrapper in pipeline_job_executor.py
is the one place that catches MappingExecutionError and re-raises it as
NodeExecutionError, which the executor's dispatch loop already handles
uniformly for every other node type.

Three real, separately-testable pieces:
  - resolve_mapping_version()          — load the EXACT pinned (id, version)
    row and refuse anything not PUBLISHED, server-side, regardless of
    whatever the config panel's picker already filtered to.
  - check_source_target_consistency()  — the same source/target drift
    check the config panel does client-side and /attach does at attach
    time, re-run here as defense-in-depth at the moment of execution.
  - apply_mapping_to_dataframe()       — the actual transform: rename /
    constant / default / transformation / lookup, applied to the real
    in-flight pandas DataFrame, honoring on_conversion_failure and
    unmapped_column_policy for real, not as inert config fields.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError
from app.mapping import mapper, transform_engine
from app.models.column_mapping_version import ColumnMappingVersion
from app.schemas.mapping import ObjectRef
from app.services import connection_service, mapping_service

# Bounded, in-memory fetch of a lookup/reference table — same reasoning
# as pipeline_job_executor.SOURCE_ROW_CAP: this engine joins the lookup
# table in memory (a plain dict), so it has to fit in memory. A lookup
# table bigger than this is the same disclosed kind of limitation as the
# 100M-row source-side ceiling, not silently truncated without a log line.
LOOKUP_FETCH_CAP = 50_000

LogFn = Callable[[str, str], Awaitable[None]]


class MappingExecutionError(Exception):
    """A column_mapping_step node cannot run as configured or as the live
    schema now stands — see module docstring for the NodeExecutionError
    boundary."""


async def _noop_log(level: str, message: str) -> None:  # pragma: no cover - trivial default
    return None


async def resolve_mapping_version(
    db: AsyncSession, mapping_id: int, mapping_version: int, organization_id: int | None,
) -> ColumnMappingVersion:
    """The one place production pipeline execution is allowed to load a
    ColumnMappingVersion. Always re-checks PUBLISHED here — the whole
    reason a publish lifecycle exists (CM2) is so a pipeline can never
    silently run an unreviewed DRAFT, a not-yet-clean VALIDATED draft, or
    a withdrawn RETIRED version just because a stale node config still
    points at its version number."""
    version = await mapping_service.get_version(db, mapping_id, mapping_version)
    if version is None:
        raise MappingExecutionError(f"Column Mapping {mapping_id} version {mapping_version} was not found.")
    if version.status != "PUBLISHED":
        raise MappingExecutionError(
            f"Column Mapping {mapping_id} v{mapping_version} is {version.status}, not PUBLISHED — only a "
            f"published version can run in a production pipeline. Publish it from the Column Mapping module, "
            f"or attach a different version to this node."
        )
    return version


def check_source_target_consistency(
    version: ColumnMappingVersion, *,
    pipeline_source_connection_id: int | None = None, pipeline_source_schema: str | None = None, pipeline_source_object: str | None = None,
    pipeline_target_connection_id: int | None = None, pipeline_target_schema: str | None = None, pipeline_target_object: str | None = None,
) -> list[dict]:
    """Re-runs the same source/target comparison ColumnMappingConfig.jsx
    does client-side and api/pipeline_mapping.py's /attach does at attach
    time — repeated here, at the moment of actual execution, as
    defense-in-depth: a node's upstream Source Dataset or downstream
    Target could in principle have been edited after the mapping was
    attached without the node ever going through /attach again. Returns
    issues rather than raising — same as /attach's own behavior; the
    caller (pipeline_job_executor.py) decides whether to log a warning or
    treat a mismatch as fatal."""
    issues: list[dict] = []
    if pipeline_source_connection_id is not None and (
        pipeline_source_connection_id, pipeline_source_schema, pipeline_source_object
    ) != (version.source_connection_id, version.source_schema, version.source_object):
        issues.append({"side": "source", "message": "This node's upstream Source Dataset no longer matches the object this mapping version was built against."})
    if pipeline_target_connection_id is not None and (
        pipeline_target_connection_id, pipeline_target_schema, pipeline_target_object
    ) != (version.target_connection_id, version.target_schema, version.target_object):
        issues.append({"side": "target", "message": "This node's downstream Target no longer matches the object this mapping version was built against."})
    return issues


async def _resolve_connector_cached(db: AsyncSession, connection_id: int, organization_id: int | None, cache: dict):
    """Mirrors pipeline_job_executor._resolve_connector()'s cache shape
    exactly (same connector_cache dict is threaded in from the executor),
    duplicated rather than imported to keep this module import-cycle-free
    — see module docstring."""
    if connection_id in cache:
        return cache[connection_id]
    conn = await connection_service.get_connection(db, connection_id, organization_id)
    if conn is None:
        raise MappingExecutionError(f"Connection {connection_id} was not found or no longer belongs to this organization.")
    connector = connection_service.build_connector(conn)
    cache[connection_id] = (conn, connector)
    return conn, connector


async def _apply_lookup(db: AsyncSession, df: pd.DataFrame, m: dict, organization_id: int | None, connector_cache: dict, version: ColumnMappingVersion) -> pd.Series:
    lookup = m["lookup"]
    source_columns = m.get("source_columns") or []
    if not source_columns or source_columns[0] not in df.columns:
        raise MappingExecutionError(f"Lookup mapping for target column '{m['target_column']}' needs a resolvable source column to join on.")
    key_col = source_columns[0]
    _conn, connector = await _resolve_connector_cached(db, version.source_connection_id, organization_id, connector_cache)
    try:
        result = await asyncio.to_thread(connector.preview_object, lookup["table"], None, LOOKUP_FETCH_CAP)
    except ConnectorError as exc:
        raise MappingExecutionError(f"Could not read lookup table '{lookup['table']}' for target column '{m['target_column']}': {connector._scrub(str(exc))}") from exc
    lookup_df = pd.DataFrame(result.rows, columns=result.columns)
    if lookup["key_column"] not in lookup_df.columns or lookup["return_column"] not in lookup_df.columns:
        raise MappingExecutionError(
            f"Lookup table '{lookup['table']}' is missing key column '{lookup['key_column']}' or return column '{lookup['return_column']}' "
            f"for target column '{m['target_column']}'."
        )
    lookup_map = dict(zip(lookup_df[lookup["key_column"]], lookup_df[lookup["return_column"]]))
    return df[key_col].map(lookup_map)


async def apply_mapping_to_dataframe(
    db: AsyncSession, df: pd.DataFrame, version: ColumnMappingVersion, *,
    organization_id: int | None, connector_cache: dict, log: LogFn = _noop_log,
) -> tuple[pd.DataFrame, list[dict]]:
    """Applies every mapping row's real rule (lookup > constant_value >
    transformation > plain rename > NULL, then default_value as a
    COALESCE-style fallback — exactly the same precedence
    mapper.py::_row_expression() uses for the standalone module's real
    SQL preview, so a pipeline run and a standalone "Run Load" mean the
    same thing for the same mapping) to `df`, returning (mapped_df,
    rejects). `rejects` is [{"row": dict, "error": str}, ...] — the exact
    shape reject_service.record_rejects() already expects, so the caller
    can feed it straight through unchanged.

    Conversion-failure detection: for a lookup or transformation row, a
    row whose *source* value(s) were NOT null but whose *mapped* value
    came back null is a real conversion failure (an unmatched lookup key,
    or a transformation — e.g. CAST — that couldn't produce a value) —
    never confused with a row that was legitimately null to begin with.
    on_conversion_failure governs what happens to that row:
      FAIL_PIPELINE  — raises immediately, stops the whole job.
      REJECT_RECORD  — the row is dropped from the mapped output and
                       recorded in `rejects` (fed to reject_service by
                       the caller, same trail Run Load/Target already use).
      SET_NULL       — the row stays, with a null value in that column.
      USE_DEFAULT    — default_value (if the mapping row has one) already
                       filled the value before this check runs, so this
                       mode simply falls through to SET_NULL's behavior
                       when no default_value is configured — disclosed via
                       the same per-row null result, never silently invented.
    """
    df = df if df is not None else pd.DataFrame()
    mappings = version.mappings or []
    on_conversion_failure = version.on_conversion_failure or "REJECT_RECORD"
    unmapped_policy = version.unmapped_column_policy or "IGNORE"

    # Live re-validation as defense-in-depth: publish already requires a
    # clean VALIDATED result (CM2), but source/target schema can still
    # drift after publish. Blocks the whole run rather than mapping with
    # stale assumptions about columns that may no longer exist — this is
    # also where an unmapped NOT-NULL target column blocks pre-run (see
    # mapper.validate_mapping's own unmapped_required check).
    source_cols = mapping_service.columns_to_dicts(await mapping_service.fetch_columns(
        db, ObjectRef(connection_id=version.source_connection_id, schema_name=version.source_schema, object_name=version.source_object),
        organization_id,
    ))
    target_cols = mapping_service.columns_to_dicts(await mapping_service.fetch_columns(
        db, ObjectRef(connection_id=version.target_connection_id, schema_name=version.target_schema, object_name=version.target_object),
        organization_id,
    ))
    revalidation = mapper.validate_mapping(mappings, source_cols, target_cols)
    if revalidation["overall_status"] == "CONFLICT":
        blocking = [i["message"] for i in revalidation["issues"] if i["severity"] == "ERROR"]
        raise MappingExecutionError(
            "This mapping version no longer matches its live source/target schema and cannot run: " + "; ".join(blocking[:5])
        )

    mapped_targets = {m["target_column"] for m in mappings}
    unmapped_targets = [c for c in target_cols if c["name"] not in mapped_targets]
    if unmapped_targets:
        names = ", ".join(c["name"] for c in unmapped_targets)
        if unmapped_policy == "ERROR":
            raise MappingExecutionError(f"unmapped_column_policy is ERROR and the following target column(s) have no mapping: {names}.")
        if unmapped_policy == "WARNING":
            await log("WARN", f"{len(unmapped_targets)} target column(s) have no mapping and will be left out of the write: {names}.")
        # IGNORE: silently proceed — those target columns keep whatever their own DB default/NULL is.

    out = pd.DataFrame(index=df.index)
    failed_mask_total = pd.Series([False] * len(df), index=df.index)
    fail_reasons: dict = {}

    for m in mappings:
        target = m["target_column"]
        source_columns = [c for c in (m.get("source_columns") or []) if c in df.columns]
        pre_null_mask = pd.Series([False] * len(df), index=df.index)
        for c in source_columns:
            pre_null_mask = pre_null_mask | df[c].isna()

        is_derived = bool(m.get("lookup")) or bool(m.get("transformation"))

        if m.get("lookup"):
            series = await _apply_lookup(db, df, m, organization_id, connector_cache, version)
        elif m.get("constant_value") is not None:
            series = pd.Series([m["constant_value"]] * len(df), index=df.index)
        elif m.get("transformation"):
            try:
                series = transform_engine.evaluate_expression(df, m["transformation"])
            except transform_engine.TransformEvaluationError as exc:
                raise MappingExecutionError(f"Transformation for target column '{target}' could not be evaluated: {exc}") from exc
        elif source_columns:
            series = df[source_columns[0]]
        else:
            series = pd.Series([None] * len(df), index=df.index)

        if m.get("default_value") is not None:
            series = series.where(series.notna(), m["default_value"])

        if is_derived and len(df) > 0:
            post_null_mask = series.isna()
            row_failed_mask = post_null_mask & ~pre_null_mask
            if row_failed_mask.any():
                if on_conversion_failure == "FAIL_PIPELINE":
                    raise MappingExecutionError(
                        f"{int(row_failed_mask.sum())} row(s) failed the mapping for target column '{target}' "
                        f"(lookup miss or transformation could not produce a value) and on_conversion_failure is FAIL_PIPELINE."
                    )
                if on_conversion_failure == "REJECT_RECORD":
                    failed_mask_total = failed_mask_total | row_failed_mask
                    for idx in df.index[row_failed_mask]:
                        fail_reasons.setdefault(idx, []).append(f"'{target}': lookup/transformation produced no value")
                # SET_NULL / USE_DEFAULT-with-no-default-configured: value is already null in `series` — no-op.
        out[target] = series

    rejects: list[dict] = []
    if failed_mask_total.any():
        reject_df = df.loc[failed_mask_total]
        for idx in reject_df.index:
            rejects.append({"row": reject_df.loc[idx].to_dict(), "error": "; ".join(fail_reasons.get(idx, ["mapping conversion failed"]))})
        out = out.loc[~failed_mask_total]

    return out.reset_index(drop=True), rejects
