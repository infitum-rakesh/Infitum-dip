"""
execution — the real backend pipeline execution engine (Phase 3:
streaming ingestion). Runs as an asyncio background task per run
(pipeline_executor.start_run), never inside a request/response cycle and
never in the browser — see pipeline_executor.py module docstring for the
current scope boundary and its single-process limitation.
"""
