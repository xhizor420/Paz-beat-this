"""Our scale factor and CustomTkinter's must be the same number.

CTk's effective widget scaling is not what you pass to
set_widget_scaling - ScalingTracker.get_widget_scaling multiplies it by
the monitor's DPI factor. That factor is hard-coded to 1 on Linux and is
the real thing on Windows, so an app measured on Linux at 150% and run
on Windows at 150% draws every CTk widget at 2.25x instead of 1.5x,
hands every measured size back 1.5x too wide, asks the window manager
for a window half again too big, and moves a dragged column three times
as far as the pointer.

Every one of those was a bug reported from a Windows machine and
invisible on the machine it was measured on. This pins the invariant:

    T.SCALE  ==  dpi_factor * what_we_pass_to_ctk
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paz_suite import app as appmod      # noqa: E402
from paz_suite import theme              # noqa: E402


class Cfg:
    def __init__(self, ui_scale="Auto"):
        self.ui_scale = ui_scale


class FakeApp:
    _apply_scaling = appmod.PazApp._apply_scaling
    _ctk_dpi_scaling = appmod.PazApp._ctk_dpi_scaling
    SCALE_CHOICES = appmod.PazApp.SCALE_CHOICES

    def __init__(self, dpi=1.0, detected=1.5, ui_scale="Auto"):
        self.cfg = Cfg(ui_scale)
        self.root = object()
        self._dpi = dpi
        self._detected = detected

    def _detect_scale(self):
        return self._detected

    def _ctk_dpi_scaling(self):          # what CTk is already doing
        return self._dpi


@pytest.fixture
def recorder(monkeypatch):
    """Catch what the app asks CustomTkinter for."""
    asked = {}
    monkeypatch.setattr(appmod.ctk, "set_widget_scaling",
                        lambda v: asked.__setitem__("widget", v))
    monkeypatch.setattr(appmod.ctk, "set_window_scaling",
                        lambda v: asked.__setitem__("window", v))
    before = theme.T.SCALE
    yield asked
    theme.T.SCALE = before


def effective(asked, dpi):
    """What CTk will actually scale widgets by - see ScalingTracker."""
    return dpi * asked["widget"]


@pytest.mark.parametrize("dpi", [1.0, 1.25, 1.5, 1.75, 2.0])
def test_widgets_come_out_at_our_scale_on_any_monitor(recorder, dpi):
    app = FakeApp(dpi=dpi, detected=1.5)
    app._apply_scaling()
    assert effective(recorder, dpi) == pytest.approx(theme.T.SCALE)


@pytest.mark.parametrize("dpi", [1.0, 1.5, 2.0])
def test_window_geometry_stays_in_real_pixels(recorder, dpi):
    """The app asks for windows in real pixels. CTk multiplies geometry by
    dpi * window_scaling, so a 2000px window became 3000 on a 150%
    display - which is why it opened bigger than the screen and every
    panel inside it was cramped."""
    app = FakeApp(dpi=dpi, detected=1.5)
    app._apply_scaling()
    assert dpi * recorder["window"] == pytest.approx(1.0)


def test_an_explicit_setting_is_honoured_exactly(recorder):
    app = FakeApp(dpi=1.5, detected=1.0, ui_scale="175%")
    app._apply_scaling()
    assert theme.T.SCALE == pytest.approx(1.75)
    assert effective(recorder, 1.5) == pytest.approx(1.75)


def test_a_monitor_that_already_scales_gets_no_extra(recorder):
    """Windows at 150% reports 1.5 and Tk reports 144 DPI, so the
    detected scale and the monitor's factor are the same - and we must
    ask CTk for nothing on top."""
    app = FakeApp(dpi=1.5, detected=1.5)
    app._apply_scaling()
    assert recorder["widget"] == pytest.approx(1.0)


def test_a_nonsense_dpi_reading_is_ignored():
    app = FakeApp()
    app._ctk_dpi_scaling = appmod.PazApp._ctk_dpi_scaling.__get__(app)

    class Boom:
        pass

    app.root = Boom()
    # get_window_dpi_scaling will throw on this; 1.0 is the safe answer.
    assert app._ctk_dpi_scaling() == 1.0


def test_unscaled_undoes_exactly_what_ctk_will_do(recorder):
    """unscaled() is how a measured size gets back into a CTk widget. It
    is only correct while T.SCALE is CTk's effective scaling."""
    for dpi in (1.0, 1.5, 2.0):
        app = FakeApp(dpi=dpi, detected=1.5)
        app._apply_scaling()
        for real in (200, 682, 1200):
            asked = theme.unscaled(real)
            drawn = asked * effective(recorder, dpi)
            assert abs(drawn - real) <= effective(recorder, dpi)
