from unittest.mock import patch

from gi.repository import GLib

from lumux.capture import ScreenCapture


class FakeConnection:
    def __init__(self):
        self.subscriptions = {}
        self._next_id = 1

    def signal_subscribe(self, sender, iface, signal, path, arg0, flags, callback):
        self.subscriptions[path] = callback
        sub_id = self._next_id
        self._next_id += 1
        return sub_id

    def signal_unsubscribe(self, sub_id):
        pass


class FakeScreenCast:
    """Scripted stand-in for the org.freedesktop.portal.ScreenCast interface.

    `responses` maps method name -> (code, results) exactly as the real
    portal's Request::Response signal would deliver them.
    """

    def __init__(self, connection, responses):
        self._connection = connection
        self._responses = responses
        self._request_counter = 0
        self.select_sources_calls = []

    def _fire_response(self, method_name):
        self._request_counter += 1
        path = f"/org/freedesktop/portal/desktop/request/_/r{self._request_counter}"
        code, results = self._responses[method_name]

        def _deliver():
            callback = self._connection.subscriptions.get(path)
            if callback is not None:
                callback(None, None, path, "org.freedesktop.portal.Request", "Response", (code, results))
            return False

        GLib.idle_add(_deliver)
        return path

    def CreateSession(self, options):
        return self._fire_response("CreateSession")

    def SelectSources(self, session_handle, options):
        self.select_sources_calls.append(options)
        return self._fire_response("SelectSources")

    def Start(self, session_handle, parent_window, options):
        return self._fire_response("Start")


class FakeBus:
    def __init__(self, screencast):
        self.con = FakeConnection()
        screencast._connection = self.con
        self._screencast = screencast

    def get(self, name, path):
        return {"org.freedesktop.portal.ScreenCast": self._screencast}


def _responses(start_results, select_sources_results=None):
    return {
        "CreateSession": (0, {"session_handle": "/session/1"}),
        "SelectSources": (0, select_sources_results or {}),
        "Start": (0, start_results),
    }


def test_select_sources_requests_persist_mode_and_no_token_on_first_run():
    screencast = FakeScreenCast(None, _responses(start_results={"streams": [(42, {})]}))

    with patch("pydbus.SessionBus", return_value=FakeBus(screencast)):
        capture = ScreenCapture(source_type="screen")
        assert capture._setup_portal_session() is True

    options = screencast.select_sources_calls[0]
    assert options["persist_mode"].unpack() == 2
    assert "restore_token" not in options


def test_select_sources_includes_stored_restore_token():
    screencast = FakeScreenCast(None, _responses(start_results={"streams": [(42, {})]}))

    with patch("pydbus.SessionBus", return_value=FakeBus(screencast)):
        capture = ScreenCapture(source_type="screen", restore_token="OLD-TOKEN")
        assert capture._setup_portal_session() is True

    options = screencast.select_sources_calls[0]
    assert options["restore_token"].unpack() == "OLD-TOKEN"


def test_new_restore_token_is_saved_via_callback():
    screencast = FakeScreenCast(
        None, _responses(start_results={"streams": [(42, {})], "restore_token": "NEW-TOKEN"})
    )
    saved_tokens = []

    with patch("pydbus.SessionBus", return_value=FakeBus(screencast)):
        capture = ScreenCapture(
            source_type="screen",
            restore_token="OLD-TOKEN",
            on_restore_token=saved_tokens.append,
        )
        assert capture._setup_portal_session() is True

    assert saved_tokens == ["NEW-TOKEN"]
    assert capture._restore_token == "NEW-TOKEN"


def test_unchanged_restore_token_does_not_trigger_callback():
    screencast = FakeScreenCast(
        None, _responses(start_results={"streams": [(42, {})], "restore_token": "SAME-TOKEN"})
    )
    saved_tokens = []

    with patch("pydbus.SessionBus", return_value=FakeBus(screencast)):
        capture = ScreenCapture(
            source_type="screen",
            restore_token="SAME-TOKEN",
            on_restore_token=saved_tokens.append,
        )
        assert capture._setup_portal_session() is True

    assert saved_tokens == []
