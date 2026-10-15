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
