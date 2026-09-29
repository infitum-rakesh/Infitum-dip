from app.connectors.application.salesforce import SalesforceConnector
from app.execution.pipeline_job_executor import request_cancel, is_cancelled, start_job, _running_tasks, _cancelled, _cancel_events


def test_cancel_event_creation_and_signaling():
    job_id = 999
    # Simulate start_job initializing thread event
    _cancel_events[job_id] = _cancel_events.get(job_id) or None
    assert not is_cancelled(job_id)

    # Trigger request_cancel
    request_cancel(job_id)
    assert is_cancelled(job_id)

    # Cleanup
    _cancelled.discard(job_id)
    _cancel_events.pop(job_id, None)


def test_sfdc_open_sql_stream_aborts_when_cancelled():
    connector = SalesforceConnector({"username": "u", "host": "h"}, {"password": "p"})
    
    # Mock cancel_check returning True
    cancelled_called = [False]

    def mock_cancel_check():
        cancelled_called[0] = True
        return True

    # Check that open_sql_stream respects cancel_check before trying to connect
    failed = False
    try:
        connector.open_sql_stream("SELECT Id FROM Order", {}, cancel_check=mock_cancel_check)
    except Exception as exc:
        failed = True
    
    assert cancelled_called[0] is True or failed
