"""
Detector registry — anomaly/engine.py looks up which function to call
per configured detection method by name, rather than hardcoding an
if/elif chain.

Not every detector shares the same call signature (statistical/format/
duplicate scan row_source directly; categorical/distribution work off
already-profiled stats; timeseries needs a time column) — ALL_DETECTORS
exists for discoverability/defaults, not as a uniform dispatch table;
anomaly/engine.py calls each method's function with the arguments that
method actually needs.
"""
from __future__ import annotations

from app.anomaly.detectors.statistical import DETECTORS as _STATISTICAL, DEFAULT_PARAMS as _STATISTICAL_PARAMS
from app.anomaly.detectors.null_missing import DETECTORS as _NULL, DEFAULT_PARAMS as _NULL_PARAMS
from app.anomaly.detectors.format import DETECTORS as _FORMAT, DEFAULT_PARAMS as _FORMAT_PARAMS
from app.anomaly.detectors.categorical import DETECTORS as _CATEGORICAL, DEFAULT_PARAMS as _CATEGORICAL_PARAMS
from app.anomaly.detectors.duplicate import DETECTORS as _DUPLICATE, DEFAULT_PARAMS as _DUPLICATE_PARAMS
from app.anomaly.detectors.distribution import DETECTORS as _DISTRIBUTION, DEFAULT_PARAMS as _DISTRIBUTION_PARAMS
from app.anomaly.detectors.timeseries import DETECTORS as _TIMESERIES, DEFAULT_PARAMS as _TIMESERIES_PARAMS

ALL_DETECTORS = {**_STATISTICAL, **_NULL, **_FORMAT, **_CATEGORICAL, **_DUPLICATE, **_DISTRIBUTION, **_TIMESERIES}
ALL_DEFAULT_PARAMS = {
    **_STATISTICAL_PARAMS, **_NULL_PARAMS, **_FORMAT_PARAMS, **_CATEGORICAL_PARAMS,
    **_DUPLICATE_PARAMS, **_DISTRIBUTION_PARAMS, **_TIMESERIES_PARAMS,
}

# Methods that operate PER COLUMN, driven entirely by profiler stats
# (current + baseline) with no fresh row_source scan of their own —
# engine.py can run these cheaply for every column even at high fan-out.
STATS_ONLY_METHODS = {"CATEGORICAL"}  # DISTRIBUTION's categorical branch is stats-only too, but its numeric
                                        # branch needs a scan — engine.py special-cases DISTRIBUTION explicitly.

__all__ = ["ALL_DETECTORS", "ALL_DEFAULT_PARAMS", "STATS_ONLY_METHODS"]
