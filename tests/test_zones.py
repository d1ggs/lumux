import numpy as np
import pytest

from lumux.zones import ZoneProcessor


def _expected_zone_ids(rows, cols):
    ids = [f"top_{i}" for i in range(cols)]
    ids += [f"bottom_{i}" for i in range(cols)]
    ids += [f"left_{i}" for i in range(rows)]
    ids += [f"right_{i}" for i in range(rows)]
    return set(ids)


@pytest.mark.parametrize(
    "shape",
    [
        (8, 300, 3),  # shorter than rows: height // rows == 0
        (2, 344, 3),  # the minimum height the guard allows
        (144, 10, 3),  # narrower than cols: width // cols == 0
        (3, 3, 3),  # degenerate in both directions
    ],
)
def test_frames_smaller_than_the_zone_grid_still_produce_all_zones(shape):
    """A frame can arrive smaller than the zone grid - the black bar detector
    crops an all-black frame (e.g. the first one after wake) down to a few
    pixels. A zero-width zone stride then slices an empty region, whose mean
    is NaN, and int(NaN) raised: the whole frame was dropped with
    "Error processing ambilight: cannot convert float NaN to integer"."""
    processor = ZoneProcessor(rows=16, cols=16)
    frame = np.full(shape, 200, dtype=np.uint8)

    zones = processor.process_image(frame)

    assert set(zones) == _expected_zone_ids(16, 16)
    for zone_id, rgb in zones.items():
        assert len(rgb) == 3, zone_id
        for channel in rgb:
            assert isinstance(channel, int), f"{zone_id}: {channel!r}"
            assert 0 <= channel <= 255, f"{zone_id}: {channel}"


def test_no_numpy_warnings_on_undersized_frames():
    """The NaN came with "Mean of empty slice" warnings; an empty slice must
    not be produced at all rather than merely being tolerated."""
    processor = ZoneProcessor(rows=16, cols=16)
    frame = np.full((4, 20, 3), 128, dtype=np.uint8)

    with np.errstate(all="raise"):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            zones = processor.process_image(frame)

    assert len(zones) == 64


def test_rightmost_and_bottom_pixels_are_sampled():
    """Flooring width // cols dropped the remainder, so the right-hand and
    bottom edges of the screen were never sampled (344 px / 16 zones = 21,
    leaving the last 8 columns unread)."""
    processor = ZoneProcessor(rows=16, cols=16)
    frame = np.zeros((144, 344, 3), dtype=np.uint8)
    # Mark only the final column and final row.
    frame[:, -1] = (255, 0, 0)
    frame[-1, :] = (255, 0, 0)

    zones = processor.process_image(frame)

    assert zones["top_15"][0] > 0, "last top zone never reaches the right edge"
    assert zones["right_0"][0] > 0, "right zones never reach the right edge"


def test_normal_frame_zone_values_are_plausible():
    processor = ZoneProcessor(rows=16, cols=16)
    frame = np.zeros((144, 344, 3), dtype=np.uint8)
    frame[0:20, :] = (10, 20, 30)  # top band

    zones = processor.process_image(frame)

    assert set(zones) == _expected_zone_ids(16, 16)
    assert zones["top_0"] == (10, 20, 30)
