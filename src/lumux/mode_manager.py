"""Mode manager for switching between Video and Reading modes.

Handles the transition logic between:
- Video Mode: DTLS entertainment streaming (continuous)
- Reading Mode: REST API static color (one-time)

These modes are mutually exclusive - entertainment streaming
must be stopped before using REST API control.
"""

import time
from enum import Enum, auto
from typing import List, Optional, Tuple, Callable

try:
    from gi.repository import GLib

    HAS_GLIB = True
except ImportError:
    HAS_GLIB = False

from lumux.config.settings_manager import ReadingModeSettings
from lumux.hue_bridge import HueBridge
from lumux.sync import SyncController
from lumux.entertainment import EntertainmentStream
from lumux.reading_mode import ReadingModeController
from lumux.utils.logging import timed_print


class Mode(Enum):
    """Available lighting modes."""

    OFF = auto()
    VIDEO = auto()
    READING = auto()


class ModeManager:
    """Manages transitions between video and reading modes.

    Ensures proper shutdown of one mode before starting another.
    """

    def __init__(
        self,
        bridge: HueBridge,
        sync_controller: SyncController,
        entertainment_stream: Optional[EntertainmentStream],
        reading_mode: ReadingModeSettings = None,
        entertainment_config_id: str = "",
    ):
        self.bridge = bridge
        self.sync_controller = sync_controller
        self.entertainment_stream = entertainment_stream
        self.reading_settings = reading_mode
        self._entertainment_config_id = entertainment_config_id

        self.current_mode = Mode.OFF
        self._reading_controller: Optional[ReadingModeController] = None
        self._on_mode_changed: Optional[Callable[[Mode], None]] = None

        # Track pending reading mode activation to prevent duplicate calls
        self._reading_activation_pending = False

        # Light ids resolved and cached the moment video sync starts, while
        # the network is guaranteed to be up. Used by the urgent (suspend)
        # path in turn_off() so it never has to make a get_devices() REST
        # round-trip while NetworkManager may already be tearing interfaces
        # down (see Task 13).
        self._video_light_ids: List[str] = []

        # Set (with try/finally) around sync_controller.stop() by the urgent
        # turn_off() path to prevent on_video_sync_stopped() from running the
        # synchronous switch_to_reading() auto-activate detour, which can
        # take ~5s (openssl subprocess teardown + REST deactivate) - time we
        # don't have before the network goes down on suspend.
        self._suppress_auto_activate = False

    def set_mode_changed_callback(self, callback: Callable[[Mode], None]):
        """Set callback to be called when mode changes."""
        self._on_mode_changed = callback

    def _notify_mode_changed(self):
        """Notify listeners of mode change."""
        if self._on_mode_changed:
            self._on_mode_changed(self.current_mode)

    def get_reading_controller(self) -> ReadingModeController:
        """Get or create reading mode controller."""
        if self._reading_controller is None:
            self._reading_controller = ReadingModeController(
                self.bridge, self._entertainment_config_id
            )
            if self.reading_settings and self.reading_settings.light_ids:
                self._reading_controller.set_target_lights(
                    self.reading_settings.light_ids
                )
        return self._reading_controller

    def switch_to_video(self) -> bool:
        """Switch to video sync mode.

        Steps:
        1. Stop reading mode (leave lights as-is or dim)
        2. Deactivate any active entertainment streaming
        3. Activate entertainment configuration
        4. Start DTLS connection
        5. Start sync controller

        Returns:
            True if successfully switched to video mode
        """
        timed_print("ModeManager: Switching to VIDEO mode")

        # Step 1: Stop reading mode if active
        if self.current_mode == Mode.READING and self._reading_controller:
            timed_print("ModeManager: Stopping reading mode")
            # Don't turn off lights - leave them for smooth transition
            self._reading_controller.deactivate(turn_off=False)

        # Step 2: Stop sync if running (shouldn't happen but safety check)
        if self.sync_controller.is_running():
            timed_print("ModeManager: Stopping existing sync")
            self.sync_controller.stop()

        # Step 3: Ensure entertainment stream is ready
        if not self.entertainment_stream:
            timed_print("ModeManager: No entertainment stream configured")
            return False

        # Step 4: Activate entertainment streaming
        if not self.entertainment_stream.is_connected():
            if not self.bridge.activate_entertainment_streaming(
                self.entertainment_stream.entertainment_config_id
            ):
                timed_print("ModeManager: Failed to activate entertainment streaming")
                return False

            # Connect DTLS
            if not self.entertainment_stream.connect(self.bridge):
                timed_print("ModeManager: Failed to connect DTLS")
                self.bridge.deactivate_entertainment_streaming(
                    self.entertainment_stream.entertainment_config_id
                )
                return False

        # Step 5: Start video sync
        self.sync_controller.start()

        self.current_mode = Mode.VIDEO
        self._notify_mode_changed()

        # Resolve and cache the light ids this stream drives now, while the
        # network is guaranteed to be up. This removes the get_devices()
        # REST round-trip from the suspend path entirely (see Task 13) -
        # non-fatal on failure, turn_off() falls back to a live resolve.
        try:
            self._video_light_ids = self.bridge.resolve_light_ids(
                list(self.entertainment_stream.light_to_channel.keys())
            )
        except Exception as e:
            # Drop any cache from a previous session too - stale ids from a
            # different entertainment area are worse than an empty cache,
            # which at least falls back to a live resolve in turn_off().
            self._video_light_ids = []
            timed_print(f"ModeManager: Error resolving light ids: {e}")

        timed_print("ModeManager: Now in VIDEO mode")
        return True

    def switch_to_reading(
        self,
        xy: Optional[Tuple[float, float]] = None,
        brightness: Optional[int] = None,
        _callback: Optional[Callable[[bool], None]] = None,
    ) -> bool:
        """Switch to reading mode with static color.

        Steps:
        1. Stop video sync
        2. Stop DTLS/entertainment streaming
        3. Deactivate entertainment configuration
        4. Send REST PUT to set static color

        Args:
            xy: CIE XY color coordinates (uses settings default if None)
            brightness: Brightness 0-254 (uses settings default if None)
            _callback: Optional callback(result: bool) for async completion

        Returns:
            True if successfully switched to reading mode (immediately or scheduled)
        """
        # Prevent duplicate activation calls
        if self._reading_activation_pending:
            timed_print(
                "ModeManager: Reading mode activation already pending, ignoring duplicate call"
            )
            return True

        timed_print("ModeManager: Switching to READING mode")

        # Use settings defaults if not provided
        if xy is None and self.reading_settings:
            xy = self.reading_settings.color_xy
        if brightness is None and self.reading_settings:
            brightness = self.reading_settings.brightness

        # Step 1: Stop video sync if running
        if self.sync_controller.is_running():
            timed_print("ModeManager: Stopping video sync")
            self.sync_controller.stop()

        # Step 2: Stop entertainment streaming (disconnect already deactivates)
        if self.entertainment_stream and self.entertainment_stream.is_connected():
            timed_print("ModeManager: Stopping entertainment stream")
            self.entertainment_stream.disconnect(self.bridge)
            # Use non-blocking delay to let bridge process deactivation before REST commands
            if HAS_GLIB:
                self._reading_activation_pending = True
                GLib.timeout_add(
                    1000, self._finish_switch_to_reading, xy, brightness, _callback
                )
                return True
            else:
                # Fallback for non-GUI contexts
                time.sleep(0.3)
                return self._finish_switch_to_reading(xy, brightness, _callback)

        # No delay needed, proceed immediately
        return self._finish_switch_to_reading(xy, brightness, _callback)

    def _finish_switch_to_reading(
        self,
        xy: Optional[Tuple[float, float]],
        brightness: Optional[int],
        callback: Optional[Callable[[bool], None]],
    ) -> bool:
        """Complete the reading mode switch after delay.

        Returns:
            False to stop GLib timeout, or bool result for synchronous calls
        """
        # If the pending flag was already cleared (e.g. turn_off() cancelled
        # this activation after it was scheduled), abort before doing
        # anything else. This is the primary guard against a stale
        # GLib.timeout_add callback re-activating reading mode after
        # turn_off() has already run.
        if not self._reading_activation_pending:
            timed_print("ModeManager: Reading activation was cancelled, aborting")
            if callback:
                callback(False)
            return False

        # Clear pending flag
        self._reading_activation_pending = False

        # Check if we were interrupted (e.g., by turn_off)
        if self.current_mode != Mode.OFF:
            timed_print(
                "ModeManager: Reading activation cancelled, mode is no longer OFF"
            )
            if callback:
                callback(False)
            return False

        # Activate reading mode via REST
        reading = self.get_reading_controller()
        result = reading.activate(xy=xy, brightness=brightness)

        if result:
            self.current_mode = Mode.READING
            self._notify_mode_changed()
            timed_print("ModeManager: Now in READING mode")
        else:
            timed_print("ModeManager: Failed to activate reading mode")

        if callback:
            callback(result)

        if HAS_GLIB:
            return False
        return result

    def _send_lights_off(
        self, light_ids: List[str], timeout: Optional[float] = None
    ) -> None:
        """Send an explicit REST "off" command for each light id.

        Failures on individual lights are logged and otherwise ignored so
        one unreachable light doesn't stop the rest from being turned off.
        set_light_state() itself never raises on a REST failure (it catches
        BridgeError and returns False) - the bool return is checked here so
        those failures are no longer silent; the try/except stays too, in
        case of a non-BridgeError exception.
        """
        for light_id in light_ids:
            try:
                ok = self.bridge.client.set_light_state(
                    light_id, {"on": {"on": False}}, timeout=timeout
                )
                if not ok:
                    timed_print(
                        f"ModeManager: set_light_state returned False for light {light_id}"
                    )
            except Exception as e:
                timed_print(f"ModeManager: Failed to turn off light {light_id}: {e}")

    def turn_off(self, turn_off_lights: bool = True, urgent: bool = False) -> bool:
        """Turn off all lighting control.

        Args:
            turn_off_lights: If True, turn off the actual lights
            urgent: If True, skip the synchronous auto-activate-to-reading
                detour and get the light-off REST commands out within
                ~1s, instead of after the normal (~5s) stream teardown.
                Used on suspend, where NetworkManager tears the network
                down in parallel with our handler rather than after it.

        Returns:
            True if successfully turned off
        """
        timed_print("ModeManager: Turning OFF")

        # Capture the entertainment rids this stream is driving *before*
        # doing anything else. sync_controller.stop() below can
        # synchronously invoke switch_to_reading() (via the auto-activate
        # stop callback), which itself disconnects the entertainment
        # stream as a side effect - if we captured light_to_channel after
        # that point, it would already be gone and the light-off logic
        # below would silently never run.
        entertainment_rids = (
            list(self.entertainment_stream.light_to_channel.keys())
            if self.entertainment_stream
            else []
        )

        # Remember current mode before stopping sync
        mode_before = self.current_mode

        # Check if reading mode activation is pending (non-blocking transition)
        # If so, cancel it to prevent race conditions
        if self._reading_activation_pending:
            timed_print("ModeManager: Cancelling pending reading mode activation")
            self._reading_activation_pending = False

        if urgent:
            # Suppress the synchronous auto-activate-to-reading detour:
            # on_video_sync_stopped() would otherwise run switch_to_reading()
            # inline, which runs the FULL entertainment_stream.disconnect()
            # (openssl subprocess terminate + wait(timeout=2), plus a REST
            # deactivate) before turn_off() gets a chance to send anything -
            # ~5s we don't have before NetworkManager takes the network down.
            #
            # Fourth live test: even with the Task 13 reordering, the lamp
            # stayed ON - every REST failure in this chain is silent
            # (`except BridgeError: return False`, no log), and the ~5s
            # sync_controller.stop() (thread join + portal Close() D-Bus
            # call) plus the deactivate PUT's 5s default timeout meant the
            # off commands went out long after the network window closed.
            # New order: pause the DTLS sender and stream black frames
            # FIRST (immune to the network race - the lamp freezes at its
            # last streamed color, so black = visually off), defer the
            # expensive sync_controller.stop() teardown to the end, and
            # give every REST call a short timeout with a logged result.
            try:
                self._suppress_auto_activate = True

                # Step 1: stop new color frames going out over the DTLS
                # stream immediately. Unlike sync_controller.stop() (moved
                # to the heavy teardown below), pause_sending() doesn't
                # join the thread or close the capture pipeline - it takes
                # effect within one frame period (~33ms).
                timed_print("ModeManager: [urgent] pausing sync sender")
                self.sync_controller.pause_sending()
                time.sleep(0.1)  # let the in-flight frame drain

                # Step 2: stream black frames over the already-open DTLS
                # socket - the one channel immune to every REST/network
                # race below. 3x with a short gap for margin against a
                # dropped frame; lamp should be visually dark by ~T+0.4s.
                if self.entertainment_stream:
                    for i in range(3):
                        timed_print(f"ModeManager: [urgent] blackout {i + 1}/3")
                        self.entertainment_stream.blackout()
                        time.sleep(0.05)

                # Step 3: end the entertainment session immediately via a
                # direct REST call (short timeout) so the bridge honors
                # plain light commands again, without waiting for the full
                # disconnect() teardown below.
                if self.entertainment_stream:
                    deactivated = self.bridge.deactivate_entertainment_streaming(
                        self.entertainment_stream.entertainment_config_id,
                        timeout=2,
                    )
                    timed_print(
                        f"ModeManager: [urgent] deactivate_entertainment_streaming -> {deactivated}"
                    )

                # Step 4: off loop (cached ids) double-pass, 0.3s apart.
                if turn_off_lights and (self._video_light_ids or entertainment_rids):
                    light_ids = self._video_light_ids or self.bridge.resolve_light_ids(
                        entertainment_rids
                    )
                    if light_ids:
                        # Send the off loop twice, 0.3s apart: the bridge
                        # needs ~1s after deactivation before REST commands
                        # reliably take effect, but we can't afford a full
                        # 1s wait here - two passes 0.3s apart is the best
                        # effort within the network window.
                        timed_print(
                            f"ModeManager: [urgent] turning off {len(light_ids)} lights"
                        )
                        self._send_lights_off(light_ids, timeout=2)
                        time.sleep(0.3)
                        self._send_lights_off(light_ids, timeout=2)

                # Step 5: heavy teardown, exactly as before - thread join /
                # capture pipeline stop / portal Close(). The stop callback
                # is still suppressed.
                if self.sync_controller.is_running():
                    self.sync_controller.stop()
            finally:
                self._suppress_auto_activate = False

            # The off-loop above already handled the light-off REST
            # commands, so the shared end-of-teardown off-loop further
            # below must not run again.
            if self.entertainment_stream and self.entertainment_stream.is_connected():
                self.entertainment_stream.disconnect(self.bridge)

            if self._reading_controller and self._reading_controller.is_active():
                self._reading_controller.deactivate(turn_off=turn_off_lights)

            self.current_mode = Mode.OFF
            self._notify_mode_changed()
            timed_print("ModeManager: Now OFF")
            return True

        # Stop video sync (this may trigger auto-activation callback)
        if self.sync_controller.is_running():
            self.sync_controller.stop()

        # sync_controller.stop() above can synchronously invoke
        # on_video_sync_stopped() -> switch_to_reading(), which (when the
        # entertainment stream needs time to disconnect) arms a new
        # GLib.timeout_add to finish the switch ~1s later, setting
        # _reading_activation_pending = True again. If that happened, cancel
        # it here too, otherwise the timer can fire after turn_off() returns
        # and re-activate reading-mode lighting we're in the middle of
        # turning off. _finish_switch_to_reading() checks this flag as its
        # abort condition, so clearing it here makes any in-flight callback
        # a no-op.
        if self._reading_activation_pending:
            timed_print(
                "ModeManager: Cancelling reading mode activation armed during sync stop"
            )
            self._reading_activation_pending = False

        # Check if auto-activation already switched us to reading mode
        # In that case, don't turn off - let reading mode stay active
        if mode_before == Mode.VIDEO and self.current_mode == Mode.READING:
            timed_print(
                "ModeManager: Auto-activated reading mode, staying in READING mode"
            )
            return True

        # Stop entertainment stream if still connected. It may already have
        # been disconnected as a side effect of switch_to_reading() above
        # (Bug A) - the light-off logic below must run whether or not this
        # call is the one that performs the disconnect, so the connected
        # check only gates the disconnect() call itself.
        if self.entertainment_stream and self.entertainment_stream.is_connected():
            self.entertainment_stream.disconnect(self.bridge)

        # disconnect() only tears down the DTLS/streaming connection, it
        # never sends an explicit "off" REST command, so the lights would
        # otherwise freeze at their last streamed color instead of turning
        # off. entertainment_rids (captured at the top of this method,
        # before anything could disconnect the stream) are entertainment
        # *service* resource IDs, not light resource IDs - resolve() them
        # via the bridge's device/service map (Bug B) before calling
        # set_light_state().
        if turn_off_lights and entertainment_rids:
            light_ids = self.bridge.resolve_light_ids(entertainment_rids)
            if light_ids:
                timed_print(f"ModeManager: Turning off {len(light_ids)} lights")
                self._send_lights_off(light_ids)

        # Stop reading mode
        if self._reading_controller and self._reading_controller.is_active():
            self._reading_controller.deactivate(turn_off=turn_off_lights)

        self.current_mode = Mode.OFF
        self._notify_mode_changed()
        timed_print("ModeManager: Now OFF")
        return True

    def is_video_active(self) -> bool:
        """Check if video mode is currently active."""
        return self.current_mode == Mode.VIDEO and self.sync_controller.is_running()

    def is_reading_active(self) -> bool:
        """Check if reading mode is currently active."""
        return self.current_mode == Mode.READING and (
            self._reading_controller is not None
            and self._reading_controller.is_active()
        )

    def get_current_mode(self) -> Mode:
        """Get current mode."""
        return self.current_mode

    def on_video_sync_stopped(self) -> bool:
        """Called when video sync stops (e.g., user clicked Stop).

        If auto_activate is enabled, automatically switch to reading mode.

        Returns:
            True if auto-switched to reading mode
        """
        if self._suppress_auto_activate:
            return False

        if self.current_mode != Mode.VIDEO:
            return False

        self.current_mode = Mode.OFF  # Temporary state

        # Check if we should auto-activate reading mode
        if self.reading_settings and self.reading_settings.auto_activate:
            timed_print("ModeManager: Video stopped, auto-activating reading mode")
            return self.switch_to_reading()

        self._notify_mode_changed()
        return False
