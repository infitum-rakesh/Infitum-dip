"""
Automated test for app/connectors/ocr/aws_textract.py — written in Phase
12 to lock in the real bug found and fixed by Phase 12's static review of
the cloud OCR provider connectors (no live AWS account exists in this
environment, and Textract has no free/local emulator — see the module's
own docstring).

boto3.client() is monkeypatched to raise directly, rather than standing up
a fake AWS endpoint — the bug being tested is pure exception-handling
control flow in test_connection() itself, not the real HTTP contract with
Textract (which the module docstring already discloses as unverifiable
here).
"""
import pytest

import app.connectors.ocr.aws_textract as aws_textract_module
from app.connectors.ocr.aws_textract import AwsTextractConnector


def test_test_connection_client_construction_failure_returns_graceful_result(monkeypatch):
    """Real bug fix: _client() wraps a boto3.client() construction failure
    (e.g. ValueError from a malformed region) as ConnectorError, but
    test_connection() only caught (ClientError, BotoCoreError) — the
    ConnectorError propagated uncaught instead of the graceful
    ConnectionTestResult(success=False) every other failure path here
    returns. Same bug class as oracle_erp.py's test_connection() (Phase
    11)."""
    def _raise_value_error(*args, **kwargs):
        raise ValueError("Invalid region 'not-a-real-region' provided")

    monkeypatch.setattr(aws_textract_module.boto3, "client", _raise_value_error)
    connector = AwsTextractConnector(
        config={"region": "not-a-real-region"},
        secrets={"access_key_id": "fake", "secret_access_key": "fake"},
    )

    result = connector.test_connection()  # must not raise

    assert result.success is False
    assert "Invalid region" in result.message or "Could not create Textract client" in result.message


def test_test_connection_missing_boto3_library_returns_graceful_result(monkeypatch):
    """A second, distinct real failure mode through the same widened
    except clause: boto3 itself unavailable/misconfigured such that
    client() raises something other than ClientError/BotoCoreError."""
    def _raise_runtime_error(*args, **kwargs):
        raise RuntimeError("boto3 credential resolution chain failed unexpectedly")

    monkeypatch.setattr(aws_textract_module.boto3, "client", _raise_runtime_error)
    connector = AwsTextractConnector(config={"region": "us-east-1"}, secrets={})

    result = connector.test_connection()  # must not raise

    assert result.success is False
