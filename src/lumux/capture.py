"""Screen capture using PipeWire portal with optimized GStreamer pipeline."""

import os
import re
import time
import threading
from contextlib import contextmanager
from typing import Callable, Optional, List, Set, TYPE_CHECKING

import numpy as np

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstApp, GstVideo

from lumux.black_bar_detector import BlackBarDetector, CropRegion

if TYPE_CHECKING:
    from lumux.config.settings_manager import BlackBarSettings

Gst.init(None)

# Broken GL readback (e.g. Mesa/RADV) makes these categories log one error per
# frame until the capture falls back to the next converter. Leave the output
# alone when the user asked for specific debug output.
if not os.environ.get("GST_DEBUG"):
    for _category in ("glbasetexture", "videometa"):
        Gst.debug_set_threshold_for_name(_category, Gst.DebugLevel.NONE)

_windowless_gl_checked = False


def _prefer_windowless_gl() -> bool:
    """Make GstGL use a headless EGL device display when it works.

    On Wayland GstGL's default backend creates an xdg_toplevel titled
    "OpenGL Renderer" for its context, which shows up in docks and task
    managers for as long as the GL pipeline runs. The egl-device backend
    needs no window at all. GST_GL_WINDOW is read whenever a GL display is
    created, so it can be set after Gst.init. Only keep it if a context can
    actually be created; otherwise the GL pipelines would fail and silently
    fall back to CPU conversion. An explicit user setting is left alone.
    """
    if "GST_GL_WINDOW" in os.environ:
        return False
    try:
        gi.require_version("GstGL", "1.0")
        from gi.repository import GstGL
    except (ValueError, ImportError):
        return False

    os.environ["GST_GL_WINDOW"] = "egl-device"
    try:
        context = GstGL.GLContext.new(GstGL.GLDisplay.new())
        if context is not None and context.create(None):
            context.destroy()
            return True
    except GLib.Error:
        pass
    del os.environ["GST_GL_WINDOW"]
    return False


def _ensure_windowless_gl_checked() -> None:
    global _windowless_gl_checked
    if _windowless_gl_checked:
        return
    _windowless_gl_checked = True
    if _prefer_windowless_gl():
        print("Using headless EGL device for GStreamer GL (no renderer window)")

# xdg-desktop-portal ScreenCast persist_mode values (portal interface v4+).
# PERSIST_UNTIL_REVOKED keeps the grant across app restarts and reboots
# until the user revokes it (e.g. GNOME Settings > Privacy > Screen
# Sharing), so the monitor picker only needs to be shown once.
_PERSIST_UNTIL_REVOKED = 2


