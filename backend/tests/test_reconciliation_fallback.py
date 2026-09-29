"""
Automated tests for app/reconciliation/engine.py's ID->NAME fallback
matching (spec sections 12-18, session Phase 2 / RC1-RC5) — written in
Phase 7 to close the gap RC5 left: that work was verified live via curl
only, never captured as a repeatable pytest suite.

Exercises the real diff_keys_and_fields() function directly (the actual
code under test, unmodified) against a lightweight in-memory RowSource
test double. This double is a legitimate fixture — real production code
already treats RowSource as an interface with two concrete
implementations (ConnectorRowSource, FileRowSource); this is a third,
test-only implementation, not a mock of diff_keys_and_fields() itself.
"""
import time

from app.reconciliation.engine import (
    MAX_MATCHED_SAMPLE,
    MAX_RECONCILE_ROWS,
    RowSource,
    diff_keys_and_fields,
)


class InMemoryRowSource(RowSource):
    """A RowSource backed by an in-memory list[dict] — same interface
    contract as ConnectorRowSource/FileRowSource, so diff_keys_and_fields()
    can't tell the difference."""

    def __init__(self, rows: list[dict], name: str = "test"):
        self.rows = rows
        self.name = name

    def count(self) -> int:
        return len(self.rows)

    def get_columns(self) -> list[str]:
        return list(self.rows[0].keys()) if self.rows else []

    def iter_rows(self, columns: list[str], chunk_size: int = 5000):
        for i in range(0, len(self.rows), chunk_size):
            chunk = self.rows[i : i + chunk_size]
            yield [{c: r.get(c) for c in columns} for r in chunk]

    def describe(self) -> str:
        return f"'{self.name}'"


def test_exact_spec_worked_example():
    """The exact scenario from the spec (sections 12-18): SOURCE(id=1,
    name="ant pvt")/(id=2,name="mng ltd")/(id=3,name="uj ind") vs
    TARGET(id=1,name="ant pvt")/(id=5,name="mng ltd")/(id=8,name="uj ind")
    must produce MATCH_BY_ID for record 1 and MATCH_BY_NAME for records 2
    and 3 — never MISSING merely because ids differ."""
    source = InMemoryRowSource([
        {"id": 1, "name": "ant pvt"},
        {"id": 2, "name": "mng ltd"},
        {"id": 3, "name": "uj ind"},
    ], "source")
    target = InMemoryRowSource([
        {"id": 1, "name": "ant pvt"},
        {"id": 5, "name": "mng ltd"},
        {"id": 8, "name": "uj ind"},
    ], "target")

    result = diff_keys_and_fields(
        source, target, key_columns=["id"], compare_columns=["name"],
        fallback_columns=["name"],
    )

    assert result["matched_by_id_count"] == 1
    assert result["matched_by_name_count"] == 2
    assert result["missing_keys"] == set()
    assert result["extra_keys"] == set()
    assert result["ambiguous"] == []

    by_name_sources = {m["source_key"] for m in result["matched_by_name"]}
    assert by_name_sources == {(2,), (3,)}
    name_by_source = {m["source_key"]: m["target_key"] for m in result["matched_by_name"]}
    assert name_by_source[(2,)] == (5,)
    assert name_by_source[(3,)] == (8,)


def test_matched_sample_contains_clean_matches_excludes_mismatches():
    """matched_sample (RC-FIX: "view matched records") must surface every
    clean match (by ID and by name) with its compare-column values, but a
    key whose compare column actually differs must be excluded — that key
    already gets its own, more useful MISMATCH row instead."""
    source = InMemoryRowSource([
        {"id": 1, "name": "ant pvt", "amount": 100},   # clean id match
        {"id": 2, "name": "mng ltd", "amount": 50},    # clean name match
        {"id": 3, "name": "uj ind", "amount": 75},     # id match but amount differs -> MISMATCH, excluded
    ], "source")
    target = InMemoryRowSource([
        {"id": 1, "name": "ant pvt", "amount": 100},
        {"id": 5, "name": "mng ltd", "amount": 50},
        {"id": 3, "name": "uj ind", "amount": 999},
    ], "target")

    result = diff_keys_and_fields(
        source, target, key_columns=["id"], compare_columns=["amount"],
        fallback_columns=["name"],
    )

    assert len(result["mismatches"]) == 1
    assert result["mismatches"][0]["key"] == (3,)

    sample_keys = {s["source_key"] for s in result["matched_sample"]}
    assert sample_keys == {(1,), (2,)}
    assert (3,) not in sample_keys  # excluded — it has a mismatch

    by_key = {s["source_key"]: s for s in result["matched_sample"]}
    assert by_key[(1,)]["match_method"] == "ID"
    assert by_key[(1,)]["columns"]["amount"] == (100, 100)
    assert by_key[(2,)]["match_method"] == "NAME"
    assert by_key[(2,)]["target_key"] == (5,)
    assert by_key[(2,)]["columns"]["amount"] == (50, 50)


