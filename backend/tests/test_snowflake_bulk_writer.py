"""
Automated test for app/loading/writers/snowflake.py — written in Phase 13
to lock in the real bug found and fixed by Phase 13's static review (no
live Snowflake account exists in this environment, see the module's own
docstring).

Fakes stand in for the SQLAlchemy engine/raw connection and
write_pandas() — the bug being tested is pure connection-management
control flow (does the upsert path keep every statement on the ONE
session that owns the TEMPORARY staging table), not the real Snowflake
wire protocol, which the module docstring already discloses as
unverifiable here.
"""
import pytest

import app.loading.writers.snowflake as sf_writer_module
from app.loading.writers.snowflake import SnowflakeBulkWriter


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql):
        self._conn.executed_sql.append(sql)

    def close(self):
        pass


class _FakeDbapiConnection:
    def __init__(self):
        self.executed_sql: list[str] = []
        self.committed = False
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


class _FakeRawConnection:
    def __init__(self, dbapi_conn):
        self.dbapi_connection = dbapi_conn
        self.closed = False

    def close(self):
        self.closed = True


class _FakeEngine:
    def __init__(self, dbapi_conn):
        self._dbapi_conn = dbapi_conn
        self.begin_called = False
        self.last_raw: _FakeRawConnection | None = None

    def raw_connection(self):
        self.last_raw = _FakeRawConnection(self._dbapi_conn)
        return self.last_raw

    def begin(self):
        # Real bug regression guard: engine.begin() checks out a DIFFERENT
        # pooled connection than the one raw_connection() is still
        # holding open — a Snowflake TEMPORARY staging table created via
        # write_pandas(dbapi_conn, ...) would not be visible to a MERGE/
        # DROP issued on that other session. write_batch() must never
        # reach here for the upsert path.
        self.begin_called = True
        raise AssertionError("engine.begin() must not be used for the upsert MERGE/DROP.")


class _FakeConnector:
    def __init__(self, engine_obj):
        self._engine_obj = engine_obj

    def engine(self, pool_size=2):
        return self._engine_obj

    def quote_object(self, table, schema):
        return f'"{schema}"."{table}"' if schema else f'"{table}"'

    def quote_identifier(self, name):
        return f'"{name}"'


def test_upsert_runs_merge_and_drop_on_the_same_dbapi_connection_as_write_pandas(monkeypatch):
    dbapi_conn = _FakeDbapiConnection()
    engine = _FakeEngine(dbapi_conn)
    connector = _FakeConnector(engine)
    writer = SnowflakeBulkWriter(connector)

    def fake_write_pandas(conn, df, table_name, **kwargs):
        assert conn is dbapi_conn  # write_pandas must run on the raw dbapi connection
        return True, None, len(df), None

    monkeypatch.setattr(sf_writer_module, "write_pandas", fake_write_pandas)

    rows = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    result = writer.write_batch("PUBLIC", "TARGET", ["id", "name"], rows, key_columns=["id"])

    assert result == 2
    assert engine.begin_called is False  # the regression this test guards against
    assert any("MERGE INTO" in sql for sql in dbapi_conn.executed_sql)
    assert any(sql.startswith("DROP TABLE IF EXISTS") for sql in dbapi_conn.executed_sql)
    assert dbapi_conn.committed is True
    assert engine.last_raw.closed is True  # raw.close() in write_batch()'s outer finally


def test_upsert_still_drops_staging_table_when_merge_fails(monkeypatch):
    dbapi_conn = _FakeDbapiConnection()
    engine = _FakeEngine(dbapi_conn)
    connector = _FakeConnector(engine)
    writer = SnowflakeBulkWriter(connector)

    monkeypatch.setattr(sf_writer_module, "write_pandas", lambda conn, df, table_name, **kw: (True, None, len(df), None))

    real_execute = _FakeCursor.execute
    def failing_execute(self, sql):
        if sql.startswith("MERGE INTO"):
            raise RuntimeError("simulated Snowflake MERGE failure")
        real_execute(self, sql)
    monkeypatch.setattr(_FakeCursor, "execute", failing_execute)

    with pytest.raises(RuntimeError):
        writer.write_batch("PUBLIC", "TARGET", ["id"], [{"id": 1}], key_columns=["id"])

    assert any(sql.startswith("DROP TABLE IF EXISTS") for sql in dbapi_conn.executed_sql)
    assert engine.last_raw.closed is True


def test_append_path_does_not_touch_engine_begin(monkeypatch):
    dbapi_conn = _FakeDbapiConnection()
    engine = _FakeEngine(dbapi_conn)
    connector = _FakeConnector(engine)
    writer = SnowflakeBulkWriter(connector)

    monkeypatch.setattr(sf_writer_module, "write_pandas", lambda conn, df, table_name, **kw: (True, None, len(df), None))

    result = writer.write_batch("PUBLIC", "TARGET", ["id"], [{"id": 1}])
    assert result == 1
    assert engine.begin_called is False
    assert engine.last_raw.closed is True
