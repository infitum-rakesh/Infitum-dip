from datetime import datetime, timezone
from app.models.pipeline_job import PipelineJob, JobStatus
from app.services.pipeline_job_service import calculate_job_metrics, job_to_out


def test_calculate_job_metrics_pending():
    job = PipelineJob(
        id=1, pipeline_id=1, pipeline_version=1, pipeline_name="Test Pipeline",
        status=JobStatus.PENDING, trigger="Manual",
        node_stats={}, created_by=1
    )
    m = calculate_job_metrics(job)
    assert m["stage"] == "PREPARING"
    assert m["progress"]["percentage"] is None
    assert m["throughput"]["rows_per_second"] == 0.0
    assert m["timing"]["eta_seconds"] is None


def test_calculate_job_metrics_running_determinate():
    now = datetime.now(timezone.utc)
    started = datetime(now.year, now.month, now.day, now.hour, now.minute, now.second, tzinfo=timezone.utc)
    
    job = PipelineJob(
        id=2, pipeline_id=1, pipeline_version=1, pipeline_name="Test Pipeline",
        status=JobStatus.RUNNING, trigger="Manual",
        started_at=started,
        node_stats={
            "__metrics__": {
                "stage": "DOWNLOADING_BULK_RESULTS",
                "total_rows": 60000,
                "extracted": 40000,
                "processed": 40000,
                "loaded": 38000,
                "rejected": 200,
                "current_chunk": 2,
                "chunks_processed": 2,
            }
        },
        created_by=1
    )
    m = calculate_job_metrics(job)
    assert m["stage"] == "DOWNLOADING_BULK_RESULTS"
    assert m["records"]["extracted"] == 40000
    assert m["records"]["loaded"] == 38000
    assert m["records"]["rejected"] == 200
    # 38000 / 60000 = 63.33%
    assert m["progress"]["percentage"] == 63.33
    assert m["progress"]["processed"] == 38000
    assert m["progress"]["total"] == 60000


def test_calculate_job_metrics_uses_loaded_count_for_progress():
    now = datetime.now(timezone.utc)
    started = datetime(now.year, now.month, now.day, now.hour, now.minute, now.second, tzinfo=timezone.utc)

    job = PipelineJob(
        id=20, pipeline_id=1, pipeline_version=1, pipeline_name="SF Account",
        status=JobStatus.RUNNING, trigger="Manual",
        started_at=started,
        node_stats={
            "__metrics__": {
                "stage": "PREPARING_TARGET",
                "total_rows": 362563,
                "extracted": 20000,
                "processed": 20000,
                "loaded": 0,
                "rejected": 0,
            }
        },
        created_by=1,
    )

    m = calculate_job_metrics(job)

    assert m["records"]["extracted"] == 20000
    assert m["records"]["processed"] == 20000
    assert m["records"]["loaded"] == 0
    assert m["progress"]["percentage"] == 0.0
    assert m["progress"]["processed"] == 0
    assert m["timing"]["eta_seconds"] is None


def test_calculate_job_metrics_succeeded():
    started = datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc)
    completed = datetime(2026, 9, 12, 10, 0, 20, tzinfo=timezone.utc)
    
    job = PipelineJob(
        id=3, pipeline_id=1, pipeline_version=1, pipeline_name="Test Pipeline",
        status=JobStatus.SUCCEEDED, trigger="Manual",
        started_at=started, completed_at=completed, records_out=50000,
        node_stats={
            "__metrics__": {
                "stage": "COMPLETED",
                "total_rows": 50000,
                "extracted": 50000,
                "processed": 50000,
                "loaded": 50000,
                "rejected": 0,
                "current_chunk": 3,
                "chunks_processed": 3,
            }
        },
        created_by=1
    )
    m = calculate_job_metrics(job)
    assert m["stage"] == "COMPLETED"
    assert m["progress"]["percentage"] == 100.0
    assert m["timing"]["elapsed_seconds"] == 20.0
    # 50000 / 20 = 2500 rows/s
    assert m["throughput"]["rows_per_second"] == 2500.0
    assert m["records"]["loaded"] == 50000


def test_job_to_out_formatting():
    job = PipelineJob(
        id=4, pipeline_id=1, pipeline_version=1, pipeline_name="Test Pipeline",
        status=JobStatus.RUNNING, trigger="Manual",
        node_stats={}, created_by=1
    )
    out = job_to_out(job, {1: "Test User"})
    assert "stage" in out
    assert "progress" in out
    assert "throughput" in out
    assert "timing" in out
    assert "bulk_api" in out
    assert "records" in out
