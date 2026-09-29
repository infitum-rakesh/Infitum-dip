from unittest.mock import MagicMock
from app.connectors.base import ColumnInfo
from app.connectors.database.oracle import OracleConnector
from app.connectors.database.base import SqlAlchemyDatabaseConnector
from app.connectors.base import ConnectorError
from app.loading.writers.oracle import _oracle_type_for_column_info


def test_sfdc_oracle_type_mapping_uses_safe_varchar_for_bulk_csv_values():
    assert _oracle_type_for_column_info(ColumnInfo("Id", "id", True, max_length=18)) == "VARCHAR2(18)"
    assert _oracle_type_for_column_info(ColumnInfo("Name", "string", True, max_length=255)) == "VARCHAR2(4000)"
    assert _oracle_type_for_column_info(ColumnInfo("AccountNumber", "string", True, max_length=40)) == "VARCHAR2(4000)"
    assert _oracle_type_for_column_info(ColumnInfo("AnnualRevenue", "currency", True)) == "VARCHAR2(100)"
    assert _oracle_type_for_column_info(ColumnInfo("SomePercent", "percent", True)) == "VARCHAR2(100)"
    assert _oracle_type_for_column_info(ColumnInfo("LastModifiedDate", "datetime", True)) == "VARCHAR2(30)"
    assert _oracle_type_for_column_info(ColumnInfo("CloseDate", "date", True)) == "VARCHAR2(10)"
    assert _oracle_type_for_column_info(ColumnInfo("Description", "textarea", True, max_length=255)) == "CLOB"


def test_oracle_ora_00932_diagnostic_enrichment():
    connector = OracleConnector({"username": "testuser", "host": "localhost", "service_name": "testservice"}, {"password": "SecretPass123!"})

    # Mock base class run_sql raising ORA-00932
    mock_engine = MagicMock()
    mock_cxn = MagicMock()
    mock_cxn.execute.side_effect = Exception("ORA-00932: inconsistent datatypes: expected - got CLOB")
    mock_engine.begin.return_value.__enter__.return_value = mock_cxn
    connector.engine = MagicMock(return_value=mock_engine)

    try:
        connector.run_sql("SELECT DISTINCT * FROM my_table", {}, limit=10)
        assert False, "Should have raised ConnectorError"
    except ConnectorError as exc:
        msg = str(exc)
        assert "ORA-00932" in msg
        assert "DBMS_LOB.SUBSTR" in msg
        assert "TO_CHAR" in msg


def test_base_connector_lob_cell_reading():
    class DummyLob:
        def read(self):
            return "CLOB Content Text"

    class TestConnector(SqlAlchemyDatabaseConnector):
        connection_type = "TEST"

    connector = TestConnector({"host": "h"}, {})

    mock_engine = MagicMock()
    mock_cxn = MagicMock()
    mock_result = MagicMock()
    mock_result.returns_rows = True
    mock_result.keys.return_value = ["id", "description"]
    mock_result.fetchmany.return_value = [[1, DummyLob()]]
    mock_cxn.execute.return_value = mock_result
    mock_engine.begin.return_value.__enter__.return_value = mock_cxn
    connector.engine = MagicMock(return_value=mock_engine)

    res = connector.run_sql("SELECT id, description FROM my_table", {}, limit=10)
    assert res.is_result_set is True
    assert res.rows == [[1, "CLOB Content Text"]]
