"""Minimal in-process DTLS 1.2 PSK client on top of the system libssl.

Replaces the `openssl s_client` subprocess, which had two problems:
- the PSK (the bridge's client key) had to be passed on its command line,
  readable by any local process via /proc/<pid>/cmdline;
- frames went through its stdin pipe, so frames queued while the handshake
  was still running were read back as one chunk and sent as one oversized
  datagram (EMSGSIZE), killing the session.

Here the PSK only ever lives in this process, and every send() is exactly
one DTLS record in one datagram, as the Hue Entertainment API expects.

Python's ssl module has no DTLS, hence ctypes against libssl.so.3 (present
in the GNOME flatpak runtime and on any OpenSSL 3 system). Only the handful
of calls needed by a blocking-free client are bound.
"""

import ctypes
import ctypes.util
import errno
import select
import socket
import threading
import time
from typing import Optional

CIPHERS = b"PSK-AES128-GCM-SHA256:PSK-CHACHA20-POLY1305"

_DTLS1_2_VERSION = 0xFEFD
_SSL_CTRL_SET_MIN_PROTO_VERSION = 123
_SSL_CTRL_SET_MAX_PROTO_VERSION = 124
_DTLS_CTRL_GET_TIMEOUT = 73
_DTLS_CTRL_HANDLE_TIMEOUT = 74
_BIO_NOCLOSE = 0
_BIO_CTRL_DGRAM_SET_CONNECTED = 32

_SSL_ERROR_SSL = 1
_SSL_ERROR_WANT_READ = 2
_SSL_ERROR_WANT_WRITE = 3
_SSL_ERROR_SYSCALL = 5
_SSL_ERROR_ZERO_RETURN = 6

_PSK_CLIENT_CB = ctypes.CFUNCTYPE(
    ctypes.c_uint,
    ctypes.c_void_p,  # SSL *
    ctypes.c_char_p,  # const char *hint
    ctypes.c_void_p,  # char *identity
    ctypes.c_uint,  # max_identity_len
    ctypes.c_void_p,  # unsigned char *psk
    ctypes.c_uint,  # max_psk_len
)


class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class DtlsError(Exception):
    pass


_lib = None
_lib_lock = threading.Lock()


def _libssl():
    global _lib
    with _lib_lock:
        if _lib is not None:
            return _lib
        name = ctypes.util.find_library("ssl") or "libssl.so.3"
        lib = ctypes.CDLL(name)
        vp, c_int, c_long = ctypes.c_void_p, ctypes.c_int, ctypes.c_long
        signatures = {
            "DTLS_client_method": ([], vp),
            "SSL_CTX_new": ([vp], vp),
            "SSL_CTX_free": ([vp], None),
            "SSL_CTX_ctrl": ([vp, c_int, c_long, vp], c_long),
            "SSL_CTX_set_cipher_list": ([vp, ctypes.c_char_p], c_int),
            "SSL_CTX_set_psk_client_callback": ([vp, _PSK_CLIENT_CB], None),
            "SSL_new": ([vp], vp),
            "SSL_free": ([vp], None),
            "SSL_set_bio": ([vp, vp, vp], None),
            "SSL_connect": ([vp], c_int),
            "SSL_write": ([vp, ctypes.c_char_p, c_int], c_int),
            "SSL_read": ([vp, ctypes.c_char_p, c_int], c_int),
            "SSL_shutdown": ([vp], c_int),
            "SSL_get_error": ([vp, c_int], c_int),
            "SSL_ctrl": ([vp, c_int, c_long, vp], c_long),
            "BIO_new_dgram": ([c_int, c_int], vp),
            "BIO_ctrl": ([vp, c_int, c_long, vp], c_long),
            "BIO_ADDR_new": ([], vp),
            "BIO_ADDR_free": ([vp], None),
            "BIO_ADDR_rawmake": ([vp, c_int, vp, ctypes.c_size_t, ctypes.c_ushort], c_int),
            "ERR_get_error": ([], ctypes.c_ulong),
            "ERR_error_string_n": ([ctypes.c_ulong, ctypes.c_char_p, ctypes.c_size_t], None),
            "ERR_clear_error": ([], None),
        }
        for fn, (args, res) in signatures.items():
            f = getattr(lib, fn)
            f.argtypes = args
            f.restype = res
        _lib = lib
        return lib


