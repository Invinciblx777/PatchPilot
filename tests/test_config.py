"""Configuration loading and validation (pure functions, no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from patchpilot.agntid_connector import (
    DEFAULT_TIMEOUT_SECONDS,
    ConfigError,
    load_config,
    redact_url,
)
from patchpilot.discover_cli import merged_env

URL = "http://localhost:8082/mcp"


def test_missing_endpoint_is_a_config_error():
    with pytest.raises(ConfigError, match="AGNTID_MCP_URL is not set"):
        load_config({})


def test_blank_endpoint_is_a_config_error():
    with pytest.raises(ConfigError):
        load_config({"AGNTID_MCP_URL": "   "})


def test_valid_config_without_token():
    config = load_config({"AGNTID_MCP_URL": URL})
    assert config.mcp_url == URL
    assert config.access_token is None
    assert config.timeout_seconds == DEFAULT_TIMEOUT_SECONDS


def test_empty_token_means_no_token():
    config = load_config({"AGNTID_MCP_URL": URL, "AGNTID_ACCESS_TOKEN": ""})
    assert config.access_token is None


def test_valid_config_with_token_and_timeout():
    config = load_config(
        {"AGNTID_MCP_URL": URL, "AGNTID_ACCESS_TOKEN": "abc.def.ghi", "AGNTID_TIMEOUT_SECONDS": "5"}
    )
    assert config.access_token == "abc.def.ghi"
    assert config.timeout_seconds == 5.0


@pytest.mark.parametrize("url", ["localhost:8082/mcp", "ftp://host/mcp", "http:///mcp", "http://host:99999/mcp"])
def test_invalid_urls_are_rejected(url):
    with pytest.raises(ConfigError):
        load_config({"AGNTID_MCP_URL": url})


def test_credentials_in_url_are_rejected_without_echoing_them():
    with pytest.raises(ConfigError) as info:
        load_config({"AGNTID_MCP_URL": "http://user:hunter2@localhost:8082/mcp"})
    assert "hunter2" not in str(info.value)


def test_bearer_prefix_is_rejected_without_echoing_token():
    with pytest.raises(ConfigError, match="without the 'Bearer'") as info:
        load_config({"AGNTID_MCP_URL": URL, "AGNTID_ACCESS_TOKEN": "Bearer s3cr3t-value"})
    assert "s3cr3t-value" not in str(info.value)


def test_literal_oauth_token_is_rejected():
    # fastmcp would start an interactive OAuth flow for this exact string.
    with pytest.raises(ConfigError):
        load_config({"AGNTID_MCP_URL": URL, "AGNTID_ACCESS_TOKEN": "oauth"})


@pytest.mark.parametrize("value", ["abc", "0", "-1", "nan", "inf"])
def test_bad_timeouts_are_rejected(value):
    with pytest.raises(ConfigError):
        load_config({"AGNTID_MCP_URL": URL, "AGNTID_TIMEOUT_SECONDS": value})


def test_repr_never_contains_token():
    config = load_config({"AGNTID_MCP_URL": URL, "AGNTID_ACCESS_TOKEN": "tok-should-not-print"})
    assert "tok-should-not-print" not in repr(config)
    assert "tok-should-not-print" not in str(config)


def test_redact_url_strips_userinfo_query_and_fragment():
    assert redact_url("https://u:p@host:8443/mcp?token=x#frag") == "https://host:8443/mcp"
    assert redact_url("http://[::1]:8082/mcp") == "http://[::1]:8082/mcp"


def test_env_file_fills_only_unset_keys(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AGNTID_MCP_URL=http://from-file:8082/mcp\n"
        "AGNTID_ACCESS_TOKEN=file-token\n"
        "UNRELATED=ignored\n"
    )
    merged, sources = merged_env({"AGNTID_MCP_URL": "http://from-env:8082/mcp"}, env_file)
    assert merged == {
        "AGNTID_MCP_URL": "http://from-env:8082/mcp",
        "AGNTID_ACCESS_TOKEN": "file-token",
    }
    assert "UNRELATED" not in merged
    assert all("file-token" not in s for s in sources)


def test_missing_env_file_is_fine(tmp_path: Path):
    merged, _ = merged_env({}, tmp_path / "does-not-exist")
    assert merged == {}
