import pytest

from lookfocus.tray import STATE_COLORS, make_icon


@pytest.mark.parametrize("state", list(STATE_COLORS))
def test_icon_per_state(state):
    img = make_icon(state)
    assert img.size == (64, 64) and img.mode == "RGBA"
    assert img.getpixel((0, 0))[3] == 0  # transparent corner
    assert img.getpixel((32, 6))[:3] == STATE_COLORS[state]  # state colour on the disc
    assert (255, 255, 255, 255) in {
        img.getpixel((x, y)) for x in range(16, 48) for y in range(16, 48)
    }  # glyph


def test_icons_differ():
    images = [make_icon(s).tobytes() for s in STATE_COLORS]
    assert len(set(images)) == len(images)
