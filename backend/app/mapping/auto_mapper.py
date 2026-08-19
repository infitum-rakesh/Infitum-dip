"""
auto_mapper — real, deterministic column-name matching for the Auto Map
button (spec section 14). Tiers, in order of confidence:

  1. exact            — identical characters
  2. case_insensitive  — identical ignoring case
  3. normalized        — identical after stripping non-alphanumerics and lowercasing
  4. alias             — both names normalize into the same entry of a
                          curated synonym dictionary (ALIAS_GROUPS below)
  5. similarity         — difflib.SequenceMatcher ratio on normalized names,
                          above a minimum threshold

Each tier is resolved GLOBALLY (across every remaining target) before the
next, weaker tier even looks at what's left — not per-target in
declaration order. That distinction matters: if target "CUSTOMER_KEY" is
processed before target "CUSTOMER_NAME" and only similarity-tier matches
exist for the former, a naive per-target-in-order pass would let
"CUSTOMER_KEY" greedily claim source column "CUSTOMER_NAME" via a so-so
similarity score BEFORE "CUSTOMER_NAME" (the target) ever gets a chance
at its own exact match against the identically-named source column. This
was caught live during Phase 4 testing — resolving strongest tiers first,
for every target at once, makes exact/case/normalized/alias matches
unconditionally win over a same-source similarity guess from a different
target, exactly like a human doing this by hand would.

Within the similarity tier itself, matching is a single global best-first
greedy assignment over every (source, target) pair above threshold —
not first-target-wins — for the same reason.

"AI-assisted matching" is listed in the spec as a further tier — it is
deliberately NOT implemented here. There is no LLM call wired into this
backend, and faking one with a canned response would violate the "do not
fake functionality" rule this whole build follows. Callers that want to
advertise this tier must say so honestly (see api/mapping.py response —
`ai_assisted_available: false`), not silently omit it.

Only produces MAPPED for the first three (near-certain) tiers; alias and
similarity always come back as SUGGESTED — the spec requires the user to
approve those explicitly. A name match with an INCOMPATIBLE data type is
downgraded to SUGGESTED regardless of tier: a plausible name is not a
substitute for a sane type conversion.

Never touches a target column that already has a mapping row, and never
proposes reusing a source column another proposal (or an existing
mapping) has already claimed — auto-map only ever proposes clean 1:1
matches; multi-column expressions require a human.
"""
from __future__ import annotations

import difflib
import re

from app.mapping.datatype_mapper import INCOMPATIBLE, compatibility

SIMILARITY_THRESHOLD = 0.72
_MAPPED_TIERS = {"exact", "case_insensitive", "normalized"}
_TIER_CONFIDENCE = {"exact": 100.0, "case_insensitive": 97.0, "normalized": 92.0, "alias": 85.0}

ALIAS_GROUPS = [
    {"id", "key", "identifier", "code"},
    {"name", "fullname"},
    {"firstname", "fname", "givenname"},
    {"lastname", "lname", "surname"},
    {"email", "emailaddress", "mail"},
    {"phone", "telephone", "phonenumber", "mobile", "mobilenumber"},
    {"address", "addr", "street", "streetaddress"},
    {"dob", "dateofbirth", "birthdate"},
    {"created", "createdat", "createddate"},
    {"updated", "updatedat", "modifieddate", "lastupdated"},
    {"amount", "total", "totalamount", "sum"},
    {"qty", "quantity"},
    {"desc", "description"},
    {"country", "countrycode"},
    {"zip", "zipcode", "postalcode"},
    {"status", "state"},
    {"active", "isactive", "enabled"},
    {"customer", "customerid", "custid"},
]
_ALIAS_LOOKUP = {member: group_id for group_id, group in enumerate(ALIAS_GROUPS) for member in group}


