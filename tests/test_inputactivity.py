from lookfocus.inputactivity import InputTracker

POLL = 0.04


class Sim:
    """Simulates the polled Win32 state: last input time, pointer position, button state."""

    def __init__(self):
        self.t = 0.0
        self.last_input = -100.0
        self.cursor = (500, 500)
        self.buttons = False
        self.tr = InputTracker()
        self.state = self.tr.update(self.t, self.t - self.last_input, self.cursor, False)

    def poll(self, n=1):
        for _ in range(n):
            self.t += POLL
            self.state = self.tr.update(self.t, self.t - self.last_input, self.cursor, self.buttons)
        return self.state

    def key(self):
        self.last_input = self.t + 0.01  # a key press between two polls
        return self.poll()

    def move(self, dx, dy=0):
        self.cursor = (self.cursor[0] + dx, self.cursor[1] + dy)
        self.last_input = self.t + 0.01
        return self.poll()


def test_typing_is_inferred():
    s = Sim()
    st = s.key()
    assert st.keyboard_at > 0 and st.mouse_active_at == float("-inf")


def test_mouse_move_is_mouse_not_typing():
    s = Sim()
    st = s.move(30)
    assert st.mouse_active_at == s.t and st.keyboard_at == float("-inf")


def test_button_press_is_mouse():
    s = Sim()
    s.buttons = True
    s.last_input = s.t + 0.01
    st = s.poll()
    assert st.mouse_active_at == s.t and st.keyboard_at == float("-inf")


def test_jiggler_is_neither_mouse_use_nor_typing():
    s = Sim()
    for _ in range(10):  # +1/-1 px nudges, like a keep-awake tool
        s.move(1)
        s.poll(5)
        s.move(-1)
        s.poll(5)
    assert s.state.mouse_active_at == float("-inf")
    assert s.state.keyboard_at == float("-inf")


def test_typing_after_mouse_counts():
    s = Sim()
    s.move(50)
    s.poll(10)
    st = s.key()
    assert st.keyboard_at > st.mouse_active_at


def test_own_injected_key_tap_is_ignored():
    s = Sim()
    s.tr.ignore(s.t, s.t + 0.3)
    s.key()
    assert s.state.keyboard_at == float("-inf")
    s.poll(10)
    assert s.key().keyboard_at > 0  # later real typing still counts


def test_own_pointer_move_is_not_mouse_use():
    s = Sim()
    s.cursor = (2000, 900)
    s.tr.reset_cursor(s.cursor)
    s.tr.ignore(s.t, s.t + 0.2)
    s.last_input = s.t + 0.01
    st = s.poll()
    assert st.mouse_active_at == float("-inf") and st.keyboard_at == float("-inf")


def test_idle_means_nothing_new():
    s = Sim()
    s.key()
    first = s.state.keyboard_at
    s.poll(50)
    assert s.state.keyboard_at == first
