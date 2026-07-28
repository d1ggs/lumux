from unittest.mock import patch

from lumux.bridge_client import BridgeClient, BridgeError


def test_activate_entertainment_streaming_logs_the_real_error(capsys):
    """activate_entertainment_streaming silently swallowed BridgeError with
    no log line at all (unlike its sibling deactivate_entertainment_streaming),
    making failures impossible to diagnose from the journal."""
    client = BridgeClient(bridge_ip="10.0.0.1", app_key="key")
    with patch.object(client, "_request", side_effect=BridgeError("bridge said no")):
        assert client.activate_entertainment_streaming("cfg-id") is False

    assert "bridge said no" in capsys.readouterr().out