def test_matched_sample_capped_at_max_matched_sample():
    """Real, disclosed cap (100) — a run with far more than 100 clean
    matches must still report the true matched_by_id_count in full, but
    the sample itself never exceeds MAX_MATCHED_SAMPLE."""
    n = MAX_MATCHED_SAMPLE + 50
    source = InMemoryRowSource([{"id": i, "name": f"n{i}"} for i in range(n)], "source")
    target = InMemoryRowSource([{"id": i, "name": f"n{i}"} for i in range(n)], "target")

    result = diff_keys_and_fields(source, target, key_columns=["id"], compare_columns=["name"])

    assert result["matched_by_id_count"] == n
    assert len(result["matched_sample"]) == MAX_MATCHED_SAMPLE


def test_ambiguous_duplicate_target_names_never_guessed():
    """Two target rows share a normalized name for a source record whose
    id doesn't match either — must land in `ambiguous`, never silently
    picked as a match, and must NOT be reported as a false MISSING or a
    false EXTRA on either candidate."""
    source = InMemoryRowSource([{"id": 1, "name": "acme corp"}], "source")
    target = InMemoryRowSource([
        {"id": 9, "name": "acme corp"},
        {"id": 10, "name": "acme corp"},
    ], "target")

    result = diff_keys_and_fields(
        source, target, key_columns=["id"], compare_columns=["name"],
        fallback_columns=["name"],
    )

    assert result["matched_by_id_count"] == 0
    assert result["matched_by_name_count"] == 0
    assert result["missing_keys"] == set()
    assert result["extra_keys"] == set()
    assert len(result["ambiguous"]) == 1
    amb = result["ambiguous"][0]
    assert amb["source_key"] == (1,)
    assert set(amb["target_keys"]) == {(9,), (10,)}


def test_duplicate_target_key_never_reported_as_clean_id_match():
    """Real bug found live: the TARGET object has two rows sharing the same
    key (id=1) — a source row with id=1 must NOT be silently matched
    against whichever of the two target rows happened to be read last.
    It must land in duplicate_target_matches, be excluded from
    matched_by_id_count, and never counted as 'extra' either (the id does
    genuinely exist on the target, just ambiguously)."""
    source = InMemoryRowSource([{"id": 1, "name": "ant pvt"}], "source")
    target = InMemoryRowSource([
        {"id": 1, "name": "ant pvt"},
        {"id": 1, "name": "ant pvt (dup)"},
    ], "target")

    result = diff_keys_and_fields(
        source, target, key_columns=["id"], compare_columns=["name"],
    )

    assert result["matched_by_id_count"] == 0
    assert result["matched_count"] == 0
    assert result["missing_keys"] == set()
    assert result["extra_keys"] == set()
    assert result["mismatches"] == []
    assert len(result["duplicate_target_matches"]) == 1
    assert result["duplicate_target_matches"][0] == {"source_key": (1,), "target_key": (1,)}


def test_duplicate_target_key_detected_independent_of_fallback_matching():
    """Duplicate-target-key detection isn't gated behind
    fallback_match_columns — it's a target-data-quality issue orthogonal
    to ID->NAME fallback, so it must fire even when fallback columns ARE
    configured, without corrupting the name-matching outcome for OTHER,
    genuinely-unrelated source records."""
    source = InMemoryRowSource([
        {"id": 1, "name": "ant pvt"},
        {"id": 2, "name": "mng ltd"},
    ], "source")
    target = InMemoryRowSource([
        {"id": 1, "name": "ant pvt"},
        {"id": 1, "name": "ant pvt (dup)"},
        {"id": 5, "name": "mng ltd"},
    ], "target")

    result = diff_keys_and_fields(
        source, target, key_columns=["id"], compare_columns=["name"],
        fallback_columns=["name"],
    )

    assert len(result["duplicate_target_matches"]) == 1
    assert result["duplicate_target_matches"][0]["source_key"] == (1,)
    assert result["matched_by_id_count"] == 0
    assert result["matched_by_name_count"] == 1
    assert result["matched_by_name"][0]["source_key"] == (2,)
    assert result["matched_by_name"][0]["target_key"] == (5,)
    assert result["missing_keys"] == set()
    assert result["extra_keys"] == set()


