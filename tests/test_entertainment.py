"""Tests for EntertainmentStream's blackout latch.

The latch guarantees that black is the FINAL frame on the wire during the
urgent suspend path: a sync-thread frame that passed its pause check just
before pause_sending() took effect must be dropped rather than landing
after the blackout and re-lighting the lamp (see Task 14 review).
"""

import subprocess

from lumux.entertainment import ChannelInfo, EntertainmentStream


def _make_connected_stream():
    """Build a stream that believes it is connected, with sends captured
    instead of hitting a real DTLS socket."""
    stream = EntertainmentStream(
        bridge_ip="192.0.2.1",
        app_key="app-key",
        client_key="00" * 16,
        # Must be UUID-shaped: 36 ASCII bytes are packed into the message
        # header verbatim.
        entertainment_config_id="00000000-0000-0000-0000-000000000000",
    )
    stream._channels = {
        0: ChannelInfo(channel_id=0, position={}, members=[]),
        1: ChannelInfo(channel_id=1, position={}, members=[]),
    }
    stream._init_message_buffer()
    stream._connected = True

    sent = []
    stream._send_dtls_message = lambda message: sent.append(bytes(message))
    return stream, sent


def test_blackout_sends_zero_rgb_for_all_channels():
    stream, sent = _make_connected_stream()

    stream.blackout()

    assert len(sent) == 1
    # Per-channel payload: channel id byte + 6 bytes of 16-bit RGB, all zero.
    message = sent[0]
    channel_data = message[52:]  # header(16) + config id(36)
    assert len(channel_data) == 7 * 2
    for offset in (0, 7):
        assert channel_data[offset + 1 : offset + 7] == b"\x00" * 6


def test_send_colors_xy_is_dropped_after_blackout():
    stream, sent = _make_connected_stream()

    stream.blackout()
    stream.send_colors_xy({0: ((0.5, 0.4), 200), 1: ((0.5, 0.4), 200)})

    # Only the blackout frame went out; the late color frame was latched.
    assert len(sent) == 1


def test_reconnect_clears_the_black_latch():
    stream, sent = _make_connected_stream()

    stream.blackout()
    assert stream._black_latched is True

    # Drive a real connect() with the handshake internals stubbed out; a
    # successful connect must clear the latch so wake-resume syncs again.
    stream._fetch_entertainment_config = lambda bridge: {"channels": []}
    stream._fetch_application_id = lambda bridge: "app-id"
    stream._activate_streaming = lambda bridge: True
    stream._establish_dtls_connection = lambda: True
    assert stream.connect(object()) is True

    assert stream._black_latched is False
    stream._channels = {0: ChannelInfo(channel_id=0, position={}, members=[])}
    stream._init_message_buffer()
    stream.send_colors_xy({0: ((0.5, 0.4), 200)})

    assert len(sent) == 2


def test_send_colors_xy_still_works_without_blackout():
    stream, sent = _make_connected_stream()

    stream.send_colors_xy({0: ((0.5, 0.4), 200), 1: ((0.2, 0.3), 100)})

    assert len(sent) == 1


class _ClosedDtls:
    """DtlsPskClient stand-in whose session the peer has ended."""

    close_reason = "peer closed the session (close_notify)"

    def is_open(self):
        return False

    def close(self):
        pass


def test_unexpected_dtls_close_is_logged_once(capsys):
    """A bridge-side session kill used to be invisible: only the next
    reconnect showed up in the log, never why the old session died."""
    stream, _ = _make_connected_stream()
    stream._dtls_socket = _ClosedDtls()

    assert stream.is_connected() is False
    assert stream.is_connected() is False

    out = capsys.readouterr().out
    assert out.count("DTLS session ended unexpectedly") == 1
    assert "close_notify" in out


def test_establish_dtls_connection_uses_in_process_client(monkeypatch):
    """The PSK must never be handed to a subprocess (visible in argv)."""
    import lumux.entertainment as ent

    created = {}

    class FakeClient:
        handshake_seconds = 0.03

        def __init__(self, host, port, identity, psk_hex, handshake_timeout):
            created.update(host=host, port=port, identity=identity, psk=psk_hex)

        def connect(self):
            pass

    monkeypatch.setattr(ent, "DtlsPskClient", FakeClient)
    def no_subprocess(*args, **kwargs):
        raise AssertionError("spawned a subprocess")

    monkeypatch.setattr(subprocess, "Popen", no_subprocess)
    stream, _ = _make_connected_stream()
    stream._application_id = "app-id"

    assert stream._establish_dtls_connection() is True
    assert created == {"host": "192.0.2.1", "port": 2100, "identity": "app-id", "psk": "00" * 16}


def test_failed_handshake_returns_false(monkeypatch):
    import lumux.entertainment as ent
    from lumux.dtls import DtlsError

    class FailingClient:
        def __init__(self, *a, **k):
            pass

        def connect(self):
            raise DtlsError("handshake timed out after 5.0s")

    monkeypatch.setattr(ent, "DtlsPskClient", FailingClient)
    stream, _ = _make_connected_stream()

    assert stream._establish_dtls_connection() is False
