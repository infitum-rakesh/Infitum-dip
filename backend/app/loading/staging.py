"""
StagingWriter — the first real Writer in this codebase.

Phase 3 scope: streams each extracted chunk straight to a per-partition
newline-delimited JSON file on local disk. Rows are never accumulated in
memory beyond one chunk at a time — the same bounded-memory principle the
reader side (connectors/database/base.py) follows. This is deliberately
the simplest possible real target: proving the read -> chunk -> write
loop end to end without needing a vendor-specific bulk-load API yet (that
BulkWriter abstraction — SqlServerBulkWriter/PostgresBulkWriter/... writing
to a real target database via native bulk mechanisms — is a later phase;
see spec section 10).

One file per partition (staging/run_{id}/partition_{i}.jsonl) means
concurrent partition workers never contend on a single file handle, and
a resumed partition's append-mode write picks up exactly after the rows
its last successful checkpoint already covered — see execution/
checkpoint.py and worker.py for why last_key_read only advances after a
chunk's write call returns successfully, never before.
"""
from __future__ import annotations

import json
from pathlib import Path


def staging_dir(base_path: str, run_id: int) -> Path:
    d = Path(base_path) / f"run_{run_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def partition_file(base_path: str, run_id: int, partition_index: int) -> Path:
    return staging_dir(base_path, run_id) / f"partition_{partition_index}.jsonl"


def write_chunk(base_path: str, run_id: int, partition_index: int, rows: list[dict]) -> int:
    """Appends rows as newline-delimited JSON. Returns rows written.
    Synchronous/blocking — callers run this via asyncio.to_thread()."""
    if not rows:
        return 0
    path = partition_file(base_path, run_id, partition_index)
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")
    return len(rows)


def reset_partition(base_path: str, run_id: int, partition_index: int) -> None:
    """Truncates a partition's staging file — used only when a Restart
    (not Resume) is requested for a partition, so a from-scratch re-read
    doesn't duplicate-append onto whatever a prior attempt already wrote."""
    path = partition_file(base_path, run_id, partition_index)
    if path.exists():
        path.unlink()