def test_duplicate_source_key_does_not_trigger_duplicate_target_match():
    """Only TARGET-side duplicates are DUPLICATE_TARGET_MATCH — a
    duplicate key on the SOURCE side is a different (out of scope here)
    data-quality question and must not spuriously flag this."""
    source = InMemoryRowSource([
        {"id": 1, "name": "a"},
        {"id": 1, "name": "a again"},
    ], "source")
    target = InMemoryRowSource([{"id": 1, "name": "a"}], "target")

    result = diff_keys_and_fields(
        source, target, key_columns=["id"], compare_columns=["name"],
    )

    assert result["duplicate_target_matches"] == []
    assert result["matched_by_id_count"] == 1


def test_genuinely_missing_with_no_name_candidate():
    source = InMemoryRowSource([{"id": 1, "name": "ghost co"}], "source")
    target = InMemoryRowSource([{"id": 2, "name": "unrelated co"}], "target")

    result = diff_keys_and_fields(
        source, target, key_columns=["id"], compare_columns=["name"],
        fallback_columns=["name"],
    )

    assert result["missing_keys"] == {(1,)}
    assert result["extra_keys"] == {(2,)}
    assert result["matched_by_name_count"] == 0


def test_no_fallback_configured_is_byte_identical_to_old_behavior():
    """Every job created before this feature existed (fallback_columns
    unset) must behave EXACTLY as before: a name-only match across
    different ids is reported as MISSING/EXTRA, not silently upgraded."""
    source = InMemoryRowSource([
        {"id": 1, "name": "ant pvt"},
        {"id": 2, "name": "mng ltd"},
    ], "source")
    target = InMemoryRowSource([
        {"id": 1, "name": "ant pvt"},
        {"id": 5, "name": "mng ltd"},
    ], "target")

    result = diff_keys_and_fields(
        source, target, key_columns=["id"], compare_columns=["name"],
    )

    assert result["matched_by_id_count"] == 1
    assert result["matched_by_name_count"] == 0
    assert result["matched_by_name"] == []
    assert result["ambiguous"] == []
    assert result["missing_keys"] == {(2,)}
    assert result["extra_keys"] == {(5,)}


def test_name_normalization_trim_and_case_insensitive_by_default():
    source = InMemoryRowSource([{"id": 1, "name": "  Ant Pvt  "}], "source")
    target = InMemoryRowSource([{"id": 9, "name": "ant pvt"}], "target")

    result = diff_keys_and_fields(
        source, target, key_columns=["id"], compare_columns=["name"],
        fallback_columns=["name"],
    )
    assert result["matched_by_name_count"] == 1


def test_name_normalization_case_sensitive_when_configured_off():
    source = InMemoryRowSource([{"id": 1, "name": "Ant Pvt"}], "source")
    target = InMemoryRowSource([{"id": 9, "name": "ant pvt"}], "target")

    result = diff_keys_and_fields(
        source, target, key_columns=["id"], compare_columns=["name"],
        fallback_columns=["name"], name_normalization={"case_insensitive": False},
    )
    # Case-sensitive comparison: different case, must NOT match by name.
    assert result["matched_by_name_count"] == 0
    assert result["missing_keys"] == {(1,)}


def test_mismatch_detection_reports_correct_match_method():
    source = InMemoryRowSource([{"id": 1, "name": "ant pvt", "amount": 100}], "source")
    target = InMemoryRowSource([{"id": 1, "name": "ant pvt", "amount": 200}], "target")

    result = diff_keys_and_fields(
        source, target, key_columns=["id"], compare_columns=["name", "amount"],
    )
    assert len(result["mismatches"]) == 1
    mismatch = result["mismatches"][0]
    assert mismatch["column"] == "amount"
    assert mismatch["match_method"] == "ID"
    assert mismatch["source_value"] == 100
    assert mismatch["target_value"] == 200


def test_mismatch_detection_on_a_name_matched_pair():
    source = InMemoryRowSource([{"id": 1, "name": "ant pvt", "amount": 100}], "source")
    target = InMemoryRowSource([{"id": 9, "name": "ant pvt", "amount": 999}], "target")

    result = diff_keys_and_fields(
        source, target, key_columns=["id"], compare_columns=["name", "amount"],
        fallback_columns=["name"],
    )
    assert result["matched_by_name_count"] == 1
    assert len(result["mismatches"]) == 1
    assert result["mismatches"][0]["match_method"] == "NAME"
    assert result["mismatches"][0]["target_key"] == (9,)


def test_blank_fallback_value_never_participates_in_name_matching():
    """A row with a blank/None fallback value can't be claimed by name —
    it must stay in missing/extra, not silently match another blank."""
    source = InMemoryRowSource([{"id": 1, "name": None}], "source")
    target = InMemoryRowSource([{"id": 2, "name": None}], "target")

    result = diff_keys_and_fields(
        source, target, key_columns=["id"], compare_columns=["name"],
        fallback_columns=["name"],
    )
    assert result["matched_by_name_count"] == 0
    assert result["missing_keys"] == {(1,)}
    assert result["extra_keys"] == {(2,)}


