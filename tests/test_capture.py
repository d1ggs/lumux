from types import SimpleNamespace
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
        self.start_calls = []

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
        self.start_calls.append(options)
        return self._fire_response("Start")


class FakeSession:
    """Stand-in for an org.freedesktop.portal.Session proxy."""

    def __init__(self, handle, closed_sessions):
        self._handle = handle
        self._closed_sessions = closed_sessions

    def Close(self):
        self._closed_sessions.append(self._handle)


class FakeBus:
    def __init__(self, screencast):
        self.con = FakeConnection()
        screencast._connection = self.con
        self._screencast = screencast
        self.closed_sessions = []

    def get(self, name, path):
        if path is not None and str(path).startswith("/session/"):
            return FakeSession(path, self.closed_sessions)
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
            capture._next_portal_retry = 0.0  # output-settle delay elapsed
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


class HangingSelectSourcesScreenCast(FakeScreenCast):
    """SelectSources never delivers a Response - simulates the portal
    withholding the share picker while the monitor is off."""

    def SelectSources(self, session_handle, options):
        self.select_sources_calls.append(options)
        self._request_counter += 1
        return f"/org/freedesktop/portal/desktop/request/_/r{self._request_counter}"


def test_select_sources_timeout_returns_false_without_hanging():
    screencast = HangingSelectSourcesScreenCast(
        None, _responses(start_results={"streams": [(42, {})]})
    )

    with patch("pydbus.SessionBus", return_value=FakeBus(screencast)):
        with patch.object(ScreenCapture, "_SELECT_SOURCES_TIMEOUT_S", 0):
            capture = ScreenCapture(source_type="screen")
            assert capture._setup_portal_session() is False


class HangingStartScreenCast(FakeScreenCast):
    """Start never delivers a Response - simulates a portal session that
    went stale while SelectSources was waiting on the user."""

    def Start(self, session_handle, parent_window, options):
        self._request_counter += 1
        return f"/org/freedesktop/portal/desktop/request/_/r{self._request_counter}"


def test_start_timeout_returns_false_without_hanging():
    screencast = HangingStartScreenCast(None, _responses(start_results={}))

    with patch("pydbus.SessionBus", return_value=FakeBus(screencast)):
        with patch.object(ScreenCapture, "_START_TIMEOUT_S", 0):
            capture = ScreenCapture(source_type="screen")
            assert capture._setup_portal_session() is False


def test_token_is_retained_when_a_handshake_fails_ambiguously():
    """A restore that fails without the portal explicitly rejecting the token
    (e.g. attempted while the compositor had the screen blanked) must NOT
    discard it. Whether such an attempt really consumed the token is not
    observable, and throwing away a possibly-valid one guarantees a share
    picker, whereas keeping it costs nothing: an actually-dead token is
    cleared by the InvalidArgument path instead."""
    screencast = HangingSelectSourcesScreenCast(
        None, _responses(start_results={"streams": [(42, {})]})
    )
    saved_tokens = []

    with patch("pydbus.SessionBus", return_value=FakeBus(screencast)):
        with patch.object(ScreenCapture, "_SELECT_SOURCES_TIMEOUT_S", 0):
            capture = ScreenCapture(
                source_type="screen",
                restore_token="MAYBE-STILL-GOOD",
                on_restore_token=saved_tokens.append,
            )
            assert capture._setup_portal_session() is False

    assert capture._restore_token == "MAYBE-STILL-GOOD"
    assert saved_tokens == []


def test_capture_defers_portal_setup_while_the_screen_is_blanked():
    """The compositor cannot serve a screencast restore while the shield is
    up; attempting anyway fails and risks burning the single-use restore
    token, which is what forces a share picker once the screen returns."""
    screencast = FakeScreenCast(None, _responses(start_results={"streams": [(42, {})]}))

    with patch("pydbus.SessionBus", return_value=FakeBus(screencast)):
        capture = ScreenCapture(source_type="screen", restore_token="KEEP-ME")
        with patch.object(capture, "_screen_is_blanked", return_value=True):
            assert capture.capture() is None

    assert screencast.create_session_calls == []
    assert capture._restore_token == "KEEP-ME"


