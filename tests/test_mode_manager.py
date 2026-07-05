from lumux.config.settings_manager import ReadingModeSettings
from lumux.mode_manager import Mode, ModeManager


class FakeSyncController:
    """Stand-in for SyncController: stop() synchronously invokes the
    registered stop callback, exactly like the real thing does after
    joining its sync thread (see SyncController.stop() in sync.py)."""

    def __init__(self, events=None):
        self._running = False
        self._on_stop_callback = None
        self._send_paused = False
        # Order-recording list, shared with FakeEntertainmentStream/
        # FakeBridge/FakeBridgeClient when constructed by _make_manager, so
        # tests can assert the relative order of pause_sending/blackout/
        # deactivate/set_light_state/disconnect calls.
        self.events = events if events is not None else []

    def set_on_stop_callback(self, callback):
        self._on_stop_callback = callback

    def is_running(self):
        return self._running

    def start(self):
        self._running = True
        self._send_paused = False

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._on_stop_callback:
            self._on_stop_callback()

    def pause_sending(self):
        self._send_paused = True
        self.events.append(("pause_sending",))


class FakeEntertainmentStream:
    def __init__(self, connected=True, light_to_channel=None, events=None):
        self.entertainment_config_id = "cfg-1"
        self._connected = connected
        # Keys are fake entertainment-*service* rids (not light rids) -
        # mirrors the real EntertainmentStream.light_to_channel, which maps
        # rtype: "entertainment" resource IDs to channel numbers. Resolving
        # these to actual light rids is HueBridge.resolve_light_ids()'s job.
        self.light_to_channel = (
            light_to_channel
            if light_to_channel is not None
            else {"ent-1": 0, "ent-2": 1}
        )
        # Order-recording list, shared with FakeBridge/FakeBridgeClient when
        # both are constructed by _make_manager, so tests can assert the
        # relative order of deactivate/set_light_state/disconnect calls.
        self.events = events if events is not None else []

    def is_connected(self):
        return self._connected

    def disconnect(self, bridge):
        self._connected = False
        self.events.append(("disconnect",))

    def blackout(self):
        self.events.append(("blackout",))


class FakeBridgeClient:
    def __init__(self, events=None):
        self.calls = []
        self.timeouts = []
        self.events = events if events is not None else []

    def set_light_state(self, light_id, payload, timeout=None):
        self.calls.append((light_id, payload))
        self.timeouts.append(timeout)
        self.events.append(("set_light_state", light_id, payload, timeout))


class FakeBridge:
    """Fake HueBridge. resolve_light_ids() mirrors the real
    HueBridge.resolve_light_ids(), translating entertainment-service rids
    to light rids via a fixed fake mapping."""

    ENTERTAINMENT_TO_LIGHT = {
        "ent-1": "light-1",
        "ent-2": "light-2",
        "ent-3": "light-3",
    }

    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.client = FakeBridgeClient(events=self.events)
        self.resolve_light_ids_calls = 0
        self.deactivate_calls = []

    def resolve_light_ids(self, entertainment_rids, timeout=None):
        self.resolve_light_ids_calls += 1
        return [
            self.ENTERTAINMENT_TO_LIGHT[rid]
            for rid in entertainment_rids
            if rid in self.ENTERTAINMENT_TO_LIGHT
        ]

    def deactivate_entertainment_streaming(self, config_id, timeout=None):
        self.deactivate_calls.append(config_id)
        self.events.append(("deactivate_entertainment_streaming", config_id, timeout))
        return True


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


