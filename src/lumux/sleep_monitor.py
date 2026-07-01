"""Suspend/resume handling via systemd-logind's PrepareForSleep signal."""

import os
from typing import Callable, Optional

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from lumux.utils.logging import timed_print


class SleepMonitor:
    """Runs callbacks around system suspend, holding a logind delay inhibitor.

    The delay inhibitor makes logind wait (up to its InhibitDelayMaxSec,
    default 5s) for our sleep handler to finish before actually suspending —
    without it the lights-off network calls would race the suspend. Gio is
    used instead of pydbus because logind's Inhibit() returns a unix fd,
    which pydbus cannot receive.
    """

    def __init__(
        self,
        on_sleep: Callable[[], None],
        on_wake: Callable[[], None],
        bus=None,
    ):
        self._on_sleep = on_sleep
        self._on_wake = on_wake
        self._bus = bus
        self._subscription_id: Optional[int] = None
        self._inhibitor_fd: Optional[int] = None

    def start(self) -> bool:
        """Subscribe to PrepareForSleep. Returns False (non-fatal) on failure."""
        try:
            if self._bus is None:
                self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self._subscription_id = self._bus.signal_subscribe(
                "org.freedesktop.login1",
                "org.freedesktop.login1.Manager",
                "PrepareForSleep",
                "/org/freedesktop/login1",
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_prepare_for_sleep,
            )
        except Exception as e:
            timed_print(f"SleepMonitor: unavailable ({e}); sleep handling disabled")
            return False
        self._take_inhibitor()
        return True

    def stop(self) -> None:
        if self._bus is not None and self._subscription_id is not None:
            self._bus.signal_unsubscribe(self._subscription_id)
            self._subscription_id = None
        self._release_inhibitor()

    def _on_prepare_for_sleep(
        self, connection, sender, path, interface, signal, params
    ):
        (going_to_sleep,) = params.unpack()
        if going_to_sleep:
            timed_print("SleepMonitor: system going to sleep")
            try:
                self._on_sleep()
            finally:
                # Always let the suspend proceed, even if lights-off failed.
                self._release_inhibitor()
        else:
            timed_print("SleepMonitor: system woke up")
            self._take_inhibitor()
            self._on_wake()

    def _take_inhibitor(self) -> None:
        if self._inhibitor_fd is not None:
            return
        try:
            result, fd_list = self._bus.call_with_unix_fd_list_sync(
                "org.freedesktop.login1",
                "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager",
                "Inhibit",
                GLib.Variant(
                    "(ssss)",
                    (
                        "sleep",
                        "Lumux",
                        "Turning off Hue lights before sleep",
                        "delay",
                    ),
                ),
                GLib.VariantType("(h)"),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
                None,
            )
            handle_index = result.unpack()[0]
            self._inhibitor_fd = fd_list.get(handle_index)
        except Exception as e:
            timed_print(f"SleepMonitor: could not take sleep inhibitor ({e})")
            self._inhibitor_fd = None

    def _release_inhibitor(self) -> None:
        if self._inhibitor_fd is not None:
            try:
                os.close(self._inhibitor_fd)
            except OSError:
                pass
            self._inhibitor_fd = None