def test_capture_sets_up_portal_once_the_screen_is_back():
    screencast = FakeScreenCast(None, _responses(start_results={"streams": [(42, {})]}))

    with patch("pydbus.SessionBus", return_value=FakeBus(screencast)):
        capture = ScreenCapture(source_type="screen")
        with patch.object(capture, "_screen_is_blanked", return_value=True):
            assert capture.capture() is None
        assert screencast.create_session_calls == []

        capture._next_portal_retry = 0.0  # blanked-backoff elapsed
        with patch.object(capture, "_screen_is_blanked", return_value=False):
            with patch.object(capture, "_start_pipeline", return_value=False):
                assert capture.capture() is None

    assert len(screencast.create_session_calls) == 1


def test_unavailable_screensaver_service_does_not_block_capture():
    """On a desktop without any screensaver service the probe must fail
    open, otherwise capture would never start there."""
    screencast = FakeScreenCast(None, _responses(start_results={"streams": [(42, {})]}))

    with patch("pydbus.SessionBus", return_value=FakeBus(screencast)):
        capture = ScreenCapture(source_type="screen")
        # FakeBus.get() returns a dict with no GetActive method, standing in
        # for the name being absent from the bus.
        assert capture._screen_is_blanked() is False


class _SaverBus:
    """Session bus exposing only the given screensaver services."""

    def __init__(self, services):
        self._services = services

    def get(self, name, path=None):
        if name not in self._services:
            raise Exception(f"The name {name} was not provided by any .service files")
        active = self._services[name]
        return SimpleNamespace(GetActive=lambda: active)


def test_freedesktop_screensaver_lock_counts_as_blanked():
    """KDE Plasma has no org.gnome.ScreenSaver; its lock state is only on
    org.freedesktop.ScreenSaver. Missing it there let a handshake run while
    locked, the KDE portal found no output to restore and deleted the
    consent, and a share picker appeared after every lock."""
    bus = _SaverBus({"org.freedesktop.ScreenSaver": True})
    with patch("pydbus.SessionBus", return_value=bus):
        capture = ScreenCapture(source_type="screen")
        assert capture._screen_is_blanked() is True


def test_unlocked_freedesktop_screensaver_does_not_block_capture():
    bus = _SaverBus({"org.freedesktop.ScreenSaver": False})
    with patch("pydbus.SessionBus", return_value=bus):
        capture = ScreenCapture(source_type="screen")
        assert capture._screen_is_blanked() is False


def test_gnome_screensaver_still_detected_without_freedesktop_service():
    bus = _SaverBus({"org.gnome.ScreenSaver": True})
    with patch("pydbus.SessionBus", return_value=bus):
        capture = ScreenCapture(source_type="screen")
        assert capture._screen_is_blanked() is True


def test_new_token_is_persisted_even_when_start_yields_no_node():
    """The Start response can carry a fresh token while still failing to
    give us a node. That token is the only valid one from then on, so it
    must be persisted rather than discarded with the failure."""

    class NoNodeButTokenScreenCast(FakeScreenCast):
        def Start(self, session_handle, parent_window, options):
            self._request_counter += 1
            path = (
                "/org/freedesktop/portal/desktop/request/_/"
                f"r{self._request_counter}"
            )

            def _deliver():
                cb = self._connection.subscriptions.get(path)
                if cb is not None:
                    cb(
                        None,
                        None,
                        path,
                        "org.freedesktop.portal.Request",
                        "Response",
                        (0, {"streams": [], "restore_token": "ROTATED"}),
                    )
                return False

            GLib.idle_add(_deliver)
            return path

    screencast = NoNodeButTokenScreenCast(None, _responses(start_results={}))
    saved_tokens = []

    with patch("pydbus.SessionBus", return_value=FakeBus(screencast)):
        capture = ScreenCapture(
            source_type="screen",
            restore_token="OLD",
            on_restore_token=saved_tokens.append,
        )
        assert capture._setup_portal_session() is False

    assert capture._restore_token == "ROTATED"
    assert saved_tokens == ["ROTATED"]


