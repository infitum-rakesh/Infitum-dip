from pathlib import Path

import pytest

from tally_agent import credential_store
from tally_agent.config import AgentConfig, ConfigError, load_config, save_config
from tally_agent.credential_store import AgentCredentials, CredentialStoreError


def test_save_and_load_config_roundtrip(tmp_path):
    path = tmp_path / "config.yaml"
    config = AgentConfig(server_url="https://dip.example.com", tally_host="10.0.0.5", tally_port=9001)
    save_config(config, path)
    loaded = load_config(path)
    assert loaded.server_url == "https://dip.example.com"
    assert loaded.tally_host == "10.0.0.5"
    assert loaded.tally_port == 9001


def test_load_config_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nonexistent.yaml")


def test_load_config_rejects_non_mapping_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("- just\n- a\n- list\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_ws_url_rewrites_https_to_wss():
    config = AgentConfig(server_url="https://dip.example.com")
    assert config.ws_url() == "wss://dip.example.com/api/tally-agent/ws"


def test_ws_url_rewrites_http_to_ws():
    config = AgentConfig(server_url="http://localhost:8080")
    assert config.ws_url() == "ws://localhost:8080/api/tally-agent/ws"


def test_ws_url_requires_server_url():
    config = AgentConfig(server_url="")
    with pytest.raises(Exception):
        config.ws_url()


def test_save_and_load_credentials_roundtrip(tmp_path):
    creds = AgentCredentials(installation_id=42, credential_id="cid-1", credential_secret="sekret")
    credential_store.save_credentials(creds, tmp_path)
    loaded = credential_store.load_credentials(tmp_path)
    assert loaded.installation_id == 42
    assert loaded.credential_id == "cid-1"
    assert loaded.credential_secret == "sekret"


def test_load_credentials_missing_raises(tmp_path):
    with pytest.raises(CredentialStoreError):
        credential_store.load_credentials(tmp_path)


def test_load_credentials_corrupt_raises(tmp_path):
    path = tmp_path / "credentials.dat"
    path.write_bytes(b"not json at all")
    with pytest.raises(CredentialStoreError):
        credential_store.load_credentials(tmp_path)


def test_delete_credentials(tmp_path):
    creds = AgentCredentials(installation_id=1, credential_id="c", credential_secret="s")
    credential_store.save_credentials(creds, tmp_path)
    assert credential_store.delete_credentials(tmp_path) is True
    assert credential_store.delete_credentials(tmp_path) is False