def _make_manager(monkeypatch, auto_activate=True, light_to_channel=None):
    """Build a ModeManager wired like AppContext does, with fakes standing
    in for the sync controller, entertainment stream, bridge, and reading
    controller, and with GLib.timeout_add captured instead of actually
    scheduled so tests can fire the pending callback manually."""
    events = []
    sync_controller = FakeSyncController(events=events)
    entertainment_stream = FakeEntertainmentStream(
        connected=True, light_to_channel=light_to_channel, events=events
    )
    reading_settings = ReadingModeSettings(auto_activate=auto_activate)
    bridge = FakeBridge(events=events)

    manager = ModeManager(
        bridge=bridge,
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

    return (
        manager,
        sync_controller,
        entertainment_stream,
        fake_reading,
        scheduled,
        bridge,
    )


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
    manager, sync_controller, entertainment_stream, fake_reading, scheduled, bridge = (
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


def test_turn_off_turns_off_lights_even_when_auto_activate_disconnects_stream_first(
    monkeypatch,
):
    """Regression test for Bug A (Task 12): with reading_mode.auto_activate
    True and the entertainment stream connected, sync_controller.stop()
    synchronously invokes on_video_sync_stopped() -> switch_to_reading(),
    which itself disconnects the entertainment stream as a side effect -
    before this fix, turn_off()'s light-off block was gated on
    `entertainment_stream.is_connected()`, which was already False by the
    time turn_off() reached it, so the lights were silently never turned
    off (no "Turning off N lights" log line at all in the live re-test).
    The rid capture must happen before sync_controller.stop() runs, and the
    light-off loop must run regardless of who performed the disconnect."""
    manager, sync_controller, entertainment_stream, fake_reading, scheduled, bridge = (
        _make_manager(
            monkeypatch,
            auto_activate=True,
            light_to_channel={"ent-1": 0, "ent-2": 1},
        )
    )

    manager.current_mode = Mode.VIDEO
    sync_controller.start()
    assert entertainment_stream.is_connected()

    result = manager.turn_off(turn_off_lights=True)
    assert result is True

    # switch_to_reading()'s synchronous stop callback disconnected the
    # stream as a side effect, before turn_off()'s own connected-check ran.
    assert not entertainment_stream.is_connected()

    # The lights must still have been turned off via their resolved ids,
    # captured before the stream was disconnected out from under turn_off().
    assert sorted(bridge.client.calls) == sorted(
        [
            ("light-1", {"on": {"on": False}}),
            ("light-2", {"on": {"on": False}}),
        ]
    )


def test_switch_to_reading_still_activates_when_not_interrupted(monkeypatch):
    """Sanity check that the new guard doesn't break the normal path: a
    reading activation that is never cancelled should still complete when
    its timer fires."""
    manager, sync_controller, entertainment_stream, fake_reading, scheduled, bridge = (
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


def test_turn_off_sends_explicit_off_command_for_video_lights(monkeypatch):
    """Regression test for the bug found during the live suspend test:
    turn_off(turn_off_lights=True) in VIDEO mode must not just disconnect
    the entertainment stream (which merely stops the DTLS/streaming
    connection) - it must also send an explicit REST "off" command for
    every light the stream was driving, or the lights simply freeze at
    their last streamed color."""
    manager, sync_controller, entertainment_stream, fake_reading, scheduled, bridge = (
        _make_manager(
            monkeypatch,
            auto_activate=False,
            light_to_channel={"ent-1": 0, "ent-2": 1, "ent-3": 2},
        )
    )

    manager.current_mode = Mode.VIDEO
    assert entertainment_stream.is_connected()

    result = manager.turn_off(turn_off_lights=True)
    assert result is True

    assert not entertainment_stream.is_connected()
    # set_light_state must be called with the *resolved* light rids, not
    # the raw entertainment-service rids from light_to_channel.
    assert sorted(bridge.client.calls) == sorted(
        [
            ("light-1", {"on": {"on": False}}),
            ("light-2", {"on": {"on": False}}),
            ("light-3", {"on": {"on": False}}),
        ]
    )


def test_turn_off_without_turn_off_lights_sends_no_off_commands(monkeypatch):
    """turn_off(turn_off_lights=False) must not send any explicit "off"
    REST commands, even though the entertainment stream still gets
    disconnected."""
    manager, sync_controller, entertainment_stream, fake_reading, scheduled, bridge = (
        _make_manager(monkeypatch, auto_activate=False)
    )

    manager.current_mode = Mode.VIDEO
    assert entertainment_stream.is_connected()

    result = manager.turn_off(turn_off_lights=False)
    assert result is True

    assert not entertainment_stream.is_connected()
    assert bridge.client.calls == []


def test_turn_off_urgent_sends_lights_off_before_network_teardown(monkeypatch):
    """Task 14 regression: the Task 13 reordering still lost the race on
    live suspend test #4 - every REST failure in the chain is silent
    (`except BridgeError: return False`, no log), and sync_controller.stop()
    (thread join + portal Close()) plus the deactivate PUT's default 5s
    timeout meant the off commands went out long after the network window
    closed, with no error logged to explain why.

    urgent=True must now, in order: (1) suppress the auto-activate-to-reading
    detour and pause the DTLS sender (sync_controller.pause_sending(), NOT
    sync_controller.stop() - that expensive join/portal-close teardown is
    deferred to the very end); (2) stream 3 black frames over the
    already-open DTLS socket - immune to the REST/network race entirely;
    (3) deactivate the entertainment session directly, with a short
    timeout=2; (4) send the off loop (cached ids, no live
    resolve_light_ids() round-trip) twice, 0.3s apart, also with
    timeout=2; (5) only then run sync_controller.stop() and the normal
    stream disconnect."""
    manager, sync_controller, entertainment_stream, fake_reading, scheduled, bridge = (
        _make_manager(
            monkeypatch,
            auto_activate=True,
            light_to_channel={"ent-1": 0, "ent-2": 1},
        )
    )

    sleeps = []
    monkeypatch.setattr("lumux.mode_manager.time.sleep", lambda s: sleeps.append(s))

    manager.current_mode = Mode.VIDEO
    sync_controller.start()
    # Simulate the cache switch_to_video() would have populated.
    manager._video_light_ids = ["light-1", "light-2"]

    result = manager.turn_off(turn_off_lights=True, urgent=True)
    assert result is True

    # The synchronous auto-activate detour must never have run.
    assert fake_reading.activate_calls == []
    assert "func" not in scheduled
    assert manager.current_mode == Mode.OFF
    assert manager._suppress_auto_activate is False  # reset after use

    # The cache was used - zero live resolve_light_ids() round-trips.
    assert bridge.resolve_light_ids_calls == 0

    # Ordering: pause_sending, then 3 blackout frames, then deactivate,
    # then the off loop twice, then disconnect.
    kinds = [event[0] for event in bridge.events]
    assert kinds == [
        "pause_sending",
        "blackout",
        "blackout",
        "blackout",
        "deactivate_entertainment_streaming",
        "set_light_state",
        "set_light_state",
        "set_light_state",
        "set_light_state",
        "disconnect",
    ]
    assert sleeps == [0.1, 0.05, 0.05, 0.05, 0.3]
    assert sorted(bridge.client.calls) == sorted(
        [
            ("light-1", {"on": {"on": False}}),
            ("light-2", {"on": {"on": False}}),
        ]
        * 2
    )

    # Every REST call in the urgent path must use the short 2s timeout,
    # not the client's normal 5s default - this is the failure-is-loud-and-
    # fast half of the fix.
    deactivate_events = [
        event
        for event in bridge.events
        if event[0] == "deactivate_entertainment_streaming"
    ]
    assert deactivate_events == [("deactivate_entertainment_streaming", "cfg-1", 2)]
    assert bridge.client.timeouts == [2, 2, 2, 2]


def test_switch_to_video_caches_resolved_light_ids(monkeypatch):
    """Task 13: switch_to_video() must resolve and cache the light ids it
    drives right away, while the network is guaranteed to be up. This
    removes the get_devices() REST round-trip from the suspend path
    entirely - the urgent turn_off() path consumes this cache instead."""
    manager, sync_controller, entertainment_stream, fake_reading, scheduled, bridge = (
        _make_manager(
            monkeypatch,
            auto_activate=False,
            light_to_channel={"ent-1": 0, "ent-2": 1},
        )
    )

    assert manager._video_light_ids == []

    result = manager.switch_to_video()

    assert result is True
    assert manager._video_light_ids == ["light-1", "light-2"]


def test_turn_off_non_urgent_leaves_auto_activate_and_deactivate_untouched(
    monkeypatch,
):
    """Task 13: the default (non-urgent, user-initiated stop) path must be
    byte-for-byte unaffected by the new urgent flag - auto-activate to
    reading mode still runs (and turn_off still cancels its pending timer,
    per the Task 11 regression test), and the new direct
    deactivate_entertainment_streaming() short-circuit used by the urgent
    path must never fire here."""
    manager, sync_controller, entertainment_stream, fake_reading, scheduled, bridge = (
        _make_manager(
            monkeypatch,
            auto_activate=True,
            light_to_channel={"ent-1": 0, "ent-2": 1},
        )
    )

    manager.current_mode = Mode.VIDEO
    sync_controller.start()

    result = manager.turn_off(turn_off_lights=True)
    assert result is True

    # Auto-activate still armed a reading-activation timer (and turn_off()
    # cancelled it) - the pre-existing Task 11 behavior, untouched.
    assert "func" in scheduled
    assert manager._reading_activation_pending is False
    assert manager._suppress_auto_activate is False

    # The urgent-only direct deactivate call must not have been used.
    assert bridge.deactivate_calls == []

    # The normal path still resolves ids live (no cache populated here).
    assert bridge.resolve_light_ids_calls == 1

    # The urgent-only DTLS blackout path must never fire on the normal
    # (non-urgent) turn_off() - it still relies solely on disconnect().
    kinds = [event[0] for event in bridge.events]
    assert "pause_sending" not in kinds
    assert "blackout" not in kinds