def _ssl_errors(lib) -> str:
    msgs = []
    while True:
        code = lib.ERR_get_error()
        if not code:
            break
        buf = ctypes.create_string_buffer(256)
        lib.ERR_error_string_n(code, buf, len(buf))
        msgs.append(buf.value.decode(errors="replace"))
    return "; ".join(msgs)


class DtlsPskClient:
    """One DTLS 1.2 PSK session to host:port over a connected UDP socket.

    Thread-safe: all libssl calls on the session are serialized by a lock.
    The socket is non-blocking; timeouts and DTLS retransmits are driven
    here with select() + DTLSv1_handle_timeout.
    """

    def __init__(
        self,
        host: str,
        port: int,
        psk_identity: str,
        psk_hex: str,
        handshake_timeout: float = 5.0,
    ):
        self.host = host
        self.port = port
        self._identity = psk_identity.encode()
        self._psk = bytes.fromhex(psk_hex)
        self.handshake_timeout = handshake_timeout
        self.close_reason: Optional[str] = None
        self.handshake_seconds: Optional[float] = None

        self._lib = _libssl()
        self._lock = threading.Lock()
        self._ctx = None
        self._ssl = None
        self._sock: Optional[socket.socket] = None
        self._open = False
        # Keep the ctypes callback object alive as long as the SSL_CTX.
        self._psk_cb = _PSK_CLIENT_CB(self._on_psk)

    def _on_psk(self, ssl, hint, identity, max_identity_len, psk, max_psk_len):
        if len(self._identity) + 1 > max_identity_len or len(self._psk) > max_psk_len:
            return 0
        ctypes.memmove(identity, self._identity + b"\0", len(self._identity) + 1)
        ctypes.memmove(psk, self._psk, len(self._psk))
        return len(self._psk)

    def connect(self) -> None:
        """Run the handshake; raises DtlsError on failure or timeout."""
        with self._lock:
            self._close_locked(send_notify=False)
            self.close_reason = None
            try:
                self._connect_locked()
            except DtlsError:
                self._close_locked(send_notify=False)
                raise

    def _connect_locked(self) -> None:
        lib = self._lib
        lib.ERR_clear_error()

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.connect((self.host, self.port))
        self._sock.setblocking(False)

        self._ctx = lib.SSL_CTX_new(lib.DTLS_client_method())
        if not self._ctx:
            raise DtlsError(f"SSL_CTX_new failed: {_ssl_errors(lib)}")
        lib.SSL_CTX_ctrl(self._ctx, _SSL_CTRL_SET_MIN_PROTO_VERSION, _DTLS1_2_VERSION, None)
        lib.SSL_CTX_ctrl(self._ctx, _SSL_CTRL_SET_MAX_PROTO_VERSION, _DTLS1_2_VERSION, None)
        if lib.SSL_CTX_set_cipher_list(self._ctx, CIPHERS) != 1:
            raise DtlsError(f"no usable PSK cipher: {_ssl_errors(lib)}")
        lib.SSL_CTX_set_psk_client_callback(self._ctx, self._psk_cb)

        self._ssl = lib.SSL_new(self._ctx)
        bio = lib.BIO_new_dgram(self._sock.fileno(), _BIO_NOCLOSE)
        if not self._ssl or not bio:
            raise DtlsError(f"SSL_new/BIO_new_dgram failed: {_ssl_errors(lib)}")
        addr = lib.BIO_ADDR_new()
        try:
            raw = socket.inet_aton(socket.gethostbyname(self.host))
            lib.BIO_ADDR_rawmake(addr, socket.AF_INET, raw, 4, socket.htons(self.port))
            lib.BIO_ctrl(bio, _BIO_CTRL_DGRAM_SET_CONNECTED, 0, addr)
        finally:
            lib.BIO_ADDR_free(addr)
        lib.SSL_set_bio(self._ssl, bio, bio)  # SSL now owns the BIO

        started = time.monotonic()
        deadline = started + self.handshake_timeout
        while True:
            ret = lib.SSL_connect(self._ssl)
            if ret == 1:
                break
            err = lib.SSL_get_error(self._ssl, ret)
            if err not in (_SSL_ERROR_WANT_READ, _SSL_ERROR_WANT_WRITE):
                raise DtlsError(
                    f"handshake failed (SSL error {err}): {_ssl_errors(lib) or 'no details'}"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DtlsError(f"handshake timed out after {self.handshake_timeout:.1f}s")
            tv = _Timeval()
            wait = remaining
            if lib.SSL_ctrl(self._ssl, _DTLS_CTRL_GET_TIMEOUT, 0, ctypes.byref(tv)) > 0:
                wait = min(wait, tv.tv_sec + tv.tv_usec / 1e6)
            readable, _, _ = select.select([self._sock], [], [], max(wait, 0.0))
            if not readable:
                # Retransmit the current flight if its timer expired.
                lib.SSL_ctrl(self._ssl, _DTLS_CTRL_HANDLE_TIMEOUT, 0, None)

        self.handshake_seconds = time.monotonic() - started
        self._open = True

    def send(self, data: bytes) -> None:
        """Send one record (= one datagram). Raises DtlsError if the session died."""
        with self._lock:
            if not self._open:
                raise DtlsError(self.close_reason or "not connected")
            self._poll_incoming_locked()
            if not self._open:
                raise DtlsError(self.close_reason or "closed by peer")
            ret = self._lib.SSL_write(self._ssl, data, len(data))
            if ret > 0:
                return
            err = self._lib.SSL_get_error(self._ssl, ret)
            if err == _SSL_ERROR_WANT_WRITE:
                return  # socket buffer full: drop this frame, the next follows
            self._fail_locked(f"write failed (SSL error {err})")
            raise DtlsError(self.close_reason)

    def is_open(self) -> bool:
        with self._lock:
            if self._open:
                self._poll_incoming_locked()
            return self._open

    def _poll_incoming_locked(self) -> None:
        """Drain anything the peer sent; notice close_notify and fatal alerts.

        The bridge never sends application data on this channel, so incoming
        records are only ever alerts (the session-stop close_notify).
        """
        buf = ctypes.create_string_buffer(2048)
        while self._open:
            try:
                readable, _, _ = select.select([self._sock], [], [], 0)
            except (OSError, ValueError):
                self._fail_locked("socket error")
                return
            if not readable:
                return
            ret = self._lib.SSL_read(self._ssl, buf, len(buf))
            if ret > 0:
                continue
            err = self._lib.SSL_get_error(self._ssl, ret)
            if err == _SSL_ERROR_WANT_READ:
                return
            if err == _SSL_ERROR_ZERO_RETURN:
                self._fail_locked("peer closed the session (close_notify)")
            else:
                self._fail_locked(f"read failed (SSL error {err})")

    def _fail_locked(self, reason: str) -> None:
        details = _ssl_errors(self._lib)
        sock_err = None
        if self._sock is not None:
            try:
                sock_err = self._sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            except OSError:
                pass
        if sock_err:
            name = errno.errorcode.get(sock_err, str(sock_err))
            details = "; ".join(filter(None, [details, f"socket: {name}"]))
        self.close_reason = f"{reason}: {details}" if details else reason
        self._open = False

    def close(self) -> None:
        with self._lock:
            self._close_locked(send_notify=True)

    def _close_locked(self, send_notify: bool) -> None:
        lib = self._lib
        if self._ssl:
            if send_notify and self._open:
                lib.SSL_shutdown(self._ssl)
            lib.SSL_free(self._ssl)  # also frees the BIO
            self._ssl = None
        if self._ctx:
            lib.SSL_CTX_free(self._ctx)
            self._ctx = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self._open = False
        lib.ERR_clear_error()