class ScreenCapture:
    # Unmappable buffers within _MAP_FAILURE_WINDOW seconds after which the
    # active pipeline config is considered broken and the next converter is
    # tried. Failures need not be consecutive: broken GL readback often lets
    # the occasional (blank) frame through between the dropped ones.
    _MAP_FAILURE_LIMIT = 5
    _MAP_FAILURE_WINDOW = 2.0
    # CreateSession/Start are machine-only D-Bus round trips; SelectSources
    # shows the share picker to the user and must survive however long it
    # takes them to notice it (e.g. right after waking the monitor from a
    # blank), so it gets a much longer allowance.
    _CREATE_SESSION_TIMEOUT_S = 30
    _SELECT_SOURCES_TIMEOUT_S = 120
    _START_TIMEOUT_S = 15
    # Poll interval while waiting for a blanked screen to come back. Short, so
    # sync resumes promptly on wake; the probe is a cheap local D-Bus call.
    _BLANKED_RETRY_S = 2.0
    # How long the monitor list must stay unchanged before a handshake is
    # attempted. Some monitors (e.g. OLEDs on HDMI/DP) drop off the link and
    # re-attach when they wake, so the output vanishes for a moment exactly
    # when the compositor closes the screencast session.
    _OUTPUT_SETTLE_S = 3.0

    def __init__(
        self,
        scale_factor: float = 0.125,
        black_bar_settings: Optional["BlackBarSettings"] = None,
        source_type: str = "screen",
        restore_token: Optional[str] = None,
        on_restore_token: Optional[Callable[[str], None]] = None,
    ):
        self.scale_factor = scale_factor
        self.source_type = source_type
        self._display = None

        self._restore_token = restore_token or None
        self._on_restore_token = on_restore_token
        self._token_rejected = False
        self._token_consumed = False
        self._token_counter = 0
        self._token_lock = threading.Lock()

        self._portal_node_id: Optional[int] = None
        self._portal_session_handle: Optional[str] = None
        self._portal_bus = None
        self._session_closed_sub: Optional[int] = None
        self._next_portal_retry = 0.0
        self._blanked_logged = False
        # Written on the GTK main thread, read from the sync thread; plain
        # attribute stores, so no lock is needed.
        self._monitor_count: Optional[int] = None
        self._monitors_changed_at: Optional[float] = None

        self._pipeline: Optional[Gst.Pipeline] = None
        self._appsink: Optional[GstApp.AppSink] = None
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_frame_data: Optional[bytes] = None
        self._frame_lock = threading.Lock()
        self._pipeline_running = False
        self._pipeline_error_logged = False
        self._runtime_failed_configs: Set[str] = set()
        # Fed from the GStreamer streaming thread (appsink) and the bus watch.
        self._map_failure_times: List[float] = []
        self._map_failure_lock = threading.Lock()
        self._active_config_key: Optional[str] = None

        self._source_width: Optional[int] = None
        self._source_height: Optional[int] = None
        self._pipeline_scaled = False
        self._needs_pipeline_restart = False

        self._black_bar_detector: Optional[BlackBarDetector] = None
        if black_bar_settings is not None:
            self._init_black_bar_detector(black_bar_settings)

        self._init_display()

    def _compute_scaled_dimensions(self):
        if self._source_width and self._source_height and self.scale_factor < 1.0:
            # Round down to a multiple of 4 so RGB row strides stay 4-byte
            # aligned (avoids padded strides) and odd-width caps that some
            # converters refuse to link.
            w = max(4, int(self._source_width * self.scale_factor) & ~3)
            h = max(4, int(self._source_height * self.scale_factor) & ~3)
            return w, h
        return None, None

    def update_scale_factor(self, new_scale: float) -> None:
        new_scale = max(0.01, min(1.0, new_scale))
        changed = abs(new_scale - self.scale_factor) > 0.001
        self.scale_factor = new_scale
        if changed and self._pipeline_running:
            target_w, target_h = self._compute_scaled_dimensions()
            scaled_changed = target_w != getattr(
                self, "_scaled_w", None
            ) or target_h != getattr(self, "_scaled_h", None)
            if scaled_changed:
                self._restart_pipeline()

    def _stop_gst_pipeline(self) -> None:
        if self._pipeline:
            bus = self._pipeline.get_bus()
            if bus:
                bus.remove_signal_watch()
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
            self._appsink = None
        self._active_config_key = None
        self._pipeline_running = False
        self._latest_frame = None
        self._latest_frame_data = None

    def _restart_pipeline(self) -> None:
        self._needs_pipeline_restart = False
        self._stop_gst_pipeline()
        if self._portal_node_id:
            self._start_pipeline()

    def _init_black_bar_detector(self, settings: "BlackBarSettings") -> None:
        self._black_bar_detector = BlackBarDetector(
            enabled=settings.enabled,
            threshold=settings.threshold,
            detection_rate=settings.detection_rate,
            smooth_factor=settings.smooth_factor,
        )

    def update_black_bar_settings(self, settings: "BlackBarSettings") -> None:
        if self._black_bar_detector is None:
            self._init_black_bar_detector(settings)
        else:
            self._black_bar_detector.set_enabled(settings.enabled)
            self._black_bar_detector.set_threshold(settings.threshold)
            self._black_bar_detector.set_detection_rate(settings.detection_rate)
            self._black_bar_detector.smooth_factor = settings.smooth_factor

    def get_black_bar_crop_region(self):
        if self._black_bar_detector is None:
            return None
        return self._black_bar_detector.get_crop_region()

    def _init_display(self):
        try:
            from gi.repository import Gdk

            self._display = Gdk.Display.get_default()
        except Exception as e:
            print(f"Error initializing display: {e}")
            return
        self._watch_monitors()

    def _watch_monitors(self):
        """Track how many monitors the compositor currently exposes.

        Must run on the GTK main thread; the items-changed handler does too.
        """
        if self._display is None:
            return
        try:
            monitors = self._display.get_monitors()
            self._monitor_count = monitors.get_n_items()
            monitors.connect("items-changed", self._on_monitors_changed)
        except Exception as e:
            print(f"Error watching monitors: {e}")
            self._monitor_count = None

    def _on_monitors_changed(self, monitors, position, removed, added):
        self._monitor_count = monitors.get_n_items()
        self._monitors_changed_at = time.monotonic()
        print(f"Displays changed: {self._monitor_count} connected")

    @property
    def restore_token(self) -> str:
        """Current portal restore token (for diagnostics)."""
        return self._restore_token

    def capture(self) -> Optional[np.ndarray]:
        if self._needs_pipeline_restart and self._pipeline_running:
            self._restart_pipeline()
            if not self._pipeline_running:
                return None

        if self._pipeline_running:
            with self._frame_lock:
                frame = self._latest_frame
            if frame is not None:
                return self._process_image(frame)

        if not self._portal_node_id:
            # Rate-limit portal handshakes: without this, a session the
            # compositor refuses to restore (e.g. while the screen is
            # locked) would be re-requested once per frame.
            if time.monotonic() < self._next_portal_retry:
                return None
            if self._screen_is_blanked():
                # There is nothing to capture anyway; waiting preserves the
                # restore token so the screen coming back restores silently.
                if not self._blanked_logged:
                    print(
                        "Screen is blanked; deferring portal handshake until "
                        "it returns"
                    )
                    self._blanked_logged = True
                self._next_portal_retry = (
                    time.monotonic() + self._BLANKED_RETRY_S
                )
                return None
            self._blanked_logged = False
            if not self._setup_portal_session():
                self._next_portal_retry = time.monotonic() + 5.0
                return None
            self._next_portal_retry = 0.0

        if not self._pipeline_running:
            if not self._start_pipeline():
                return None

        timeout = 2.0
        start = time.time()
        while (time.time() - start) < timeout:
            with self._frame_lock:
                frame = self._latest_frame
            if frame is not None:
                return self._process_image(frame)
            time.sleep(0.01)

        return None

    def _process_image(self, screen: np.ndarray) -> np.ndarray:
        if screen is None or screen.size == 0:
            return screen

        if self._black_bar_detector is not None:
            try:
                crop_region = self._black_bar_detector.process(screen)
                if crop_region is not None and crop_region.is_valid(
                    screen.shape[1], screen.shape[0]
                ):
                    screen = screen[
                        crop_region.top : crop_region.bottom,
                        crop_region.left : crop_region.right,
                        :,
                    ]
            except Exception as e:
                print(f"Black bar detection error: {e}")

        if screen is None or screen.size == 0:
            return screen

        if not self._pipeline_scaled and self.scale_factor < 1.0:
            import PIL.Image as Image

            new_h = max(1, int(screen.shape[0] * self.scale_factor))
            new_w = max(1, int(screen.shape[1] * self.scale_factor))
            if not screen.flags["C_CONTIGUOUS"]:
                screen = np.ascontiguousarray(screen)
            screen = np.array(
                Image.fromarray(screen).resize(
                    (new_w, new_h), Image.Resampling.BILINEAR
                )
            )
        return screen

    @staticmethod
    @contextmanager
    def _run_loop_with_timeout(loop, timeout_s):
        """Run `loop` until it quits or `timeout_s` elapses.

        Yields a callable reporting whether the watchdog fired. The timeout
        source is ALWAYS removed: a surviving source would fire later, during
        a subsequent attempt's nested main loop, and quit it prematurely -
        which collapses the retry backoff into a dialog storm.
        """
        timed_out = False

        def _on_timeout():
            nonlocal timed_out
            timed_out = True
            loop.quit()
            return False

        source_id = GLib.timeout_add_seconds(timeout_s, _on_timeout)
        try:
            loop.run()
            yield lambda: timed_out
        finally:
            if not timed_out:
                try:
                    GLib.source_remove(source_id)
                except Exception:
                    pass

    def _setup_portal_session(self) -> bool:
        self._token_rejected = False
        if self._try_setup_portal_session():
            return True
        if self._token_rejected:
            # GNOME's portal rejects malformed restore tokens with a hard
            # InvalidArgument error from SelectSources itself, rather than
            # gracefully ignoring them per spec. Clear the bad token and
            # try once more without it, so a corrupted token can't wedge
            # sync forever. Failures the token can't have caused
            # (CreateSession, user cancel, Start) must NOT clear it.
            print("Portal rejected stored restore token; clearing and retrying")
            self._restore_token = None
            if self._on_restore_token:
                self._on_restore_token("")
            return self._try_setup_portal_session()
        return False

    def _next_token(self, prefix: str) -> str:
        """Return a token unique to this process, for portal object paths.

        Both `handle_token` (Request path) and `session_handle_token` (Session
        path) must be unique per request: the portal exports an object at a
        path derived from them, and a collision fails with "An object is
        already exported for the interface ... Request at ...". A wall-clock
        seconds value is too coarse - back-to-back handshakes land in the same
        second - and omitting handle_token entirely makes the portal reuse one
        constant path for every request.
        """
        with self._token_lock:
            self._token_counter += 1
            counter = self._token_counter
        return f"{prefix}{os.getpid()}_{counter}"

    def _screen_is_blanked(self) -> bool:
        """True when the compositor has the screen blanked, locked or has no
        stable output to capture.

        A portal handshake cannot be served in that state: GNOME will not
        restore a screencast behind the shield, and KDE finds no output to
        restore once the monitor is off and deletes the consent outright.
        Either way the doomed attempt costs us our restore token - which is
        what forces a share picker once the screen comes back. GNOME exposes
        its state on org.gnome.ScreenSaver, KDE only on the freedesktop name.
        Any probe failure counts as not blanked so desktops without either
        service behave exactly as before.

        The lock check alone misses a monitor that wakes (or sleeps) without
        a lock: if it drops off the link, KDE has no output for a moment and
        the handshake fails the same way. So no monitors, or a monitor list
        that changed within the last few seconds, counts as blanked too.
        """
        if self._monitor_count == 0:
            return True
        changed_at = self._monitors_changed_at
        if (
            changed_at is not None
            and time.monotonic() - changed_at < self._OUTPUT_SETTLE_S
        ):
            return True
        try:
            import pydbus

            bus = pydbus.SessionBus()
        except Exception:
            return False
        for name, path in (
            ("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver"),
            ("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver"),
        ):
            try:
                if bus.get(name, path).GetActive():
                    return True
            except Exception:
                continue
        return False

    def _persist_new_token(self, new_token: Optional[str]) -> None:
        """Store a token the portal just issued, notifying the owner."""
        if not new_token or new_token == self._restore_token:
            return
        print(
            f"[capture] Screen-share consent cached "
            f"(token length {len(new_token)}) — will skip prompt on next sync"
        )
        self._restore_token = new_token
        self._token_consumed = False
        if self._on_restore_token:
            try:
                self._on_restore_token(new_token)
            except Exception as e:
                print(f"Error persisting restore token: {e}")

    def _try_setup_portal_session(self) -> bool:
        self._token_consumed = False
        if self._do_setup_portal_session():
            return True
        # A session that was created but never Started still owns its
        # share-picker dialog. Dropping the handle without closing it leaves
        # that dialog on screen, so every retry stacks another one.
        if self._portal_session_handle:
            self._close_portal_session()
        # Deliberately KEEP the restore token here. A handshake can fail for
        # reasons that say nothing about the token's validity (most commonly:
        # attempted while the compositor had the screen blanked), and whether
        # such an attempt consumed it is not observable. Discarding a
        # possibly-valid token guarantees a share picker on the next attempt;
        # keeping a dead one costs nothing, because the portal rejects it
        # explicitly and the _token_rejected path clears it then.
        return False

    def _do_setup_portal_session(self) -> bool:
        try:
            import pydbus

            kind = "window" if self.source_type == "window" else "screen"
            print(f"Requesting {kind} capture permission via portal...")
            bus = pydbus.SessionBus()
            self._portal_bus = bus
            portal = bus.get(
                "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop"
            )
            screencast = portal["org.freedesktop.portal.ScreenCast"]

            loop = GLib.MainLoop()
            state = {
                "session_handle": None,
                "node_id": None,
                "error": None,
                "restore_token": None,
            }

            def on_response(connection, sender, object, interface, signal, params):
                code, results = params
                if code != 0:
                    state["error"] = code
                    loop.quit()
                    return

                if "session_handle" in results:
                    state["session_handle"] = results["session_handle"]
                    loop.quit()
                elif "streams" in results:
                    # An empty stream list is possible (and used to raise
                    # IndexError here, killing the handshake mid-flight).
                    streams = results["streams"]
                    state["node_id"] = streams[0][0] if streams else None
                    # The Start response may carry a restore_token when
                    # persistent consent was granted; persist it for next time.
                    state["restore_token"] = results.get("restore_token")
                    loop.quit()
                else:
                    loop.quit()

            create_options = {
                "session_handle_token": GLib.Variant("s", self._next_token("s")),
                "handle_token": GLib.Variant("s", self._next_token("r")),
                # persist_mode = 2: persist consent until explicitly revoked,
                # so the portal can issue a restore_token for future sessions.
                "persist_mode": GLib.Variant("u", 2),
            }
            # Only send a restore_token when we actually have one; sending an
            # empty string can confuse some portal backends.
            if self._restore_token:
                create_options["restore_token"] = GLib.Variant(
                    "s", self._restore_token
                )
                print(
                    "[capture] Restoring cached screen-share consent "
                    f"(token length {len(self._restore_token)}) — "
                    "dialog should be skipped"
                )
            else:
                print(
                    "[capture] Requesting fresh screen-share consent "
                    "(persist_mode=2) — dialog will be shown once"
                )
            req = screencast.CreateSession(create_options)
            sub = bus.con.signal_subscribe(
                None,
                "org.freedesktop.portal.Request",
                "Response",
                req,
                None,
                0,
                on_response,
            )
            try:
                with self._run_loop_with_timeout(
                    loop, self._CREATE_SESSION_TIMEOUT_S
                ):
                    pass
            finally:
                bus.con.signal_unsubscribe(sub)

            if not state["session_handle"]:
                return False
            self._portal_session_handle = state["session_handle"]

            portal_types = 1 if self.source_type == "screen" else 2
            select_sources_options = {
                "types": GLib.Variant("u", portal_types),
                "multiple": GLib.Variant("b", False),
                "handle_token": GLib.Variant("s", self._next_token("r")),
                "persist_mode": GLib.Variant("u", _PERSIST_UNTIL_REVOKED),
            }
            if self._restore_token:
                select_sources_options["restore_token"] = GLib.Variant(
                    "s", self._restore_token
                )
                # Restore tokens are single-use: the portal invalidates this
                # one the moment SelectSources receives it and returns a
                # replacement in the Start response. Our stored copy is dead
                # from here on, whatever happens next.
                self._token_consumed = True

            loop = GLib.MainLoop()
            try:
                req = screencast.SelectSources(
                    self._portal_session_handle,
                    select_sources_options,
                )
            except Exception:
                if "restore_token" in select_sources_options:
                    # Refused outright, not accepted-and-invalidated: the
                    # _token_rejected path clears and retries without it, so
                    # this must not also count as consumed (double clear).
                    self._token_rejected = True
                    self._token_consumed = False
                raise
            sub = bus.con.signal_subscribe(
                None,
                "org.freedesktop.portal.Request",
                "Response",
                req,
                None,
                0,
                on_response,
            )
            timed_out = False
            try:
                with self._run_loop_with_timeout(
                    loop, self._SELECT_SOURCES_TIMEOUT_S
                ) as select_sources_timed_out:
                    timed_out = select_sources_timed_out()
            finally:
                bus.con.signal_unsubscribe(sub)

            if timed_out:
                print(
                    "Screen-share selection timed out waiting for a user "
                    "response; aborting this portal attempt"
                )
                return False
            if state["error"] is not None:
                print(f"Screen-share selection was rejected (code {state['error']})")
                return False

            loop = GLib.MainLoop()
            req = screencast.Start(
                self._portal_session_handle,
                "",
                {"handle_token": GLib.Variant("s", self._next_token("r"))},
            )
            sub = bus.con.signal_subscribe(
                None,
                "org.freedesktop.portal.Request",
                "Response",
                req,
                None,
                0,
                on_response,
            )
            try:
                with self._run_loop_with_timeout(loop, self._START_TIMEOUT_S):
                    pass
            finally:
                bus.con.signal_unsubscribe(sub)

            # Persist any rotated token BEFORE deciding success: the Start
            # response's token is the only valid one from now on, so losing
            # it because no node arrived would strand us on a dead token.
            self._persist_new_token(state["restore_token"])

            if state["node_id"]:
                self._portal_node_id = state["node_id"]
                print(f"Portal session started. PipeWire node: {self._portal_node_id}")

                # The compositor can kill the session while we're running
                # (screen lock, monitor mode change, permission revoked).
                # Without this subscription the pipeline keeps pushing into
                # a dead PipeWire node and capture never recovers.
                self._session_closed_sub = bus.con.signal_subscribe(
                    None,
                    "org.freedesktop.portal.Session",
                    "Closed",
                    self._portal_session_handle,
                    None,
                    0,
                    self._on_session_closed_signal,
                )

                return True

        except Exception as e:
            print(f"Failed to setup portal session: {e}")

        return False

    def _get_pipeline_configs(self, node_id: int) -> List[str]:
        configs = []

        target_w, target_h = self._compute_scaled_dimensions()

        has_glupload = Gst.ElementFactory.find("glupload") is not None
        has_glcolorconvert = Gst.ElementFactory.find("glcolorconvert") is not None
        has_gldownload = Gst.ElementFactory.find("gldownload") is not None
        has_videoscale = Gst.ElementFactory.find("videoscale") is not None
        has_v4l2convert = Gst.ElementFactory.find("v4l2convert") is not None
        has_videoconvert = Gst.ElementFactory.find("videoconvert") is not None

        sink_props = "name=sink emit-signals=true drop=true max-buffers=1 sync=false"
        src = f"pipewiresrc path={node_id} do-timestamp=true"

        if target_w and target_h:
            scaled_caps = f"video/x-raw,format=RGB,width={target_w},height={target_h}"

            # No glcolorscale variants: it only outputs RGBA, which videoscale
            # cannot convert to RGB, so they failed to link on every machine.
            if has_glupload and has_glcolorconvert and has_gldownload:
                if has_videoscale:
                    configs.append(
                        f"{src} ! "
                        f"glupload ! glcolorconvert ! gldownload ! videoscale ! "
                        f"{scaled_caps} ! appsink {sink_props}"
                    )

            if has_v4l2convert and has_videoscale:
                configs.append(
                    f"{src} ! v4l2convert ! videoscale ! {scaled_caps} ! "
                    f"appsink {sink_props}"
                )

            if has_videoconvert and has_videoscale:
                configs.append(
                    f"{src} ! videoconvert ! videoscale ! {scaled_caps} ! "
                    f"appsink {sink_props}"
                )

        if has_glupload and has_glcolorconvert and has_gldownload:
            configs.append(
                f"{src} ! "
                f"glupload ! glcolorconvert ! gldownload ! "
                f"video/x-raw,format=RGB ! "
                f"appsink {sink_props}"
            )

        if has_v4l2convert:
            configs.append(
                f"{src} ! v4l2convert ! video/x-raw,format=RGB ! appsink {sink_props}"
            )

        if has_videoconvert:
            configs.append(
                f"{src} ! videoconvert ! video/x-raw,format=RGB ! appsink {sink_props}"
            )

        return configs

    @staticmethod
    def _config_key(pipeline_str: str) -> str:
        """Identify a pipeline config independent of node id and dimensions,
        so runtime failures can be remembered across restarts."""
        key = re.sub(r"path=\d+", "path=*", pipeline_str)
        key = re.sub(r"width=\d+", "width=*", key)
        key = re.sub(r"height=\d+", "height=*", key)
        return key

    def _start_pipeline(self) -> bool:
        if not self._portal_node_id:
            return False

        if self._pipeline:
            self._stop_gst_pipeline()

        _ensure_windowless_gl_checked()
        configs = self._get_pipeline_configs(self._portal_node_id)
        if not configs:
            print("No suitable GStreamer pipeline configuration found")
            print("Need one of: glupload+glcolorconvert, v4l2convert, or videoconvert")
            self._log_pipeline_details()
            return False

        target_w, target_h = self._compute_scaled_dimensions()

        for i, pipeline_str in enumerate(configs):
            config_key = self._config_key(pipeline_str)
            if config_key in self._runtime_failed_configs:
                continue
            try:
                self._pipeline = Gst.parse_launch(pipeline_str)
                self._appsink = self._pipeline.get_by_name("sink")
                self._appsink.connect("new-sample", self._on_new_sample)

                bus = self._pipeline.get_bus()
                bus.add_signal_watch()
                bus.connect("message::error", self._on_pipeline_error)
                bus.connect("message::warning", self._on_pipeline_warning)
                bus.connect("message::element", self._on_pipeline_element_message)

                ret = self._pipeline.set_state(Gst.State.PLAYING)
                if ret == Gst.StateChangeReturn.FAILURE:
                    desc = (
                        pipeline_str.split(" ! ")[1]
                        if " ! " in pipeline_str
                        else "unknown"
                    )
                    print(f"Pipeline config {i + 1} failed (converter: {desc})")
                    self._pipeline.set_state(Gst.State.NULL)
                    self._pipeline = None
                    continue

                uses_scaled_caps = (
                    target_w is not None and f"width={target_w}" in pipeline_str
                )
                self._pipeline_scaled = uses_scaled_caps
                self._scaled_w = target_w
                self._scaled_h = target_h

                self._pipeline_running = True
                self._pipeline_error_logged = False
                with self._map_failure_lock:
                    self._map_failure_times.clear()
                self._active_config_key = config_key
                desc = (
                    pipeline_str.split(" ! ")[1] if " ! " in pipeline_str else "unknown"
                )
                scale_info = (
                    f", scaled to {target_w}x{target_h}" if uses_scaled_caps else ""
                )
                print(
                    f"GStreamer capture pipeline started (converter: {desc}{scale_info})"
                )
                return True

            except Exception as e:
                print(f"Pipeline config {i + 1} exception: {e}")
                if self._pipeline:
                    self._pipeline.set_state(Gst.State.NULL)
                    self._pipeline = None
                continue

        self._pipeline_scaled = False
        print("All pipeline configurations failed")
        self._log_pipeline_details()
        return False

    def _on_pipeline_error(self, bus, message):
        err, debug = message.parse_error()
        if not self._pipeline_error_logged:
            print(f"GStreamer pipeline error: {err.message}")
            print(f"GStreamer debug info: {debug}")
            self._pipeline_error_logged = True
        if self._active_config_key:
            self._runtime_failed_configs.add(self._active_config_key)
            print(
                "Marking pipeline config as failed; "
                "will fall back to next converter on restart"
            )
        self._stop_gst_pipeline()

    def _on_unmappable_buffer(self, detail: str) -> None:
        """Called for every frame that cannot be mapped to system memory.

        Some GL/driver combinations (e.g. Mesa/RADV, where gldownload can only
        read back RGBA textures) negotiate fine but never deliver CPU-readable
        frames, so the pipeline never errors out and every frame is dropped.
        After a few failures in a short window, mark the active config as
        failed and restart with the next converter.
        """
        with self._map_failure_lock:
            now = time.monotonic()
            self._map_failure_times.append(now)
            self._map_failure_times = [
                t for t in self._map_failure_times if now - t <= self._MAP_FAILURE_WINDOW
            ]
            if len(self._map_failure_times) < self._MAP_FAILURE_LIMIT:
                return
            if self._needs_pipeline_restart:
                return
            count = len(self._map_failure_times)
            self._map_failure_times.clear()

        key = self._active_config_key
        print(
            f"Failed to map {count} buffers in {self._MAP_FAILURE_WINDOW:g}s "
            f"({detail}); trying next pipeline config"
        )
        if key:
            self._runtime_failed_configs.add(key)
            # All GL variants read back through the same gldownload path, so
            # they share the failure; skip them rather than retrying each one.
            if "glupload" in key and self._portal_node_id:
                for config in self._get_pipeline_configs(self._portal_node_id):
                    if "glupload" in config:
                        self._runtime_failed_configs.add(self._config_key(config))
        self._needs_pipeline_restart = True

    def _on_pipeline_warning(self, bus, message):
        warn, debug = message.parse_warning()
        if debug and "invalid video buffer received" in debug:
            # An element downstream of gldownload (e.g. videoscale) could not
            # map the frame and dropped it, so it never reaches the appsink
            # map() check. Same root cause, same fallback.
            self._on_unmappable_buffer("dropped by a downstream element")
            return
        print(f"GStreamer pipeline warning: {warn.message} ({debug})")

    def _on_pipeline_element_message(self, bus, message):
        structure = message.get_structure()
        if structure and structure.get_name() == "missing-plugin":
            print(f"Missing GStreamer plugin: {structure.to_string()}")

    def _log_pipeline_details(self):
        if not self._pipeline:
            return
        try:
            state = self._pipeline.get_state(Gst.CLOCK_TIME_NONE)
            print(f"Pipeline state: {state}")
            print(f"PipeWire node ID: {self._portal_node_id}")
            plugins = {
                "pipewiresrc": Gst.ElementFactory.find("pipewiresrc") is not None,
                "videoconvert": Gst.ElementFactory.find("videoconvert") is not None,
                "videoscale": Gst.ElementFactory.find("videoscale") is not None,
                "v4l2convert": Gst.ElementFactory.find("v4l2convert") is not None,
                "glupload": Gst.ElementFactory.find("glupload") is not None,
                "glcolorconvert": Gst.ElementFactory.find("glcolorconvert") is not None,
                "gldownload": Gst.ElementFactory.find("gldownload") is not None,
                "glcolorscale": Gst.ElementFactory.find("glcolorscale") is not None,
            }
            print(f"GStreamer plugins: {plugins}")
        except Exception as e:
            print(f"Error logging pipeline details: {e}")

    @staticmethod
    def _extract_pixel_rows(
        data: bytes, width: int, height: int, bpp: int, stride: Optional[int]
    ) -> np.ndarray:
        """Return a (height, width*bpp) uint8 array, stripping row padding.

        GStreamer may pad each row to an alignment boundary (e.g. 4 bytes),
        so the buffer stride can exceed width*bpp. The real stride comes from
        GstVideoMeta when available, otherwise it is inferred from the buffer
        size.
        """
        row_bytes = width * bpp
        if not stride or stride < row_bytes:
            if len(data) == height * row_bytes:
                stride = row_bytes
            else:
                stride = len(data) // height if height else row_bytes
        if stride < row_bytes or len(data) < stride * (height - 1) + row_bytes:
            raise ValueError(
                f"Buffer too small: {len(data)} bytes for "
                f"{width}x{height} with bpp={bpp} (stride={stride})"
            )

        arr = np.frombuffer(data, dtype=np.uint8)
        if stride == row_bytes:
            return arr[: height * row_bytes].reshape(height, row_bytes)

        # Padded rows: build a strided view (handles a possibly unpadded
        # final row) and copy to detach from the mapped buffer.
        rows = np.lib.stride_tricks.as_strided(
            arr, shape=(height, row_bytes), strides=(stride, 1)
        )
        return rows.copy()

    def _on_new_sample(self, appsink) -> Gst.FlowReturn:
        try:
            sample = appsink.emit("pull-sample")
            if not sample:
                return Gst.FlowReturn.OK

            buffer = sample.get_buffer()
            caps = sample.get_caps()

            struct = caps.get_structure(0)
            width = struct.get_value("width")
            height = struct.get_value("height")
            fmt = struct.get_value("format") if struct.has_field("format") else None

            if not self._pipeline_scaled and (
                self._source_width != width or self._source_height != height
            ):
                self._source_width = width
                self._source_height = height
                if self.scale_factor < 1.0:
                    self._needs_pipeline_restart = True

            stride = None
            try:
                meta = GstVideo.buffer_get_video_meta(buffer)
                if meta and meta.n_planes >= 1:
                    stride = meta.stride[0]
            except Exception:
                stride = None

            success, map_info = buffer.map(Gst.MapFlags.READ)
            if not success:
                self._on_unmappable_buffer(f"format={fmt}, {width}x{height}")
                return Gst.FlowReturn.OK

            try:
                data = bytes(map_info.data)

                if fmt in ("RGBA", "RGBx", "BGRA", "BGRx"):
                    bpp = 4
                elif fmt in ("BGR15", "RGB15"):
                    bpp = 2
                else:
                    bpp = 3

                rows = self._extract_pixel_rows(data, width, height, bpp, stride)

                if fmt == "BGR":
                    frame = rows.reshape(height, width, 3)[:, :, ::-1].copy()
                elif fmt in ("RGBA", "RGBx"):
                    frame = rows.reshape(height, width, 4)
                elif fmt in ("BGRA", "BGRx"):
                    frame = rows.reshape(height, width, 4)[:, :, [2, 1, 0, 3]].copy()
                elif fmt in ("BGR15", "RGB15"):
                    arr = np.ascontiguousarray(rows).view(np.uint16).reshape(
                        (height, width)
                    )
                    r = ((arr >> 10) & 0x1F).astype(np.uint8) * 255 // 31
                    g = ((arr >> 5) & 0x1F).astype(np.uint8) * 255 // 31
                    b = (arr & 0x1F).astype(np.uint8) * 255 // 31
                    frame = np.stack([r, g, b], axis=2)
                else:
                    # RGB and unknown formats assumed 3 bytes per pixel
                    frame = rows.reshape(height, width, 3)

                with self._frame_lock:
                    self._latest_frame = frame
                    self._latest_frame_data = data

            finally:
                buffer.unmap(map_info)

            return Gst.FlowReturn.OK

        except Exception as e:
            print(f"Error processing frame: {e}")
            return Gst.FlowReturn.OK

    def _on_session_closed_signal(
        self, connection, sender, path, interface, signal, params
    ):
        if path != self._portal_session_handle:
            return
        print("Portal session closed by the compositor; stopping capture pipeline")
        self._unsubscribe_session_closed()
        # The session is already gone remotely - drop the handle without
        # calling Close() on it, and tear down the pipeline so the next
        # capture() re-runs the portal handshake (restore token skips the
        # picker).
        self._portal_session_handle = None
        self._portal_node_id = None
        self._stop_gst_pipeline()
        # The compositor usually closes the session because outputs are
        # changing (monitor sleeping, waking or re-linking). Give them time
        # to settle - and the monitor watcher time to report it - before the
        # next handshake, which would otherwise spend the restore token.
        self._next_portal_retry = time.monotonic() + self._OUTPUT_SETTLE_S

    def _unsubscribe_session_closed(self):
        if self._session_closed_sub is not None and self._portal_bus is not None:
            try:
                self._portal_bus.con.signal_unsubscribe(self._session_closed_sub)
            except Exception:
                pass
        self._session_closed_sub = None

    def _close_portal_session(self):
        self._unsubscribe_session_closed()
        if self._portal_session_handle and self._portal_bus:
            try:
                session = self._portal_bus.get(
                    "org.freedesktop.portal.Desktop",
                    self._portal_session_handle,
                )
                session.Close()
                print("Portal session closed")
            except Exception as e:
                print(f"Error closing portal session (may already be closed): {e}")
        self._portal_session_handle = None
        self._portal_bus = None

    def stop_pipeline(self):
        self._stop_gst_pipeline()
        self._source_width = None
        self._source_height = None
        self._pipeline_scaled = False
        self._needs_pipeline_restart = False
        self._portal_node_id = None
        self._runtime_failed_configs.clear()
        print("GStreamer pipeline stopped")
        self._close_portal_session()

    def __del__(self):
        self.stop_pipeline()