def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def auto_map(source_columns: list[dict], target_columns: list[dict], existing_mappings: list[dict] | None = None) -> list[dict]:
    """
    source_columns / target_columns: [{"name": ..., "data_type": ...}, ...]
    existing_mappings: current mapping rows — target columns they already
    cover, and source columns they already consume, are never touched.
    Returns a list of NEW proposed mapping rows (does not include
    existing_mappings — callers merge).
    """
    existing = existing_mappings or []
    claimed_targets = {m["target_column"] for m in existing}
    claimed_sources = {c for m in existing for c in (m.get("source_columns") or [])}

    remaining_sources = [s for s in source_columns if s["name"] not in claimed_sources]
    remaining_targets = [t for t in target_columns if t["name"] not in claimed_targets]

    matches: dict[str, tuple[dict, float, str]] = {}
    for tier, key_fn in (("exact", lambda n: n), ("case_insensitive", str.lower), ("normalized", normalize)):
        _resolve_key_tier(tier, key_fn, remaining_sources, remaining_targets, matches)
    _resolve_alias_tier(remaining_sources, remaining_targets, matches)
    _resolve_similarity_tier(remaining_sources, remaining_targets, matches)

    proposals = []
    for target in target_columns:
        if target["name"] not in matches:
            continue
        source, confidence, method = matches[target["name"]]
        compat = compatibility(source.get("data_type", ""), target.get("data_type", ""))
        status = "MAPPED" if method in _MAPPED_TIERS else "SUGGESTED"
        if compat["status"] == INCOMPATIBLE:
            status = "SUGGESTED"
            confidence = max(40.0, confidence - 30)
        proposals.append({
            "id": f"map_auto_{target['name']}",
            "source_columns": [source["name"]],
            "target_column": target["name"],
            "transformation": "",
            "constant_value": None,
            "default_value": None,
            "lookup": None,
            "status": status,
            "confidence": confidence,
            "match_method": method,
        })
    return proposals


def _resolve_key_tier(method: str, key_fn, remaining_sources: list[dict], remaining_targets: list[dict], matches: dict) -> None:
    """Resolves one exact/case-insensitive/normalized pass globally: every
    remaining target is checked against every remaining source under this
    tier's key function before anything is removed, so this tier can never
    itself pick a bad pairing order — collisions are inherently rare
    (distinct column names within one table), but ties resolve first-seen."""
    source_by_key: dict[str, dict] = {}
    for s in remaining_sources:
        source_by_key.setdefault(key_fn(s["name"]), s)

    still_targets = []
    for t in remaining_targets:
        src = source_by_key.get(key_fn(t["name"]))
        if src is not None:
            matches[t["name"]] = (src, _TIER_CONFIDENCE[method], method)
            remaining_sources.remove(src)
            del source_by_key[key_fn(t["name"])]
        else:
            still_targets.append(t)
    remaining_targets[:] = still_targets


def _resolve_alias_tier(remaining_sources: list[dict], remaining_targets: list[dict], matches: dict) -> None:
    source_by_group: dict[int, dict] = {}
    for s in remaining_sources:
        group = _ALIAS_LOOKUP.get(normalize(s["name"]))
        if group is not None:
            source_by_group.setdefault(group, s)

    still_targets = []
    for t in remaining_targets:
        group = _ALIAS_LOOKUP.get(normalize(t["name"]))
        src = source_by_group.get(group) if group is not None else None
        if src is not None:
            matches[t["name"]] = (src, _TIER_CONFIDENCE["alias"], "alias")
            remaining_sources.remove(src)
            del source_by_group[group]
        else:
            still_targets.append(t)
    remaining_targets[:] = still_targets


def _resolve_similarity_tier(remaining_sources: list[dict], remaining_targets: list[dict], matches: dict) -> None:
    """Global best-first greedy assignment: score every remaining
    (source, target) pair above threshold, then commit from the highest
    score down, skipping anything already claimed by a better-scoring
    pair — never first-candidate-wins."""
    candidates = []
    for t in remaining_targets:
        t_norm = normalize(t["name"])
        if not t_norm:
            continue
        for s in remaining_sources:
            s_norm = normalize(s["name"])
            if not s_norm:
                continue
            ratio = difflib.SequenceMatcher(None, s_norm, t_norm).ratio()
            if ratio >= SIMILARITY_THRESHOLD:
                candidates.append((ratio, s, t))
    candidates.sort(key=lambda c: c[0], reverse=True)

    used_sources: set[str] = set()
    used_targets: set[str] = set()
    for ratio, s, t in candidates:
        if s["name"] in used_sources or t["name"] in used_targets:
            continue
        matches[t["name"]] = (s, round(ratio * 78, 1), "similarity")  # capped below the alias tier — a guess, not a curated match
        used_sources.add(s["name"])
        used_targets.add(t["name"])
