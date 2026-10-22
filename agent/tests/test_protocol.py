import json

import pytest

from tally_agent import protocol


def test_build_auth_shape():
    msg = json.loads(protocol.build_auth(
        credential_id="cid", credential_secret="sec", agent_version="0.1.0", hostname="H", os_version="Win 11",
    ))
    assert msg == {
        "type": "auth", "credential_id": "cid", "credential_secret": "sec",
        "agent_version": "0.1.0", "hostname": "H", "os_version": "Win 11",
    }


def test_build_heartbeat():
    assert json.loads(protocol.build_heartbeat()) == {"type": "heartbeat"}


def test_build_command_result_success():
    msg = json.loads(protocol.build_command_result(request_id="r1", success=True, data={"x": 1}))
    assert msg == {"type": "command_result", "request_id": "r1", "success": True, "data": {"x": 1}, "error": None}


def test_build_command_result_failure():
    msg = json.loads(protocol.build_command_result(request_id="r1", success=False, error="boom"))
    assert msg == {"type": "command_result", "request_id": "r1", "success": False, "data": None, "error": "boom"}


def test_parse_message_rejects_non_json():
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_message("not json")


def test_parse_message_rejects_missing_type():
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_message(json.dumps({"foo": "bar"}))


def test_parse_message_rejects_non_object():
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_message(json.dumps([1, 2, 3]))


def test_message_type_predicates():
    assert protocol.is_auth_ack({"type": "auth_ack"})
    assert protocol.is_heartbeat_ack({"type": "heartbeat_ack"})
    assert protocol.is_command({"type": "command"})
    assert protocol.is_job_cancel({"type": "job_cancel"})
    assert protocol.is_error({"type": "error"})
    assert not protocol.is_command({"type": "heartbeat_ack"})


def test_command_fields_extracts_request_id_operation_parameters():
    request_id, operation, parameters = protocol.command_fields({
        "type": "command", "request_id": "abc", "operation": "list_companies", "parameters": {"x": 1},
    })
    assert request_id == "abc"
    assert operation == "list_companies"
    assert parameters == {"x": 1}


def test_command_fields_defaults_parameters_to_empty_dict():
    request_id, operation, parameters = protocol.command_fields({
        "type": "command", "request_id": "abc", "operation": "list_companies",
    })
    assert parameters == {}


def test_command_fields_requires_request_id_and_operation():
    with pytest.raises(protocol.ProtocolError):
        protocol.command_fields({"type": "command", "operation": "list_companies"})
    with pytest.raises(protocol.ProtocolError):
        protocol.command_fields({"type": "command", "request_id": "abc"})
