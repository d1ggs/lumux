from lumux.config.settings_manager import ReadingModeSettings
from lumux.mode_manager import Mode, ModeManager


class FakeSyncController:
    """Stand-in for SyncController: stop() synchronously invokes the
    registered stop callback, exactly like the real thing does after
    joining its sync thread (see SyncController.stop() in sync.py)."""

    def __init__(self):
        self._running = False
        self._on_stop_callback = None

    def set_on_stop_callback(self, callback):
        self._on_stop_callback = callback

    def is_running(self):
        return self._running

    def start(self):
        self._running = True

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._on_stop_callback:
            self._on_stop_callback()


class FakeEntertainmentStream:
    def __init__(self, connected=True):
        self.entertainment_config_id = "cfg-1"
        self._connected = connected

    def is_connected(self):
        return self._connected

    def disconnect(self, bridge):
        self._connected = False


class FakeReadingController:
    def __init__(self):
        self.activate_calls = []
        self._active = False

    def set_target_lights(self, light_ids):
        pass

    def activate(self, xy=None, brightness=None):
        self.activate_calls.append((xy, brightness))
        self._active = True
        return True

    def is_active(self):
        return self._active

    def deactivate(self, turn_off=True):
        self._active = False


def _make_manager(monkeypatch, auto_activate=True):
    """Build a ModeManager wired like AppContext does, with fakes standing
    in for the sync controller, entertainment stream, and reading
    controller, and with GLib.timeout_add captured instead of actually
    scheduled so tests can fire the pending callback manually."""
    sync_controller = FakeSyncController()
    entertainment_stream = FakeEntertainmentStream(connected=True)
    reading_settings = ReadingModeSettings(auto_activate=auto_activate)

    manager = ModeManager(
        bridge=object(),
        sync_controller=sync_controller,
        entertainment_stream=entertainment_stream,
        reading_mode=reading_settings,
        entertainment_config_id="cfg-1",
    )
    sync_controller.set_on_stop_callback(manager.on_video_sync_stopped)

    fake_reading = FakeReadingController()
    manager._reading_controller = fake_reading

    scheduled = {}

    def fake_timeout_add(interval_ms, func, *args):
        scheduled["func"] = func
        scheduled["args"] = args
        return 1

    monkeypatch.setattr("lumux.mode_manager.GLib.timeout_add", fake_timeout_add)
    monkeypatch.setattr("lumux.mode_manager.HAS_GLIB", True)

    return manager, sync_controller, entertainment_stream, fake_reading, scheduled


def test_turn_off_cancels_pending_reading_activation_armed_during_stop(monkeypatch):
    """Regression test: turn_off() while video sync is active and reading
    auto_activate is on must prevent the reading-mode timer armed by the
    synchronous stop callback from later activating reading mode.

    Sequence (see ModeManager.turn_off / on_video_sync_stopped /
    switch_to_reading / _finish_switch_to_reading in mode_manager.py):
    1. sync_controller.stop() synchronously calls on_video_sync_stopped()
    2. auto_activate is True, so switch_to_reading() runs
    3. entertainment stream is still connected, so it arms a GLib timer
       instead of finishing synchronously, setting
       _reading_activation_pending = True
    4. turn_off() must notice and cancel this before returning
    5. when the timer callback fires later, it must be a no-op
    """
    manager, sync_controller, entertainment_stream, fake_reading, scheduled = (
        _make_manager(monkeypatch, auto_activate=True)
    )

    # Get into VIDEO mode with sync running and entertainment connected.
    manager.current_mode = Mode.VIDEO
    sync_controller.start()
    assert entertainment_stream.is_connected()

    result = manager.turn_off(turn_off_lights=True)
    assert result is True

    # A reading-activation timer must have been armed by the stop callback...
    assert "func" in scheduled
    # ...but turn_off() must have cancelled it before returning.
    assert manager._reading_activation_pending is False
    assert manager.current_mode == Mode.OFF

    # Simulate the ~1s GLib timeout firing after turn_off() has returned.
    func = scheduled["func"]
    args = scheduled["args"]
    func(*args)

    # The stale callback must be a no-op: reading mode must NOT have been
    # activated, and the mode must remain OFF.
    assert fake_reading.activate_calls == []
    assert manager.current_mode == Mode.OFF


def test_switch_to_reading_still_activates_when_not_interrupted(monkeypatch):
    """Sanity check that the new guard doesn't break the normal path: a
    reading activation that is never cancelled should still complete when
    its timer fires."""
    manager, sync_controller, entertainment_stream, fake_reading, scheduled = (
        _make_manager(monkeypatch, auto_activate=False)
    )

    manager.current_mode = Mode.VIDEO
    sync_controller.start()

    assert manager.switch_to_reading() is True
    assert manager._reading_activation_pending is True

    func = scheduled["func"]
    args = scheduled["args"]
    result = func(*args)

    assert result is False  # GLib timeout convention: don't reschedule
    assert fake_reading.activate_calls == [((0.5, 0.4), 150)]
    assert manager.current_mode == Mode.READING