def test_every_portal_request_gets_a_unique_handle_token():
    """handle_token forms the Request object path
    (/org/freedesktop/portal/desktop/request/SENDER/TOKEN). Omitting it makes
    the portal fall back to a constant, so every request collides on one path
    and fails with "An object is already exported ..." whenever a previous
    Request has not been torn down yet - which is exactly the churn right
    after wake, and it breaks the silent restore."""
    screencast = FakeScreenCast(None, _responses(start_results={"streams": [(42, {})]}))

    with patch("pydbus.SessionBus", return_value=FakeBus(screencast)):
        capture = ScreenCapture(source_type="screen")
        assert capture._setup_portal_session() is True

    tokens = [
        screencast.create_session_calls[0].get("handle_token"),
        screencast.select_sources_calls[0].get("handle_token"),
        screencast.start_calls[0].get("handle_token"),
    ]
    assert all(t is not None for t in tokens), f"missing handle_token: {tokens}"
    unpacked = [t.unpack() for t in tokens]
    assert len(set(unpacked)) == 3, f"handle_tokens must all differ: {unpacked}"


def test_handle_and_session_tokens_differ_across_back_to_back_handshakes():
    """Two handshakes inside the same second must not reuse object paths;
    a wall-clock seconds value is too coarse to guarantee that."""
    screencast = FakeScreenCast(None, _responses(start_results={"streams": [(42, {})]}))

    with patch("pydbus.SessionBus", return_value=FakeBus(screencast)):
        capture = ScreenCapture(source_type="screen")
        assert capture._setup_portal_session() is True
        capture._portal_node_id = None
        assert capture._setup_portal_session() is True

    session_tokens = [
        c["session_handle_token"].unpack() for c in screencast.create_session_calls
    ]
    assert len(set(session_tokens)) == 2, f"session tokens reused: {session_tokens}"

    all_handles = [
        c["handle_token"].unpack()
        for c in (
            screencast.create_session_calls
            + screencast.select_sources_calls
            + screencast.start_calls
        )
    ]
    assert len(set(all_handles)) == len(all_handles), f"reused: {all_handles}"


def _track_glib_timeout_sources():
    """Patch GLib timeout add/remove so a test can assert every source the
    portal handshake creates is also destroyed. Calls through to the real
    functions so the sources genuinely go away."""
    added, removed = [], []
    real_add = GLib.timeout_add_seconds
    real_remove = GLib.source_remove

    def tracking_add(interval, callback, *args):
        source_id = real_add(interval, callback, *args)
        added.append(source_id)
        return source_id

    def tracking_remove(source_id):
        removed.append(source_id)
        return real_remove(source_id)

    return added, removed, tracking_add, tracking_remove


def test_portal_handshake_removes_its_timeout_sources():
    """Each handshake stage arms a GLib timeout as a watchdog. Leaving those
    sources alive lets them fire during a LATER attempt's nested main loop
    and quit it prematurely, which collapses the retry backoff and spams the
    user with share-picker dialogs."""
    screencast = FakeScreenCast(None, _responses(start_results={"streams": [(42, {})]}))
    added, removed, tracking_add, tracking_remove = _track_glib_timeout_sources()

    with patch("pydbus.SessionBus", return_value=FakeBus(screencast)):
        with patch.object(GLib, "timeout_add_seconds", tracking_add):
            with patch.object(GLib, "source_remove", tracking_remove):
                capture = ScreenCapture(source_type="screen")
                assert capture._setup_portal_session() is True

    assert added, "expected the handshake to arm timeout watchdogs"
    leaked = sorted(set(added) - set(removed))
    assert not leaked, f"leaked GLib timeout sources: {leaked}"


