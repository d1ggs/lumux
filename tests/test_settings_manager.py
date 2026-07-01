from dataclasses import asdict

from lumux.config.settings_manager import CaptureSettings


def test_capture_settings_restore_token_defaults_to_empty_string():
    assert CaptureSettings().restore_token == ""


def test_capture_settings_restore_token_round_trips_through_dict():
    original = CaptureSettings(restore_token="abc123")

    data = asdict(original)
    restored = CaptureSettings(**data)

    assert restored.restore_token == "abc123"
