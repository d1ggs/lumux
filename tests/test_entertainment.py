"""Tests for EntertainmentStream's blackout latch.

The latch guarantees that black is the FINAL frame on the wire during the
urgent suspend path: a sync-thread frame that passed its pause check just
before pause_sending() took effect must be dropped rather than landing
after the blackout and re-lighting the lamp (see Task 14 review).
"""

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


class _ExitedProc:
    returncode = 1

    def __init__(self, stderr=b"SSL3 alert read:fatal:handshake failure\n"):
        self._stderr = stderr
        self.communicate_calls = 0

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        self.communicate_calls += 1
        return b"", self._stderr


def test_unexpected_dtls_exit_is_logged_once(capsys):
    """A bridge-side session kill used to be invisible: only the next
    reconnect showed up in the log, never why the old session died."""
    stream, _ = _make_connected_stream()
    proc = _ExitedProc()
    stream._openssl_proc = proc

    assert stream.is_connected() is False
    assert stream.is_connected() is False

    out = capsys.readouterr().out
    assert out.count("DTLS session ended unexpectedly (exit code 1)") == 1
    assert "handshake failure" in out
    assert proc.communicate_calls == 1


import subprocess
import sys
import time as _time


def _fake_openssl(script):
    """Real subprocess standing in for openssl s_client (stderr protocol)."""
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _handshake_stream(script, timeout=2.0):
    stream, _ = _make_connected_stream()
    stream._connected = False
    stream._handshake_timeout = timeout
    stream._openssl_proc = _fake_openssl(script)
    return stream


def test_handshake_waits_for_openssl_to_report_it_established():
    """A slow handshake (the bridge right after an external session stop)
    used to be declared done after a fixed 1.3s; frames written before the
    real handshake piled up in the pipe and went out as one oversized
    datagram (EMSGSIZE), killing the session."""
    stream = _handshake_stream(
        "import sys, time; sys.stderr.write('Connecting to x\\n'); sys.stderr.flush();"
        "time.sleep(1.2); sys.stderr.write('CONNECTION ESTABLISHED\\nProtocol version: DTLSv1.2\\n');"
        "sys.stderr.flush(); time.sleep(5)"
    )
    started = _time.monotonic()

    assert stream._wait_for_handshake() is True
    assert _time.monotonic() - started >= 1.2
    assert stream._openssl_proc.poll() is None
    stream._openssl_proc.kill()


def test_handshake_fails_when_openssl_exits_without_establishing():
    stream = _handshake_stream(
        "import sys; sys.stderr.write('handshake failure\\n'); sys.exit(1)"
    )

    assert stream._wait_for_handshake() is False


def test_handshake_times_out_and_kills_openssl():
    stream = _handshake_stream("import time; time.sleep(30)", timeout=0.5)
    proc = stream._openssl_proc
    started = _time.monotonic()

    assert stream._wait_for_handshake() is False
    assert _time.monotonic() - started < 3
    assert proc.poll() is not None


def test_openssl_command_reports_handshake_completion():
    stream, _ = _make_connected_stream()

    cmd = stream._build_openssl_command()

    assert "-brief" in cmd
    assert "-quiet" not in cmd
    assert "-ign_eof" in cmd and "-nocommands" in cmd
