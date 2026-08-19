"""
validation — real data-quality rule checking (Phase 7). Pure SQL-building
logic (rules.py, no I/O) plus the orchestrator that turns a rule set into
ONE real aggregate query and runs it via a live connector (engine.py).
"""
