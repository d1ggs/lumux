from unittest.mock import patch

from gi.repository import GLib

from lumux.capture import ScreenCapture


class FakeConnection:
    def __init__(self):
        self.subscriptions = {}
        self._sub_ids = {}
        self.unsubscribed = []
        self._next_id = 1

    def signal_subscribe(self, sender, iface, signal, path, arg0, flags, callback):
        self.subscriptions[path] = callback
        sub_id = self._next_id
        self._next_id += 1
        self._sub_ids[sub_id] = path
        return sub_id

    def signal_unsubscribe(self, sub_id):
        self.unsubscribed.append(sub_id)
        path = self._sub_ids.pop(sub_id, None)
        if path is not None and self.subscriptions.get(path) is not None:
            self.subscriptions.pop(path, None)


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
        self.create_session_calls = []

    def _fire_response(self, method_name):
        self._request_counter += 1
        path = f"/org/freedesktop/portal/desktop/request/_/r{self._request_counter}"
        code, results = self._responses[method_name]

        def _deliver():
            callback = self._connection.subscriptions.get(path)
            if callback is not None:
                callback(
                    None,
                    None,
                    path,
                    "org.freedesktop.portal.Request",
                    "Response",
                    (code, results),
                )
            return False

        GLib.idle_add(_deliver)
        return path

    def CreateSession(self, options):
        self.create_session_calls.append(options)
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
        None,
        _responses(start_results={"streams": [(42, {})], "restore_token": "NEW-TOKEN"}),
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
        None,
        _responses(
            start_results={"streams": [(42, {})], "restore_token": "SAME-TOKEN"}
        ),
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


class FailOnTokenScreenCast(FakeScreenCast):
    """Raises on SelectSources when a restore_token is present, mimicking
    GNOME's hard InvalidArgument failure on malformed tokens."""

    def SelectSources(self, session_handle, options):
        self.select_sources_calls.append(options)
        if "restore_token" in options:
            raise Exception(
                "GDBus.Error:org.freedesktop.portal.Error.InvalidArgument: "
                "Restore token is not a valid UUID string (36)"
            )
        return self._fire_response("SelectSources")


def test_malformed_token_failure_clears_token_and_retries_without_it():
    screencast = FailOnTokenScreenCast(
        None,
        _responses(start_results={"streams": [(42, {})], "restore_token": "FRESH"}),
    )
    saved_tokens = []

    with patch("pydbus.SessionBus", return_value=FakeBus(screencast)):
        capture = ScreenCapture(
            source_type="screen",
            restore_token="corrupted-not-a-uuid",
            on_restore_token=saved_tokens.append,
        )
        assert capture._setup_portal_session() is True

    # First attempt carried the bad token, retry did not
    assert "restore_token" in screencast.select_sources_calls[0]
    assert "restore_token" not in screencast.select_sources_calls[1]
    # Token was cleared (""), then the fresh one from the retry was saved
    assert saved_tokens == ["", "FRESH"]
    assert capture._restore_token == "FRESH"


class FailOnCreateSessionScreenCast(FakeScreenCast):
    """Raises on CreateSession, before any restore token is ever sent."""

    def CreateSession(self, options):
        raise Exception("portal unavailable")


def test_create_session_failure_preserves_stored_token():
    screencast = FailOnCreateSessionScreenCast(None, _responses(start_results={}))
    saved_tokens = []

    with patch("pydbus.SessionBus", return_value=FakeBus(screencast)):
        capture = ScreenCapture(
            source_type="screen",
            restore_token="VALID-TOKEN",
            on_restore_token=saved_tokens.append,
        )
        assert capture._setup_portal_session() is False

    # The token was never sent, so it must not be cleared and no retry
    # (which would clear it) may happen.
    assert capture._restore_token == "VALID-TOKEN"
    assert saved_tokens == []
    assert screencast.select_sources_calls == []


