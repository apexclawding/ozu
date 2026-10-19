"""`mcp --http` refuses to start without a token, and says which file to edit."""
import sys

import pytest


def test_no_token_refusal_names_the_configured_home(monkeypatch, tmp_path):
    home = tmp_path / "elsewhere"
    monkeypatch.setenv("OZU_HOME", str(home))
    monkeypatch.delenv("OZU_MCP_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["ozu", "--http"])
    import ozu.config as config
    from ozu import mcp_server
    config._config = None
    try:
        with pytest.raises(SystemExit) as exc:
            mcp_server.main()
    finally:
        config._config = None
    msg = str(exc.value)
    assert "will not start" in msg
    assert f"{home / '.env'}" in msg
    assert "ozu-data" not in msg
