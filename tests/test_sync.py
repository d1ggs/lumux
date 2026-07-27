import time

from lumux.sync import SyncController


class FakeCapture:
    """Capture stand-in whose portal session is gone: every capture() is None."""

    def capture(self):
        return None


class FakeStream:
    def __init__(self):
        self.sent = []

    def is_connected(self):
        return True

    def send_colors_xy(self, channel_colors):
        self.sent.append(channel_colors)

    def get_channel_positions(self):
        return {}


class FakeSettings:
    fps = 30
    smoothing_factor = 0.3


def _controller(stream):
    return SyncController(
        bridge=None,
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
    repeating the last frame keeps the session alive."""
    stream = FakeStream()
    controller = _controller(stream)
    controller._last_channel_colors = COLORS
    controller._last_send_time = time.monotonic() - 6.0

    controller._process_frame()

    assert stream.sent == [COLORS]


def test_no_keepalive_before_interval_elapses():
    stream = FakeStream()
    controller = _controller(stream)
    controller._last_channel_colors = COLORS
    controller._last_send_time = time.monotonic()

    controller._process_frame()

    assert stream.sent == []


def test_no_keepalive_while_sending_is_paused():
    """The urgent suspend path must stay dark: black is latched as the last
    frame and the keepalive may not repeat the pre-blackout colors."""
    stream = FakeStream()
    controller = _controller(stream)
    controller._last_channel_colors = COLORS
    controller._last_send_time = time.monotonic() - 6.0
    controller.pause_sending()

    controller._process_frame()

    assert stream.sent == []


def test_update_lights_records_last_frame_for_keepalive():
    stream = FakeStream()
    controller = _controller(stream)
    controller._zone_channel_map = {"z1": 1}
    before = time.monotonic()

    controller._update_lights({"z1": ((0.3, 0.3), 120)})

    assert stream.sent == [COLORS]
    assert controller._last_channel_colors == COLORS
    assert controller._last_send_time >= before
