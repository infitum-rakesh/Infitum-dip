import pytest

from app.execution.pipeline_job_executor import NodeExecutionError, normalize_target_load_config


def test_drop_recreate_full_strategy_normalizes_to_full_drop_and_create():
    cfg = {"loadType": "full", "fullLoadStrategy": "drop_recreate", "mode": "replace"}

    load_config = normalize_target_load_config(cfg)

    assert load_config.load_mode == "FULL"
    assert load_config.load_strategy == "DROP_AND_CREATE"
    assert load_config.drop_existing is True
    assert load_config.create_table is True
    assert load_config.truncate_first is False
    assert load_config.write_mode == "insert"
    assert load_config.as_log_dict()["loadMode"] == "FULL"
    assert load_config.as_log_dict()["loadStrategy"] == "DROP_AND_CREATE"


def test_canonical_drop_and_create_overrides_legacy_append_fields():
    cfg = {
        "loadMode": "FULL",
        "loadStrategy": "DROP_AND_CREATE",
        "loadType": "full",
        "fullLoadStrategy": "append",
        "mode": "append",
    }

    load_config = normalize_target_load_config(cfg)

    assert load_config.load_mode == "FULL"
    assert load_config.load_strategy == "DROP_AND_CREATE"
    assert load_config.drop_existing is True
    assert load_config.create_table is True
    assert load_config.truncate_first is False


def test_truncate_full_strategy_normalizes_without_drop():
    cfg = {"loadType": "full", "fullLoadStrategy": "truncate_load"}

    load_config = normalize_target_load_config(cfg)

    assert load_config.load_mode == "FULL"
    assert load_config.load_strategy == "TRUNCATE_AND_LOAD"
    assert load_config.drop_existing is False
    assert load_config.create_table is True
    assert load_config.truncate_first is True


def test_append_strategy_never_drops_or_truncates():
    cfg = {"loadType": "full", "fullLoadStrategy": "append"}

    load_config = normalize_target_load_config(cfg)

    assert load_config.load_mode == "APPEND"
    assert load_config.load_strategy == "APPEND"
    assert load_config.drop_existing is False
    assert load_config.create_table is False
    assert load_config.truncate_first is False


def test_legacy_drop_and_recreate_normalizes_to_full_drop_and_create():
    cfg = {"mode": "replace", "dropAndRecreate": True, "createIfNotExists": False}

    load_config = normalize_target_load_config(cfg)

    assert load_config.load_mode == "FULL"
    assert load_config.load_strategy == "DROP_AND_CREATE"
    assert load_config.drop_existing is True
    assert load_config.create_table is True
    assert load_config.truncate_first is False


def test_unknown_full_strategy_fails_validation():
    with pytest.raises(NodeExecutionError, match="unknown full-load strategy"):
        normalize_target_load_config({"loadType": "full", "fullLoadStrategy": "drop_maybe"})
