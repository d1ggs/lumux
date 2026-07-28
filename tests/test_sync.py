import threading
import time

from lumux.sync import SyncController


class FakeCapture:
    """Capture stand-in whose portal session is gone: every capture() is None."""

    def capture(self):
        return None


class FakeStream:
    def __init__(self, connected=True, connect_succeeds=True):
        self.sent = []
        self._connected = connected
        self.connect_succeeds = connect_succeeds
        self.connect_calls = 0

    def is_connected(self):
        return self._connected

    def connect(self, bridge):
        self.connect_calls += 1
        if self.connect_succeeds:
            self._connected = True
        return self.connect_succeeds

    def send_colors_xy(self, channel_colors):
        self.sent.append(channel_colors)

    def get_channel_positions(self):
        return {}


class FakeSettings:
    fps = 30
    smoothing_factor = 0.3


class FakeBridge:
    pass


def _controller(stream):
    return SyncController(
        bridge=FakeBridge(),
        capture=FakeCapture(),
        zone_processor=None,
        color_analyzer=None,
        zone_mapping=None,
        settings=FakeSettings(),
        entertainment_stream=stream,
    )


COLORS = {1: ((0.3, 0.3), 120)}


def test_keepalive_resends_last_frame_when_capture_stalls():
    """A capture outage >10s lets the bridge time the DTLS session out;
    repeating the last frame keeps the session alive. The keepalive runs on
    its own thread (see test_keepalive_survives_a_stalled_capture_thread)
    so it is exercised here directly rather than via _process_frame()."""
    stream = FakeStream()
    controller = _controller(stream)
    controller._last_channel_colors = COLORS
    controller._last_send_time = time.monotonic() - 6.0

    controller._maybe_send_keepalive()

    assert stream.sent == [COLORS]


def test_no_keepalive_before_interval_elapses():
    stream = FakeStream()
    controller = _controller(stream)
    controller._last_channel_colors = COLORS
    controller._last_send_time = time.monotonic()

    controller._maybe_send_keepalive()

    assert stream.sent == []


def test_no_keepalive_while_sending_is_paused():
    """The urgent suspend path must stay dark: black is latched as the last
    frame and the keepalive may not repeat the pre-blackout colors."""
    stream = FakeStream()
    controller = _controller(stream)
    controller._last_channel_colors = COLORS
    controller._last_send_time = time.monotonic() - 6.0
    controller.pause_sending()

    controller._maybe_send_keepalive()

    assert stream.sent == []


class BlockingCapture:
    """Simulates a stuck portal handshake: capture() never returns until
    explicitly released, like a sync thread stuck waiting on the share
    picker after the monitor wakes from a blank."""

    def __init__(self):
        self.released = threading.Event()

    def capture(self):
        self.released.wait()
        return None


def test_keepalive_survives_a_stalled_capture_thread():
    """A capture() call blocked for a long time must not starve the DTLS
    keepalive - it has to run on its own thread, independent of
    _process_frame()/_sync_loop, since a stuck portal handshake can
    legitimately take minutes while waiting on the user."""
    stream = FakeStream()
    controller = SyncController(
        bridge=None,
        capture=BlockingCapture(),
        zone_processor=None,
        color_analyzer=None,
        zone_mapping=None,
        settings=FakeSettings(),
        entertainment_stream=stream,
    )
    controller._last_channel_colors = COLORS
    controller._last_send_time = time.monotonic()
    controller._keepalive_interval = 0.05
    controller._keepalive_poll_interval = 0.02

    controller.start()
    try:
        deadline = time.monotonic() + 2.0
        sent_while_blocked = False
        while time.monotonic() < deadline:
            if stream.sent:
                sent_while_blocked = True
                break
            time.sleep(0.01)
    finally:
        controller.capture.released.set()
        controller.stop()

    assert sent_while_blocked, "keepalive never fired while capture() was blocked"


def test_update_lights_records_last_frame_for_keepalive():
    stream = FakeStream()
    controller = _controller(stream)
    controller._zone_channel_map = {"z1": 1}
    before = time.monotonic()

    controller._update_lights({"z1": ((0.3, 0.3), 120)})

    assert stream.sent == [COLORS]
    assert controller._last_channel_colors == COLORS
    assert controller._last_send_time >= before


def test_update_lights_reconnects_when_entertainment_disconnected():
    """is_connected() only reflects a stream that was connected once and
    has since died (e.g. the local DTLS process exited); nothing else in
    this class re-establishes it, so without a reconnect attempt here the
    lights stay dark forever after any disconnect."""
    stream = FakeStream(connected=False, connect_succeeds=True)
    controller = _controller(stream)
    controller._zone_channel_map = {"z1": 1}

    controller._update_lights({"z1": ((0.3, 0.3), 120)})

    assert stream.connect_calls == 1
    assert stream.sent == [COLORS]


def test_update_lights_rate_limits_reconnect_attempts():
    stream = FakeStream(connected=False, connect_succeeds=False)
    controller = _controller(stream)
    controller._zone_channel_map = {"z1": 1}

    controller._update_lights({"z1": ((0.3, 0.3), 120)})
    controller._update_lights({"z1": ((0.3, 0.3), 120)})

    assert stream.connect_calls == 1
    assert stream.sent == []


def test_keepalive_reconnects_and_resends_last_frame():
    stream = FakeStream(connected=False, connect_succeeds=True)
    controller = _controller(stream)
    controller._last_channel_colors = COLORS
    controller._last_send_time = time.monotonic() - 6.0

    controller._maybe_send_keepalive()

    assert stream.connect_calls == 1
    assert stream.sent == [COLORS]