def test_timed_out_handshake_closes_the_portal_session():
    """A session created but never started must be closed on the way out;
    otherwise its share-picker dialog stays on screen and every retry adds
    another one."""
    screencast = HangingSelectSourcesScreenCast(
        None, _responses(start_results={"streams": [(42, {})]})
    )
    bus = FakeBus(screencast)

    with patch("pydbus.SessionBus", return_value=bus):
        with patch.object(ScreenCapture, "_SELECT_SOURCES_TIMEOUT_S", 0):
            capture = ScreenCapture(source_type="screen")
            assert capture._setup_portal_session() is False

    assert bus.closed_sessions == ["/session/1"]
    assert capture._portal_session_handle is None


class _FakeGLContext:
    def __init__(self, create_result):
        self._create_result = create_result
        self.destroyed = False

    def create(self, other):
        if isinstance(self._create_result, Exception):
            raise self._create_result
        return self._create_result

    def destroy(self):
        self.destroyed = True


def _fake_gstgl(context):
    return SimpleNamespace(
        GLDisplay=SimpleNamespace(new=lambda: object()),
        GLContext=SimpleNamespace(new=lambda display: context),
    )


def _run_prefer_windowless_gl(context, environ):
    import gi.repository
    from lumux import capture

    with patch.dict("os.environ", environ, clear=True), \
            patch.object(gi.repository, "GstGL", _fake_gstgl(context), create=True), \
            patch("gi.require_version"):
        used = capture._prefer_windowless_gl()
        import os
        return used, os.environ.get("GST_GL_WINDOW")


def test_windowless_gl_is_used_when_egl_device_context_works():
    context = _FakeGLContext(True)
    used, value = _run_prefer_windowless_gl(context, {})
    assert used is True
    assert value == "egl-device"
    assert context.destroyed


def test_windowless_gl_is_reverted_when_context_creation_fails():
    error = GLib.Error("Cannot create an GLX context from a non-X11 window")
    used, value = _run_prefer_windowless_gl(_FakeGLContext(error), {})
    assert used is False
    assert value is None


def test_windowless_gl_respects_explicit_user_setting():
    used, value = _run_prefer_windowless_gl(
        _FakeGLContext(True), {"GST_GL_WINDOW": "wayland"}
    )
    assert used is False
    assert value == "wayland"


def test_session_closed_waits_for_outputs_to_settle_before_handshake():
    """A monitor that re-links on wake vanishes for a moment exactly when the
    compositor closes the session; an immediate handshake reaches the KDE
    portal with no output, which deletes the stored consent."""
    screencast = FakeScreenCast(None, _responses(start_results={"streams": [(42, {})]}))
    bus = FakeBus(screencast)

    with patch("pydbus.SessionBus", return_value=bus):
        capture = ScreenCapture(source_type="screen")
        with patch.object(capture, "_start_pipeline", return_value=False):
            assert capture.capture() is None
            _fire_session_closed(bus)
            assert capture.capture() is None

    assert len(screencast.create_session_calls) == 1


def test_no_connected_monitor_counts_as_blanked():
    capture = ScreenCapture(source_type="screen")
    capture._monitor_count = 0
    capture._monitors_changed_at = None
    assert capture._screen_is_blanked() is True


def test_recent_monitor_change_counts_as_blanked_until_settled():
    capture = ScreenCapture(source_type="screen")
    capture._monitor_count = 1
    with patch("lumux.capture.time.monotonic", return_value=100.0):
        capture._on_monitors_changed(SimpleNamespace(get_n_items=lambda: 1), 0, 0, 1)
    with patch("pydbus.SessionBus", return_value=_SaverBus({})):
        with patch("lumux.capture.time.monotonic", return_value=101.0):
            assert capture._screen_is_blanked() is True
        with patch("lumux.capture.time.monotonic", return_value=104.0):
            assert capture._screen_is_blanked() is False


def test_monitor_watcher_tracks_count_changes():
    capture = ScreenCapture(source_type="screen")
    capture._on_monitors_changed(SimpleNamespace(get_n_items=lambda: 0), 0, 1, 0)
    assert capture._monitor_count == 0
    capture._on_monitors_changed(SimpleNamespace(get_n_items=lambda: 1), 0, 0, 1)
    assert capture._monitor_count == 1
