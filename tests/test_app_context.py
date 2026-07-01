import json

import pytest

from lumux.config.settings_manager import SettingsManager
from lumux.app_context import AppContext


@pytest.fixture
def settings_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(SettingsManager, "_get_config_dir", lambda self: tmp_path)
    SettingsManager._instance = None
    manager = SettingsManager.get_instance()
    yield manager
    SettingsManager._instance = None


def test_app_context_passes_stored_restore_token_to_capture(settings_manager):
    settings_manager.capture.restore_token = "EXISTING-TOKEN"

    context = AppContext(settings_manager)

    assert context.capture._restore_token == "EXISTING-TOKEN"


def test_app_context_saves_new_restore_token_from_capture(settings_manager):
    context = AppContext(settings_manager)

    context.capture._on_restore_token("FRESH-TOKEN")

    assert settings_manager.capture.restore_token == "FRESH-TOKEN"


def test_app_context_saves_new_restore_token_to_disk(settings_manager, tmp_path):
    context = AppContext(settings_manager)

    context.capture._on_restore_token("FRESH-TOKEN")

    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["capture"]["restore_token"] == "FRESH-TOKEN"
