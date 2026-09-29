"""
engine — the four real reconciliation comparison types.

COUNT and CHECKSUM stream in bounded chunks (via a RowSource — either a
real DB connector's own open_sql_stream, the exact same primitive
execution/worker.py's QUERY mode and Phase 6's load_worker.py already use,
or a real LocalFileConnector's open_row_stream for an uploaded file — see
RowSource below) and work at any object size. CHECKSUM combines each row's
SHA-256 hash with XOR — commutative and associative, so row order (which
can differ between two different database engines, or a file whose rows
just aren't in the same order as the DB it's being compared against) never
affects the result; two datasets with the same rows in any order produce
the same checksum, and any real difference in content changes it.

KEY_MATCH and FIELD_LEVEL need a real, correct diff — every source row's
key (and, for FIELD_LEVEL, its compare-column values) has to be
compared against every target row's, which means holding both sides in
memory at once. MAX_RECONCILE_ROWS is the real, disclosed limit that
implies: past it, this refuses outright rather than silently sampling or
truncating, which would report a wrong count and is exactly the kind of
faked correctness this build avoids elsewhere.
"""
from __future__ import annotations

import hashlib
import unicodedata

MAX_RECONCILE_ROWS = 200_000
# Real matched records were never persisted before (see ExceptionType.MATCHED's
# docstring) — this caps the sample diff_keys_and_fields() builds so viewing
# them can never approach MAX_RECONCILE_ROWS-scale memory/storage.
MAX_MATCHED_SAMPLE = 100


class ReconciliationEngineError(Exception):
    """Bad request, or the comparison genuinely can't be done as asked."""


class RowSource:
    """Abstracts over a real DB connector+object OR a real uploaded file —
    every aggregate primitive below (count/checksum/key-map) is written
    once against this interface and doesn't care which kind of source is
    actually behind it. Two concrete implementations below; nothing else
    in this module ever touches a connector directly."""

    def count(self) -> int:
        raise NotImplementedError

    def get_columns(self) -> list[str]:
        raise NotImplementedError

    def iter_rows(self, columns: list[str], chunk_size: int = 5000):
        """Yields list[dict] chunks — each dict has at least `columns`."""
        raise NotImplementedError

    def describe(self) -> str:
        """Human-readable identity for error messages, e.g. "'orders'" or
        "uploaded file 'orders.csv'"."""
        raise NotImplementedError


class ConnectorRowSource(RowSource):
    """A real DB (or SFTP/API/etc.) connection + schema + object — reads
    via real SQL, exactly as this module always has."""

    def __init__(self, connector, schema: str | None, object_name: str):
        self.connector = connector
        self.schema = schema
        self.object_name = object_name

    def count(self) -> int:
        base = self.connector.quote_object(self.object_name, self.schema)
        result = self.connector.preview_sql(f"SELECT COUNT(*) AS n FROM {base}", {}, 1)
        return int(result.rows[0][0] or 0)

    def get_columns(self) -> list[str]:
        return [c.name for c in self.connector.get_columns(self.object_name, self.schema)]

    def iter_rows(self, columns: list[str], chunk_size: int = 5000):
        quoted_cols = ", ".join(self.connector.quote_identifier(c) for c in columns)
        base = self.connector.quote_object(self.object_name, self.schema)
        stream = self.connector.open_sql_stream(f"SELECT {quoted_cols} FROM {base}", {})
        try:
            while True:
                rows = stream.fetch_chunk(chunk_size)
                if not rows:
                    break
                yield rows
        finally:
            stream.close()

    def describe(self) -> str:
        return f"'{self.object_name}'"


class FileRowSource(RowSource):
    """A real uploaded file, read via a real LocalFileConnector pointed at
    that upload's own sandboxed directory (services/uploaded_file_service.py
    builds the connector) — the same connectors/files/ streaming readers
    SFTP/Local pipeline sources already use. File connectors don't support
    SQL SELECT lists, so column projection happens in Python here instead
    of in the query, unlike ConnectorRowSource."""

    def __init__(self, connector, object_name: str):
        self.connector = connector
        self.object_name = object_name

    def count(self) -> int:
        n = 0
        stream = self.connector.open_row_stream(self.object_name)
        try:
            while True:
                rows = stream.fetch_chunk(5000)
                if not rows:
                    break
                n += len(rows)
        finally:
            stream.close()
        return n

    def get_columns(self) -> list[str]:
        return [c.name for c in self.connector.get_columns(self.object_name)]

    def iter_rows(self, columns: list[str], chunk_size: int = 5000):
        stream = self.connector.open_row_stream(self.object_name)
        try:
            while True:
                rows = stream.fetch_chunk(chunk_size)
                if not rows:
                    break
                yield [{c: r.get(c) for c in columns} for r in rows]
        finally:
            stream.close()

    def describe(self) -> str:
        return f"uploaded file '{self.object_name}'"


