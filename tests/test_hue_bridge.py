from lumux.bridge_client import BridgeError
from lumux.hue_bridge import HueBridge


class FakeClient:
    def __init__(self, light_on, fail=False):
        self.light_on = light_on
        self.fail = fail
        self.get_light_calls = 0

    def get_light(self, light_id):
        self.get_light_calls += 1
        if self.fail:
            return None
        return {"id": light_id, "on": {"on": self.light_on[light_id]}}


def _bridge(client, light_ids):
    bridge = HueBridge("192.0.2.1", "key")
    bridge._client = client
    bridge.get_entertainment_light_ids = lambda config_id: list(light_ids)
    return bridge


def test_entertainment_lights_off_when_every_light_is_off():
    bridge = _bridge(FakeClient({"a": False, "b": False}), ["a", "b"])

    assert bridge.entertainment_lights_off("cfg") is True


def test_entertainment_lights_not_off_when_any_light_is_on():
    bridge = _bridge(FakeClient({"a": False, "b": True}), ["a", "b"])

    assert bridge.entertainment_lights_off("cfg") is False


def test_entertainment_lights_not_off_when_state_unreadable():
    """Fail open: an unreadable state must not keep sync paused."""
    bridge = _bridge(FakeClient({"a": False}, fail=True), ["a"])

    assert bridge.entertainment_lights_off("cfg") is False


def test_entertainment_lights_not_off_when_zone_has_no_lights():
    bridge = _bridge(FakeClient({}), [])

    assert bridge.entertainment_lights_off("cfg") is False


def test_entertainment_lights_off_caches_light_ids():
    bridge = _bridge(FakeClient({"a": False}), ["a"])
    resolve_calls = []
    bridge.get_entertainment_light_ids = lambda config_id: resolve_calls.append(config_id) or ["a"]

    bridge.entertainment_lights_off("cfg")
    bridge.entertainment_lights_off("cfg")

    assert resolve_calls == ["cfg"]


def test_entertainment_lights_not_off_on_bridge_error():
    bridge = _bridge(FakeClient({"a": False}), ["a"])

    def boom(light_id):
        raise BridgeError("unreachable")

    bridge._client.get_light = boom

    assert bridge.entertainment_lights_off("cfg") is False


# --- Link-button polling (ported from upstream PR #23 by NickdeK) ---

from unittest.mock import patch

from lumux.bridge_client import BridgeAuthError, BridgeConnectionError

CREDS = {"app_key": "new-app-key", "client_key": "CK"}


def _create_user_with(outcomes):
    """Run HueBridge.create_user with BridgeClient.create_user scripted."""
    calls = []

    def fake_create_user(ip, name):
        calls.append(ip)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    bridge = HueBridge("192.0.2.1", "")
    with patch("lumux.hue_bridge.BridgeClient.create_user", side_effect=fake_create_user):
        result = bridge.create_user("192.0.2.1", max_retries=5, retry_delay=0)
    return bridge, result, calls


def test_create_user_polls_until_link_button_pressed():
    """The bridge refuses until its button is pressed: keep polling instead
    of failing on the first attempt (the user may need to walk over)."""
    not_pressed = BridgeAuthError("Bridge error: link button not pressed")

    bridge, result, calls = _create_user_with([not_pressed, not_pressed, CREDS])

    assert result == CREDS
    assert len(calls) == 3
    assert bridge.app_key == "new-app-key"


def test_create_user_gives_up_after_max_retries():
    not_pressed = BridgeAuthError("Bridge error: link button not pressed")

    _, result, calls = _create_user_with([not_pressed] * 5)

    assert result is None
    assert len(calls) == 5


def test_create_user_stops_at_once_when_bridge_unreachable():
    """A wrong IP must not keep the wizard spinning for 90 s."""
    _, result, calls = _create_user_with([BridgeConnectionError("timed out"), CREDS])

    assert result is None
    assert len(calls) == 1
