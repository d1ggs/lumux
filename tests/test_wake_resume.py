"""Wake-from-suspend video sync resume: must outlast a slow WiFi reconnect.

After resume, WiFi re-association + DHCP can take well over a minute
(ath12k is especially slow); the retry loop must keep trying long enough
instead of giving up while the network is still down.
"""

from unittest.mock import patch

from lumux.__main__ import LumuxApp


class FakeModeManager:
    """switch_to_video() fails until `elapsed` reaches network_back_after."""

    def __init__(self, network_back_after):
        self.network_back_after = network_back_after
        self.elapsed = 0  # simulated seconds since wake, advanced by drive_wake
        self.video_active = False
        self.switch_attempts = 0

    def is_video_active(self):
        return self.video_active

    def switch_to_video(self):
        self.switch_attempts += 1
        if self.elapsed >= self.network_back_after:
            self.video_active = True
            return True
        return False


class FakeAppContext:
    def __init__(self, mode_manager):
        self.mode_manager = mode_manager


def make_app(mode_manager):
    app = LumuxApp.__new__(LumuxApp)  # skip Adw.Application init
    app.app_context = FakeAppContext(mode_manager)
    app._resume_video_after_wake = True
    app._wake_resume_attempts = 0
    return app


def drive_wake(app, mode_manager, max_seconds):
    """Fire _on_system_wake and simulate GLib's timeout scheduling.

    A callback returning True re-arms at the same interval; returning False
    stops that timer (it may have scheduled a new one at another interval).
    """
    scheduled = []

    def fake_timeout_add_seconds(secs, fn):
        scheduled.append((secs, fn))
        return 1

    with patch("lumux.__main__.GLib.timeout_add_seconds", fake_timeout_add_seconds):
        app._on_system_wake()
        while scheduled and mode_manager.elapsed < max_seconds:
            secs, fn = scheduled.pop(0)
            mode_manager.elapsed += secs
            if fn():
                scheduled.append((secs, fn))


def test_resumes_when_network_takes_a_minute_to_return():
    mm = FakeModeManager(network_back_after=60)
    app = make_app(mm)
    drive_wake(app, mm, max_seconds=600)
    assert mm.video_active, (
        f"gave up after {mm.switch_attempts} attempts (~{mm.elapsed}s simulated)"
    )


def test_resumes_quickly_when_network_is_already_back():
    mm = FakeModeManager(network_back_after=0)
    app = make_app(mm)
    drive_wake(app, mm, max_seconds=600)
    assert mm.video_active
    assert mm.switch_attempts == 1
    assert mm.elapsed <= 5


def test_gives_up_if_network_never_returns():
    mm = FakeModeManager(network_back_after=10_000)
    app = make_app(mm)
    drive_wake(app, mm, max_seconds=1_000)
    assert not mm.video_active
    # It must stop on its own (drive_wake exits because nothing is scheduled,
    # not because the simulation clock ran out) within a bounded window.
    assert mm.elapsed < 1_000
    assert mm.switch_attempts >= 10


def test_no_resume_scheduled_if_video_was_not_running_before_sleep():
    mm = FakeModeManager(network_back_after=0)
    app = make_app(mm)
    app._resume_video_after_wake = False
    drive_wake(app, mm, max_seconds=600)
    assert mm.switch_attempts == 0
