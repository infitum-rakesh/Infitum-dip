"""
DataFrameRowSource — AD10 (Pipeline Builder integration).

A third real RowSource implementation (alongside reconciliation.engine's
ConnectorRowSource and FileRowSource) — wraps an in-memory pandas
DataFrame, the exact same DataFrame object flowing through a running
Pipeline Builder job at the point an anomaly_detection_step node sits in
the graph. This is what makes the pipeline node's detection genuinely
real rather than a passthrough: anomaly/profiler.py's tier-2 streaming
path and every detector in anomaly/detectors/ are written once against
the RowSource interface and don't care which concrete source is behind
it — profiler.py's _sql_connector() only recognizes ConnectorRowSource
for its tier-1 SQL fast path, so a DataFrameRowSource always takes tier 2
(exact Welford mean/variance + sorted-sample percentiles over what's
actually in the DataFrame), never a fabricated aggregate.

No connector, no live database round-trip — reading a column here is
just a pandas slice, which is the honest reality of "check the data
flowing through this exact point in the pipeline" (works identically
for POST_LOAD, checking a just-loaded target's data, or PRE_LOAD,
checking data before it reaches the target).
"""
from __future__ import annotations

import pandas as pd

from app.reconciliation.engine import RowSource


class DataFrameRowSource(RowSource):
    def __init__(self, df: pd.DataFrame, label: str = "pipeline data"):
        self.df = df if df is not None else pd.DataFrame()
        self.label = label

    def count(self) -> int:
        return len(self.df)

    def get_columns(self) -> list[str]:
        return list(self.df.columns)

    def iter_rows(self, columns: list[str], chunk_size: int = 5000):
        n = len(self.df)
        if n == 0:
            return
        for start in range(0, n, chunk_size):
            chunk_records = self.df.iloc[start:start + chunk_size].to_dict("records")
            yield [{c: r.get(c) for c in columns} for r in chunk_records]

    def describe(self) -> str:
        return f"'{self.label}'"
