"""The UI reserves colour for machine state, so the state colours have to stay
readable. These parse the real tokens out of app.css and check them, which is
the only way a later palette tweak can't quietly drop text below WCAG AA."""
import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parent.parent / "ui" / "assets" / "css" / "app.css").read_text(encoding="utf-8")


def _vars(block: str) -> dict:
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"(--[\w-]+):\s*([^;]+);", block)}


DARK = _vars(CSS.split(":root {")[1].split("}")[0])
LIGHT = {**DARK, **_vars(CSS.split('[data-theme="light"] {')[1].split("}")[0])}


def _rgb(value: str):
    if value.startswith("#"):
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5)) + (1.0,)
    m = re.match(r"rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)", value)
    return (float(m.group(1)), float(m.group(2)), float(m.group(3)),
            float(m.group(4)) if m.group(4) else 1.0)


def _luminance(colour, backdrop=(0, 0, 0, 1.0)):
    a = colour[3]
    rgb = [colour[i] * a + backdrop[i] * (1 - a) for i in range(3)]
    def channel(x):
        x /= 255.0
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2])


def contrast(fg: str, bg: str, theme: dict, over: str = None) -> float:
    """Contrast of token `fg` on token `bg`; `over` composites a translucent
    bg onto an opaque surface first (badge tints sit on a row)."""
    backdrop = _rgb(theme[over]) if over else (0, 0, 0, 1.0)
    hi, lo = sorted((_luminance(_rgb(theme[fg])), _luminance(_rgb(theme[bg]), backdrop)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


THEMES = [("dark", DARK), ("light", LIGHT)]

# Every text tier carries real copy — tertiary holds footnotes and hints — so
# all three are held to AA body text, not to the 3:1 large-text allowance.
TEXT = ["--text", "--text-2", "--text-3"]
SURFACES = ["--bg", "--surface", "--raised"]
STATE = ["--run", "--wait", "--fail"]


@pytest.mark.parametrize("theme_name,theme", THEMES)
@pytest.mark.parametrize("fg", TEXT)
@pytest.mark.parametrize("bg", SURFACES)
def test_text_meets_aa(theme_name, theme, fg, bg):
    assert contrast(fg, bg, theme) >= 4.5


@pytest.mark.parametrize("theme_name,theme", THEMES)
@pytest.mark.parametrize("state", STATE)
def test_status_label_meets_aa(theme_name, theme, state):
    """Status words (Running / Starting / Failed) on a row and on their badge tint."""
    assert contrast(state, "--raised", theme) >= 4.5
    assert contrast(state, state + "-bg", theme, over="--raised") >= 4.5


@pytest.mark.parametrize("theme_name,theme", THEMES)
@pytest.mark.parametrize("state", STATE)
def test_status_spine_is_distinguishable(theme_name, theme, state):
    """The card's left spine encodes state without words, so it is a non-text
    indicator and owes 3:1 (WCAG 1.4.11)."""
    assert contrast(state, "--bg", theme) >= 3.0


@pytest.mark.parametrize("theme_name,theme", THEMES)
def test_focus_ring_is_visible(theme_name, theme):
    assert "outline: 2px solid var(--text-2)" in CSS, "focus ring token changed"
    for surface in SURFACES:
        assert contrast("--text-2", surface, theme) >= 3.0


def test_state_colours_are_the_only_hues():
    """The identity rests on colour meaning something. A gradient or a glow is
    how that discipline gets lost, so neither is allowed back in."""
    assert "linear-gradient" not in CSS
    assert not re.search(r"box-shadow:\s*0 0 \d+px", CSS)
