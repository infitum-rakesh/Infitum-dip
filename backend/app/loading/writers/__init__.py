"""
loading/writers — BulkWriter implementations (Phase 6: Target Loading).

Each writer takes a live database connector (already built, pooled — see
connectors/database/base.py) and writes batches of rows to a real target
table via a genuine vendor-appropriate bulk mechanism, chosen for
correctness over claiming the single fastest possible path — see each
module's docstring for the specific tradeoff and why. No writer here ever
creates or alters a target table's schema; a missing/mismatched target is
a real, surfaced error, never silently worked around.
"""
