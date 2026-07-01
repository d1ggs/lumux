import json
import threading
from dataclasses import asdict

import pytest

from lumux.config.settings_manager import CaptureSettings, SettingsManager


@pytest.fixture
def tmp_config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(SettingsManager, "_get_config_dir", lambda self: tmp_path)
    SettingsManager._instance = None
    yield tmp_path
    SettingsManager._instance = None


def test_capture_settings_restore_token_defaults_to_empty_string():
    assert CaptureSettings().restore_token == ""


def test_capture_settings_restore_token_round_trips_through_dict():
    original = CaptureSettings(restore_token="abc123")

    data = asdict(original)
    restored = CaptureSettings(**data)

    assert restored.restore_token == "abc123"


def test_concurrent_saves_do_not_corrupt_settings_file(tmp_config_dir):
    manager = SettingsManager.get_instance()
    manager.hue.app_key = "precious-app-key"

    def hammer():
        for _ in range(25):
            manager.save()

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    saved = json.loads((tmp_config_dir / "settings.json").read_text())
    assert saved["hue"]["app_key"] == "precious-app-key"
    assert list(tmp_config_dir.glob("*.tmp")) == []


def test_non_string_restore_token_is_reset_to_empty(tmp_config_dir):
    (tmp_config_dir / "settings.json").write_text(
        json.dumps({"capture": {"restore_token": 123}})
    )

    manager = SettingsManager.get_instance()

    assert manager.capture.restore_token == ""
