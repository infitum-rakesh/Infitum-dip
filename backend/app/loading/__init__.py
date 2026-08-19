"""
loading — where extracted chunks land. staging.py is the first real
writer in this codebase; a vendor-specific BulkWriter abstraction for
real target databases (SQL Server/Postgres/Snowflake/...) is a later
phase — see loading/staging.py module docstring for the current scope.
"""
