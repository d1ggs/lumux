"""In-process DTLS 1.2 PSK client, tested against a real `openssl s_server`.

The PSK used to be passed to an `openssl s_client` subprocess on its
command line, readable by any local process via /proc/<pid>/cmdline.
"""

import shutil
import socket
import subprocess
import time

import pytest

from lumux.dtls import DtlsError, DtlsPskClient

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="needs the openssl binary as test server"
)

IDENTITY = "c1058807-1cd8-4b5c-875b-62dea5d1ead2"
PSK_HEX = "00112233445566778899aabbccddeeff"
CIPHER = "PSK-AES128-GCM-SHA256"


def _free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server():
    """openssl s_server speaking DTLS 1.2 PSK; received app data -> stdout."""
    port = _free_udp_port()
    proc = subprocess.Popen(
        [
            "openssl", "s_server", "-dtls1_2", "-accept", f"127.0.0.1:{port}",
            "-nocert", "-psk_identity", IDENTITY, "-psk", PSK_HEX,
            "-cipher", CIPHER, "-quiet",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(0.5)
    yield proc, port
    proc.kill()
    proc.wait()


def _read_exactly(proc, n, timeout=3.0):
    import os
    import select

    data = b""
    deadline = time.monotonic() + timeout
    fd = proc.stdout.fileno()
    while len(data) < n and time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], deadline - time.monotonic())
        if ready:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            data += chunk
    return data


def test_handshake_and_frames_arrive_intact(server):
    proc, port = server
    client = DtlsPskClient("127.0.0.1", port, IDENTITY, PSK_HEX, handshake_timeout=5)

    client.connect()
    frames = [b"HueStream" + bytes([i]) * 50 for i in range(3)]
    for frame in frames:
        client.send(frame)

    assert _read_exactly(proc, sum(map(len, frames))) == b"".join(frames)
    assert client.is_open()
    client.close()


def test_each_send_is_its_own_record(server):
    """Many frames sent back-to-back must not be coalesced into one
    oversized datagram (the EMSGSIZE failure of the stdin-pipe design)."""
    proc, port = server
    client = DtlsPskClient("127.0.0.1", port, IDENTITY, PSK_HEX, handshake_timeout=5)
    client.connect()

    frame = b"F" * 115
    for _ in range(60):
        client.send(frame)

    assert _read_exactly(proc, 115 * 60) == frame * 60
    assert client.is_open()
    client.close()


def test_handshake_times_out_without_server():
    client = DtlsPskClient(
        "127.0.0.1", _free_udp_port(), IDENTITY, PSK_HEX, handshake_timeout=1.0
    )
    started = time.monotonic()

    with pytest.raises(DtlsError):
        client.connect()

    assert time.monotonic() - started < 3
    assert not client.is_open()


def test_wrong_psk_fails_handshake(server):
    _, port = server
    client = DtlsPskClient(
        "127.0.0.1", port, IDENTITY, "ff" * 16, handshake_timeout=3
    )

    with pytest.raises(DtlsError):
        client.connect()


def test_peer_close_is_detected(server):
    """The bridge ends a session with close_notify; the openssl subprocess
    used to exit on it, which is how a dropped session was noticed."""
    proc, port = server
    client = DtlsPskClient("127.0.0.1", port, IDENTITY, PSK_HEX, handshake_timeout=5)
    client.connect()
    client.send(b"hello")
    assert _read_exactly(proc, 5) == b"hello"

    proc.terminate()  # s_server sends close_notify? not guaranteed: also kill below
    proc.wait()
    client.send(b"after close")  # ICMP port unreachable -> next op fails
    time.sleep(0.2)

    deadline = time.monotonic() + 3
    while client.is_open() and time.monotonic() < deadline:
        try:
            client.send(b"x")
        except DtlsError:
            pass
        time.sleep(0.1)
    assert not client.is_open()
    assert client.close_reason


def test_psk_not_on_any_command_line(server):
    _, port = server
    client = DtlsPskClient("127.0.0.1", port, IDENTITY, PSK_HEX, handshake_timeout=5)
    client.connect()

    out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    # The test server itself has it; nothing else may.
    lines = [line for line in out.splitlines() if PSK_HEX in line and "s_server" not in line]
    assert lines == []
    client.close()
