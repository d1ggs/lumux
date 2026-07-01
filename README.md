# Lumux — Philips Hue Sync for Wayland

> **This is a fork** of [enginkirmaci/lumux](https://github.com/enginkirmaci/lumux) by
> [Engin Kırmacı](https://github.com/enginkirmaci), who created Lumux and deserves
> all credit for the app itself. This fork adds **screen-share permission persistence**:
> upstream Lumux asks "which monitor to share?" on every launch, because the
> xdg-desktop-portal grant is never remembered. This fork implements the portal's
> `persist_mode`/`restore_token` mechanism, so you're asked **once** and sync starts
> silently on every subsequent launch — including autostart at login.
>
> Install this fork's flatpak from the [Releases page](../../releases) **instead of**
> the Flathub build (see [Install](#install) below). Same app ID, so it cleanly
> replaces/shadows the Flathub install.

Real-time ambient lighting synchronization for Philips Hue lights on Linux (Wayland) using a GTK4/Adwaita interface.

**Status:** Active

## Features

- Real-time screen capture and color synchronization
- Ambilight-style edge zone layout
- GTK4 + libadwaita native interface
- Configurable resolution scale and FPS for performance tuning
- Support for multiple Hue zones and lights
- RGB ↔ XY color conversion for Hue color accuracy
- Smooth color transitions with configurable smoothing
- Multi-monitor support (subject to compositor portal support)
- System tray integration when available

## Install

### This fork's flatpak (Recommended)

Download `lumux-vX.Y.Z.flatpak` from the [Releases page](../../releases), then:

```bash
flatpak install --user ./lumux-vX.Y.Z.flatpak
```

It uses the same app ID as the Flathub build, so a `--user` install takes
priority over (and cleanly coexists with) a system-scope Flathub install —
`flatpak run io.github.enginkirmaci.lumux` and any autostart entry pick up
this build automatically. To go back to upstream:
`flatpak uninstall --user io.github.enginkirmaci.lumux`.

### Upstream Flathub build

The original, without permission persistence (re-prompts every launch):

```bash
flatpak install flathub io.github.enginkirmaci.lumux
```

### From Source

1. Clone the repository:
```bash
git clone https://github.com/d1ggs/lumux.git
cd lumux
```

2. Install in editable mode:
```bash
pip install -e .
```

3. Run:
```bash
python -m lumux
# or, if installed:
lumux
```

### Editable Install
```bash
pip install -e .
```

## Requirements

**System:**
- Linux with a Wayland compositor that supports screen-casting portals (xdg-desktop-portal)

**Common packages (example on Fedora):**
- gtk4, libadwaita, xdg-desktop-portal (install via your distro package manager)

**Python:**
- Python 3.10+ recommended
- Dependencies are managed in `pyproject.toml` and installed automatically with `pip install -e .`:
  - PyGObject>=3.42.0
  - numpy>=1.24.0
  - Pillow>=10.0.0
  - requests>=2.28.0
  - urllib3>=1.26.0
  - pydbus>=0.6.0
  - zeroconf>=0.60.0

## Usage

### First Time Setup

1. Start the application:
```bash
python -m lumux
# or
lumux
```

2. Open **Settings**

3. **Bridge Connection**
   - Discover Bridges or enter IP manually
   - Click **Authenticate** and press the link button on your Hue bridge to obtain the app key

4. **Zone Configuration**
   - Configure ambilight zones for edge-based color capture

5. **Sync Settings**
   - Adjust transition time, brightness scale, smoothing, resolution scale and FPS
   - Save settings

### Starting Sync

1. Click **Start Sync**
2. Zone preview shows real-time screen colors
3. Hue lights will follow the captured colors
4. Click **Stop Sync** to stop

## Configuration

- Settings are saved to `~/.config/lumux/settings.json`
- Autostart can be enabled via settings (if supported by the system)

## Performance Tips

- **Resolution scale:** reduce (e.g. 0.125 or 0.0625) to lower CPU usage
- **FPS:** 15–30 is often sufficient
- **Transition time and smoothing:** increase for smoother output

## Troubleshooting

### Wayland Screen Capture Issues
- Ensure xdg-desktop-portal is installed and properly configured on your distro
- Some compositors (or portal backends) may require additional user permissions or settings

### Bridge Connection Issues
- Ensure your device is on the same local network as the Hue bridge
- Verify the bridge is reachable via the bridge web interface
- Temporarily test with firewall disabled if necessary

### High CPU Usage
- Lower FPS and resolution scale in settings

## Contributing

Contributions welcome. Please open issues and pull requests. If you plan large changes, open an issue first to discuss.

## License

MIT License

## Acknowledgements / Notes

- Lumux is created and maintained by [Engin Kırmacı](https://github.com/enginkirmaci) —
  this fork only adds portal permission persistence on top of his work
- Works with Hue v2 API library (python-hue-v2)
- Screen capture on Wayland depends on the compositor and the portal implementation