def compare_counts(source: RowSource) -> int:
    return source.count()


def _row_hash(values) -> int:
    parts = ["\x00NULL\x00" if v is None else str(v) for v in values]
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8", errors="replace")).digest()
    return int.from_bytes(digest[:8], "big")


def compute_checksum(source: RowSource, columns: list[str], chunk_size: int = 5000) -> tuple[str, int]:
    """Returns (hex_checksum, row_count). Order-independent, bounded
    memory regardless of object size — never holds more than one chunk."""
    if not columns:
        raise ReconciliationEngineError("Checksum comparison needs at least one column.")
    acc = 0
    count = 0
    for rows in source.iter_rows(columns, chunk_size):
        for row in rows:
            acc ^= _row_hash(tuple(row.get(c) for c in columns))
            count += 1
    return format(acc, "016x"), count


def _fetch_key_rows(source: RowSource, key_columns: list[str], extra_columns: list[str], chunk_size: int = 5000) -> tuple[dict, set]:
    """Bulk-loads this source into ONE hash map keyed by key_columns, each
    value the full row dict — a single pass, same MAX_RECONCILE_ROWS-bounded
    memory as the old tuple-only version this replaces, just keeping named
    field access instead of positional so callers can read multiple
    independent column groups (compare columns, fallback/name columns) out
    of the SAME fetched row without a second pass over the source. This is
    the one bulk primitive both the plain exact-key path and the ID->NAME
    fallback path below share — no per-row query either way (never the
    N+1 anti-pattern an ID/NAME reconciliation could otherwise fall into).

    Returns (rows_by_key, duplicate_keys). Real bug found live: a key that
    appears on more than one row (e.g. the TARGET table has two rows with
    the same id) used to be silently overwritten here — the later row won
    with no trace of the earlier one, and a source record matching that id
    was confidently reported MATCH_BY_ID even though which target row it
    actually matched was never decided. Now the FIRST row for a key is
    kept (deterministic) and every key seen more than once is collected in
    duplicate_keys so diff_keys_and_fields can report it as
    DUPLICATE_TARGET_MATCH instead of a clean match."""
    cols = list(dict.fromkeys(list(key_columns) + list(extra_columns)))
    result: dict = {}
    duplicate_keys: set = set()
    for rows in source.iter_rows(cols, chunk_size):
        for row in rows:
            key = tuple(row.get(c) for c in key_columns)
            if key in result:
                duplicate_keys.add(key)
            else:
                result[key] = row
            if len(result) > MAX_RECONCILE_ROWS:
                raise ReconciliationEngineError(
                    f"{source.describe()} has more than {MAX_RECONCILE_ROWS:,} rows — key/field-level "
                    "reconciliation isn't supported at this scale yet (Record Count and Checksum "
                    "comparisons still work at any scale)."
                )
    return result, duplicate_keys


def _normalize(v) -> str | None:
    return None if v is None else str(v).strip()


def _normalize_name(v, config: dict) -> str | None:
    """Name-fallback normalization (spec: trim + case-insensitive by
    default; collapse-repeated-spaces and Unicode NFKC normalization
    available but opt-in — never applied unless the job's
    name_normalization config asks for it, since either can change what a
    name compares equal to). Deliberately NOT fuzzy — an exact match on
    the normalized string is the whole fallback; fuzzy matching is
    explicitly out of scope until it ships as its own configured,
    reviewable feature (spec section 16)."""
    if v is None:
        return None
    s = str(v)
    if config.get("trim", True):
        s = s.strip()
    if config.get("collapse_spaces"):
        s = " ".join(s.split())
    if config.get("unicode_normalize"):
        s = unicodedata.normalize("NFKC", s)
    if config.get("case_insensitive", True):
        s = s.casefold()
    return s if s else None


def missing_columns(source: RowSource, columns: list[str]) -> list[str]:
    """Which of `columns` don't actually exist on this source — a real
    describe/reflection call, not a guess. Comparing two differently-shaped
    objects (a common real case — e.g. a legacy table vs its replacement,
    or an uploaded file vs a DB table) means a picked key/compare column
    can exist on one side and not the other; without this check that only
    surfaces as an opaque 'column does not exist' error from whichever
    side happens to be read second."""
    if not columns:
        return []
    existing = set(source.get_columns())
    return [c for c in columns if c not in existing]