def test_first_ever_token_fires_callback():
    screencast = FakeScreenCast(
        None,
        _responses(start_results={"streams": [(42, {})], "restore_token": "FIRST"}),
    )
    saved_tokens = []

    with patch("pydbus.SessionBus", return_value=FakeBus(screencast)):
        capture = ScreenCapture(
            source_type="screen",
            on_restore_token=saved_tokens.append,
        )
        assert capture._setup_portal_session() is True

    assert saved_tokens == ["FIRST"]
    assert capture._restore_token == "FIRST"


def _fire_session_closed(bus, session_handle="/session/1"):
    """Deliver the portal's Session.Closed signal like the compositor would."""
    callback = bus.con.subscriptions[session_handle]
    callback(
        None,
        ":1.99",
        session_handle,
        "org.freedesktop.portal.Session",
        "Closed",
        (),
    )


def test_session_closed_signal_resets_portal_state():
    screencast = FakeScreenCast(None, _responses(start_results={"streams": [(42, {})]}))
    bus = FakeBus(screencast)

    with patch("pydbus.SessionBus", return_value=bus):
        capture = ScreenCapture(source_type="screen")
        assert capture._setup_portal_session() is True
        _fire_session_closed(bus)

    assert capture._portal_node_id is None
    assert capture._portal_session_handle is None
    assert capture._pipeline_running is False


def test_capture_reestablishes_session_after_session_closed():
    screencast = FakeScreenCast(
        None,
        _responses(start_results={"streams": [(42, {})], "restore_token": "TOK"}),
    )
    bus = FakeBus(screencast)

    with patch("pydbus.SessionBus", return_value=bus):
        capture = ScreenCapture(source_type="screen")
        with patch.object(capture, "_start_pipeline", return_value=False):
            assert capture.capture() is None  # first portal setup
            _fire_session_closed(bus)
            assert capture.capture() is None  # must re-run the portal setup

    assert len(screencast.create_session_calls) == 2
    # The recovery attempt reuses the restore token from the first grant
    assert screencast.select_sources_calls[1]["restore_token"].unpack() == "TOK"


def test_failed_portal_setup_is_rate_limited():
    class CountingFailScreenCast(FakeScreenCast):
        def CreateSession(self, options):
            self.create_session_calls.append(options)
            raise Exception("portal unavailable")

    screencast = CountingFailScreenCast(None, _responses(start_results={}))

    with patch("pydbus.SessionBus", return_value=FakeBus(screencast)):
        capture = ScreenCapture(source_type="screen")
        assert capture.capture() is None
        assert capture.capture() is None  # inside backoff window: no new attempt

    assert len(screencast.create_session_calls) == 1

    with patch("pydbus.SessionBus", return_value=FakeBus(screencast)):
        capture._next_portal_retry = 0.0  # backoff elapsed
        assert capture.capture() is None

    assert len(screencast.create_session_calls) == 2


def test_stop_pipeline_unsubscribes_session_closed_signal():
    screencast = FakeScreenCast(None, _responses(start_results={"streams": [(42, {})]}))
    bus = FakeBus(screencast)

    with patch("pydbus.SessionBus", return_value=bus):
        capture = ScreenCapture(source_type="screen")
        assert capture._setup_portal_session() is True
        assert "/session/1" in bus.con.subscriptions
        capture.stop_pipeline()

    assert "/session/1" not in bus.con.subscriptions


def test_failure_without_stored_token_does_not_retry():
    class AlwaysFailScreenCast(FakeScreenCast):
        def SelectSources(self, session_handle, options):
            self.select_sources_calls.append(options)
            raise Exception("portal unavailable")

    screencast = AlwaysFailScreenCast(None, _responses(start_results={}))
    with patch("pydbus.SessionBus", return_value=FakeBus(screencast)):
        capture = ScreenCapture(source_type="screen")
        assert capture._setup_portal_session() is False

    assert len(screencast.select_sources_calls) == 1
