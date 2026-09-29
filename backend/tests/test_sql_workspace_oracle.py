from unittest.mock import MagicMock
from app.connectors.database.oracle import OracleConnector
from app.connectors.database.base import SqlAlchemyDatabaseConnector


def test_oracle_get_session_id_fallback_on_v_session_error():
    connector = OracleConnector({"username": "test", "host": "localhost", "service_name": "XE"}, {"password": "pass"})

    # Mock connection where v$session query raises an ORA-00942 exception
    mock_conn = MagicMock()

    def mock_execute(sql, *args, **kwargs):
        sql_str = str(sql)
        if "v$session" in sql_str:
            raise Exception("ORA-00942: table or view does not exist")
        elif "SYS_CONTEXT" in sql_str:
            mock_res = MagicMock()
            mock_res.first.return_value = ["1234"]
            return mock_res
        raise Exception(f"Unexpected query: {sql_str}")

    mock_conn.execute.side_effect = mock_execute

    # get_session_id should catch the v$session exception and fall back to SYS_CONTEXT SID
    sid = connector.get_session_id(mock_conn)
    assert sid == "1234"


def test_base_connector_run_sql_suppresses_on_session_ready_error():
    class TestConnector(SqlAlchemyDatabaseConnector):
        connection_type = "TEST"

        def get_session_id(self, conn):
            raise RuntimeError("Session ID lookup failed unexpectedly")

    connector = TestConnector({"host": "localhost"}, {})

    # Mock engine execution
    mock_engine = MagicMock()
    mock_cxn = MagicMock()
    mock_result = MagicMock()
    mock_result.returns_rows = True
    mock_result.keys.return_value = ["col1"]
    mock_result.fetchmany.return_value = [["val1"]]
    mock_cxn.execute.return_value = mock_result
    mock_engine.begin.return_value.__enter__.return_value = mock_cxn
    connector.engine = MagicMock(return_value=mock_engine)

    # Callback that triggers get_session_id
    on_ready_called = []

    def on_session_ready(sid):
        on_ready_called.append(sid)

    # run_sql should execute successfully despite get_session_id raising an exception inside on_session_ready
    res = connector.run_sql("SELECT 1 FROM dual", {}, limit=10, on_session_ready=on_session_ready)
    assert res.is_result_set is True
    assert res.rows == [["val1"]]