def require_matching_columns(source: RowSource, source_columns: list[str], target: RowSource, target_columns: list[str]) -> None:
    """Raises a clear, specific ReconciliationEngineError naming exactly
    which column(s) are missing from which side, instead of letting a
    mismatched-schema comparison fail later with a raw driver error.
    `source_columns`/`target_columns` are already resolved through any
    column mapping — positionally aligned (same logical field at the same
    index), not necessarily the same names."""
    missing_source = missing_columns(source, source_columns)
    missing_target = missing_columns(target, target_columns)
    if not missing_source and not missing_target:
        return
    parts = []
    if missing_source:
        parts.append(f"source {source.describe()} is missing column(s): {', '.join(missing_source)}")
    if missing_target:
        parts.append(f"target {target.describe()} is missing column(s): {', '.join(missing_target)}")
    raise ReconciliationEngineError(
        "Column mismatch — " + "; ".join(parts) + ". Reconciliation requires the same column "
        "name(s) on both sides, or an explicit Column Mapping in Setup & Configuration — update "
        "Key Column(s) / Columns to Compare / Column Mapping."
    )


def diff_keys_and_fields(
    source: RowSource, target: RowSource,
    key_columns: list[str], compare_columns: list[str],
    target_key_columns: list[str] | None = None, target_compare_columns: list[str] | None = None,
    fallback_columns: list[str] | None = None, target_fallback_columns: list[str] | None = None,
    name_normalization: dict | None = None,
) -> dict:
    """Returns {source_count, target_count, matched_count,
    matched_by_id_count, matched_by_name_count, matched_by_name (list of
    {source_key, target_key, name, target_name}), ambiguous (list of
    {source_key, name, target_keys}), duplicate_target_matches (list of
    {source_key, target_key} — the target side has 2+ rows sharing this
    key, so an id-match against it is never trusted as a clean match),
    missing_keys, extra_keys (sets of key-tuples), mismatches (list of
    {key, column, source_value, target_value, match_method, target_key})}.

    `target_key_columns`/`target_compare_columns` default to the source
    names (the "no mapping picked" case) but let the target side read
    different real column names for the same logical fields — a mapped
    column and its source counterpart line up by position, not by name,
    so the returned key-tuples and mismatch `column` labels (always the
    SOURCE name) stay directly comparable either way.

    ID->NAME fallback matching (spec sections 12-17): when
    `fallback_columns` is empty/None (every job created before this
    existed, and any job that doesn't explicitly opt in), this function's
    behavior is IDENTICAL to the exact-key-only matching it always did —
    matched_by_name is always [], ambiguous is always [], missing_keys/
    extra_keys/mismatches are computed exactly as before. Only a job that
    sets fallback_match_columns exercises the extra matching pass below.

    Matching sequence when fallback_columns IS set:
      1. Exact key match (as always) — same bulk hash-map join, no N+1.
      2. For a source key NOT found by step 1, normalize its fallback-
         column value(s) and look it up in a name-index built from the
         target rows NOT already claimed by an id-match (also a single
         bulk pass, not a per-row query) —
         - exactly one candidate  -> MATCH_BY_NAME
         - two or more candidates -> AMBIGUOUS_MATCH (never auto-picked —
           spec section 15 explicitly forbids guessing among duplicates)
         - zero candidates        -> still MISSING (now genuinely missing,
           not just missing-by-id)
    A target row is only ever "extra" if no source row claimed it by
    either method — an ambiguous candidate is excluded from "extra" too
    (it may well be the real match; reporting it as a false extra would
    be exactly the kind of confidently-wrong result this exists to avoid).

    DUPLICATE_TARGET_MATCH (independent of fallback matching — this check
    always runs): if the TARGET has 2+ rows sharing the same key, a source
    row whose id matches that key is never counted as a clean id-match —
    which of the duplicate target rows it actually corresponds to was
    never decided, so reporting MATCH_BY_ID here would be confidently
    wrong. These keys are excluded from matched_count, from "extra" (the
    id does genuinely exist on both sides), and from field-level mismatch
    checking (comparing against an arbitrarily-picked one of several
    candidates isn't a real comparison).
    """
    if not key_columns:
        raise ReconciliationEngineError("Key/field-level comparison needs at least one key column.")
    target_key_columns = target_key_columns if target_key_columns is not None else key_columns
    target_compare_columns = target_compare_columns if target_compare_columns is not None else compare_columns
    fallback_columns = fallback_columns or []
    target_fallback_columns = target_fallback_columns if target_fallback_columns is not None else fallback_columns
    name_normalization = name_normalization or {}

    extra_source_cols = list(compare_columns) + list(fallback_columns)
    extra_target_cols = list(target_compare_columns) + list(target_fallback_columns)
    source_rows, _source_duplicate_keys = _fetch_key_rows(source, key_columns, extra_source_cols)
    target_rows, target_duplicate_keys = _fetch_key_rows(target, target_key_columns, extra_target_cols)
    source_keys, target_keys = set(source_rows), set(target_rows)

    id_matched_keys = source_keys & target_keys
    duplicate_target_matches_keys = id_matched_keys & target_duplicate_keys
    id_matched_keys = id_matched_keys - duplicate_target_matches_keys
    missing_by_id = source_keys - target_keys
    extra_by_id = target_keys - source_keys

    matched_by_name: list[dict] = []
    ambiguous: list[dict] = []
    missing_keys = set(missing_by_id)
    consumed_target_keys = set(id_matched_keys)
    ambiguous_target_keys: set = set()

    if fallback_columns:
        # Name-index built ONLY from target rows not already spoken for by
        # an id-match — a row that's already the id-match partner of one
        # source record can't also be claimed by name from a different one.
        name_index: dict[tuple, list] = {}
        for tk in extra_by_id:
            trow = target_rows[tk]
            norm = tuple(_normalize_name(trow.get(c), name_normalization) for c in target_fallback_columns)
            if any(part is None for part in norm):
                continue  # a blank/absent fallback value never participates in name matching
            name_index.setdefault(norm, []).append(tk)

        missing_keys = set()
        for sk in missing_by_id:
            srow = source_rows[sk]
            raw = tuple(srow.get(c) for c in fallback_columns)
            norm = tuple(_normalize_name(v, name_normalization) for v in raw)
            if any(part is None for part in norm):
                missing_keys.add(sk)
                continue
            candidates = name_index.get(norm, [])
            if len(candidates) == 1:
                tk = candidates[0]
                target_raw = tuple(target_rows[tk].get(c) for c in target_fallback_columns)
                matched_by_name.append({"source_key": sk, "target_key": tk, "name": raw, "target_name": target_raw})
                consumed_target_keys.add(tk)
            elif len(candidates) > 1:
                ambiguous.append({"source_key": sk, "name": raw, "target_keys": candidates})
                ambiguous_target_keys.update(candidates)
            else:
                missing_keys.add(sk)

    extra_keys = target_keys - consumed_target_keys - ambiguous_target_keys - duplicate_target_matches_keys

    mismatches = []

    def _emit_mismatches(skey, tkey, match_method):
        srow, trow = source_rows[skey], target_rows[tkey]
        for i, col in enumerate(compare_columns):
            sv, tv = srow.get(col), trow.get(target_compare_columns[i])
            if _normalize(sv) != _normalize(tv):
                mismatches.append({
                    "key": skey, "column": col, "source_value": sv, "target_value": tv,
                    "match_method": match_method, "target_key": tkey,
                })

    for k in id_matched_keys:
        _emit_mismatches(k, k, "ID")
    for m in matched_by_name:
        _emit_mismatches(m["source_key"], m["target_key"], "NAME")

    duplicate_target_matches = [{"source_key": k, "target_key": k} for k in duplicate_target_matches_keys]

    # A capped sample of CLEAN matches (no compare-column mismatch) — see
    # ExceptionType.MATCHED. Excludes any key that also appears in
    # `mismatches` (that key already gets its own, more useful MISMATCH row).
    mismatched_source_keys = {m["key"] for m in mismatches}
    matched_sample: list[dict] = []
    for k in id_matched_keys:
        if len(matched_sample) >= MAX_MATCHED_SAMPLE:
            break
        if k in mismatched_source_keys:
            continue
        srow, trow = source_rows[k], target_rows[k]
        matched_sample.append({
            "source_key": k, "target_key": k, "match_method": "ID",
            "columns": {c: (srow.get(c), trow.get(target_compare_columns[i])) for i, c in enumerate(compare_columns)},
        })
    for m in matched_by_name:
        if len(matched_sample) >= MAX_MATCHED_SAMPLE:
            break
        sk, tk = m["source_key"], m["target_key"]
        if sk in mismatched_source_keys:
            continue
        srow, trow = source_rows[sk], target_rows[tk]
        matched_sample.append({
            "source_key": sk, "target_key": tk, "match_method": "NAME",
            "name": m.get("name"), "target_name": m.get("target_name"),
            "columns": {c: (srow.get(c), trow.get(target_compare_columns[i])) for i, c in enumerate(compare_columns)},
        })

    return {
        "source_count": len(source_keys), "target_count": len(target_keys),
        "matched_count": len(id_matched_keys) + len(matched_by_name),
        "matched_by_id_count": len(id_matched_keys), "matched_by_name_count": len(matched_by_name),
        "matched_by_name": matched_by_name, "ambiguous": ambiguous,
        "duplicate_target_matches": duplicate_target_matches,
        "matched_sample": matched_sample,
        "missing_keys": missing_keys, "extra_keys": extra_keys,
        "mismatches": mismatches,
    }