def test_id_matched_row_cannot_also_be_claimed_by_name():
    """An id-matched target row is excluded from the name-index — it
    can't double as a different source record's name-match partner."""
    source = InMemoryRowSource([
        {"id": 1, "name": "dup name"},
        {"id": 2, "name": "dup name"},
    ], "source")
    target = InMemoryRowSource([
        {"id": 1, "name": "dup name"},
    ], "target")

    result = diff_keys_and_fields(
        source, target, key_columns=["id"], compare_columns=["name"],
        fallback_columns=["name"],
    )
    assert result["matched_by_id_count"] == 1
    assert result["matched_by_name_count"] == 0
    assert result["missing_keys"] == {(2,)}


def test_column_mapping_target_uses_different_names():
    """target_key_columns/target_compare_columns let the target side use
    different real column names for the same logical fields — a mapped
    column and its source counterpart line up by position."""
    source = InMemoryRowSource([{"src_id": 1, "src_name": "ant pvt"}], "source")
    target = InMemoryRowSource([{"tgt_id": 5, "tgt_name": "ant pvt"}], "target")

    result = diff_keys_and_fields(
        source, target,
        key_columns=["src_id"], compare_columns=["src_name"],
        target_key_columns=["tgt_id"], target_compare_columns=["tgt_name"],
        fallback_columns=["src_name"], target_fallback_columns=["tgt_name"],
    )
    assert result["matched_by_name_count"] == 1
    assert result["matched_by_name"][0]["target_key"] == (5,)


def test_performance_bulk_hash_join_scales_linearly_no_n_plus_1():
    """Real scale check (spec item 30: performance testing) — 20,000 rows
    per side with a realistic mix of id-matches, name-fallback-matches,
    and genuine misses, comfortably under MAX_RECONCILE_ROWS, must
    complete in well under a second if it's truly one bulk hash-map join
    per side (not, e.g., an O(n*m) nested-loop comparison)."""
    n = 20_000
    assert n < MAX_RECONCILE_ROWS

    source_rows = []
    target_rows = []
    for i in range(n):
        name = f"company-{i}"
        source_rows.append({"id": i, "name": name})
        if i % 3 == 0:
            target_rows.append({"id": i, "name": name})          # id-match
        elif i % 3 == 1:
            target_rows.append({"id": i + 1_000_000, "name": name})  # name-fallback match
        # i % 3 == 2 -> genuinely missing from target

    source = InMemoryRowSource(source_rows, "source")
    target = InMemoryRowSource(target_rows, "target")

    start = time.perf_counter()
    result = diff_keys_and_fields(
        source, target, key_columns=["id"], compare_columns=["name"],
        fallback_columns=["name"],
    )
    elapsed = time.perf_counter() - start

    expected_id = sum(1 for i in range(n) if i % 3 == 0)
    expected_name = sum(1 for i in range(n) if i % 3 == 1)
    expected_missing = sum(1 for i in range(n) if i % 3 == 2)

    assert result["matched_by_id_count"] == expected_id
    assert result["matched_by_name_count"] == expected_name
    assert len(result["missing_keys"]) == expected_missing
    assert result["extra_keys"] == set()
    # A true O(n) bulk hash-join over 20k+20k rows is milliseconds; a
    # regression to nested-loop comparison would take minutes. 5s is a
    # generous ceiling that only a real algorithmic regression would miss.
    assert elapsed < 5.0, f"diff_keys_and_fields took {elapsed:.2f}s for {n} rows/side — possible N+1/nested-loop regression"


def test_over_max_reconcile_rows_refuses_outright():
    """MAX_RECONCILE_ROWS is a hard, disclosed limit — past it this must
    refuse outright, never silently sample or truncate."""
    from app.reconciliation.engine import ReconciliationEngineError

    class HugeRowSource(RowSource):
        def count(self):
            return MAX_RECONCILE_ROWS + 1

        def get_columns(self):
            return ["id"]

        def iter_rows(self, columns, chunk_size=5000):
            total = MAX_RECONCILE_ROWS + 1
            produced = 0
            while produced < total:
                n = min(chunk_size, total - produced)
                yield [{"id": produced + j} for j in range(n)]
                produced += n

        def describe(self):
            return "'huge'"

    source = HugeRowSource()
    target = InMemoryRowSource([{"id": 1}], "target")

    try:
        diff_keys_and_fields(source, target, key_columns=["id"], compare_columns=[])
        assert False, "expected ReconciliationEngineError for over-limit source"
    except ReconciliationEngineError as exc:
        assert "more than" in str(exc)
