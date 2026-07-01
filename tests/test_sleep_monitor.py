import os
from unittest.mock import patch

from gi.repository import GLib

from lumux.sleep_monitor import SleepMonitor


class FakeFdList:
    def __init__(self, fd):
        self._fd = fd

    def get(self, index):
        assert index == 0
        return self._fd


class FakeSystemBus:
    def __init__(self, inhibit_fails=False):
        self.inhibit_fails = inhibit_fails
        self.signal_callback = None
        self.subscribe_args = None
        self.unsubscribed = []
        self.inhibit_calls = 0
        self._next_id = 7

    def signal_subscribe(self, sender, iface, signal, path, arg0, flags, callback):
        self.subscribe_args = (sender, iface, signal, path)
        self.signal_callback = callback
        return self._next_id

    def signal_unsubscribe(self, sub_id):
        self.unsubscribed.append(sub_id)

    def call_with_unix_fd_list_sync(
        self,
        name,
        path,
        iface,
        method,
        params,
        reply_type,
        flags,
        timeout,
        fd_list,
        cancellable,
    ):
        assert method == "Inhibit"
        assert params.unpack() == (
            "sleep",
            "Lumux",
            "Turning off Hue lights before sleep",
            "delay",
        )
        if self.inhibit_fails:
            raise Exception("access denied")
        self.inhibit_calls += 1
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        return GLib.Variant("(h)", (0,)), FakeFdList(read_fd)

    def fire_prepare_for_sleep(self, going_to_sleep):
        self.signal_callback(
            None,
            "org.freedesktop.login1",
            "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager",
            "PrepareForSleep",
            GLib.Variant("(b)", (going_to_sleep,)),
        )


def _fd_is_open(fd):
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


def test_start_subscribes_to_prepare_for_sleep_and_takes_inhibitor():
    bus = FakeSystemBus()
    monitor = SleepMonitor(on_sleep=lambda: None, on_wake=lambda: None, bus=bus)

    assert monitor.start() is True
    assert bus.subscribe_args == (
        "org.freedesktop.login1",
        "org.freedesktop.login1.Manager",
        "PrepareForSleep",
        "/org/freedesktop/login1",
    )
    assert bus.inhibit_calls == 1
    assert monitor._inhibitor_fd is not None and _fd_is_open(monitor._inhibitor_fd)
    monitor.stop()


def test_sleep_signal_calls_on_sleep_then_releases_inhibitor():
    calls = []
    bus = FakeSystemBus()
    monitor = SleepMonitor(
        on_sleep=lambda: calls.append("sleep"), on_wake=lambda: None, bus=bus
    )
    monitor.start()
    fd = monitor._inhibitor_fd

    bus.fire_prepare_for_sleep(True)

    assert calls == ["sleep"]
    assert monitor._inhibitor_fd is None
    assert not _fd_is_open(fd)


def test_inhibitor_released_even_if_on_sleep_raises():
    def boom():
        raise RuntimeError("bridge unreachable")

    bus = FakeSystemBus()
    monitor = SleepMonitor(on_sleep=boom, on_wake=lambda: None, bus=bus)
    monitor.start()
    fd = monitor._inhibitor_fd

    try:
        bus.fire_prepare_for_sleep(True)
    except RuntimeError:
        pass

    assert monitor._inhibitor_fd is None
    assert not _fd_is_open(fd)


def test_wake_signal_retakes_inhibitor_and_calls_on_wake():
    calls = []
    bus = FakeSystemBus()
    monitor = SleepMonitor(
        on_sleep=lambda: None, on_wake=lambda: calls.append("wake"), bus=bus
    )
    monitor.start()
    bus.fire_prepare_for_sleep(True)
    assert bus.inhibit_calls == 1

    bus.fire_prepare_for_sleep(False)

    assert calls == ["wake"]
    assert bus.inhibit_calls == 2
    assert monitor._inhibitor_fd is not None and _fd_is_open(monitor._inhibitor_fd)
    monitor.stop()


def test_start_returns_false_when_bus_unavailable():
    class BrokenBus:
        def signal_subscribe(self, *args):
            raise Exception("no system bus in sandbox")

    monitor = SleepMonitor(on_sleep=lambda: None, on_wake=lambda: None, bus=BrokenBus())
    assert monitor.start() is False


def test_inhibit_failure_is_non_fatal():
    bus = FakeSystemBus(inhibit_fails=True)
    monitor = SleepMonitor(on_sleep=lambda: None, on_wake=lambda: None, bus=bus)

    assert monitor.start() is True
    assert monitor._inhibitor_fd is None

    bus.fire_prepare_for_sleep(True)  # must not raise
    monitor.stop()
