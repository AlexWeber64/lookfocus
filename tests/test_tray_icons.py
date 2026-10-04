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


@pytest.mark.parametrize("state", list(STATE_COLORS))
def test_large_icon_keeps_the_64px_proportions(state):
    small = make_icon(state).convert("L").point(lambda v: 255 if v > 250 else 0)
    big = make_icon(state, 256).convert("L").point(lambda v: 255 if v > 250 else 0)
    # the white glyph covers the same share of the icon at both sizes (it used to stay 64 px-sized)
    share = [i.histogram()[255] / (i.width * i.height) for i in (small, big)]
    assert share[1] == pytest.approx(share[0], rel=0.25)


def test_icons_differ():
    images = [make_icon(s).tobytes() for s in STATE_COLORS]
    assert len(set(images)) == len(images)
