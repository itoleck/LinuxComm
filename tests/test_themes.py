"""Tests for the color themes.

Run with:  python3 -m unittest discover -s tests -v
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linuxcomm import themes  # noqa: E402


class ThemeTests(unittest.TestCase):
    def test_the_seven_themes(self):
        self.assertEqual([t.name for t in themes.THEMES.values()],
                         ["Classic", "Soft Dark", "Forest", "Ocean", "Sunset", "Monochrome", "Lavender"])
        for theme in themes.THEMES.values():
            for field in ("background", "surface", "primary", "accent", "text", "text_secondary", "border", "today"):
                self.assertRegex(getattr(theme, field), r"^#[0-9A-F]{6}$", (theme.name, field))

    def test_values_from_the_spec(self):
        classic = themes.THEMES["classic"]
        self.assertEqual((classic.background, classic.primary, classic.today), ("#FFFFFF", "#2563EB", "#EF4444"))
        dark = themes.THEMES["soft-dark"]
        self.assertEqual((dark.background, dark.surface, dark.text), ("#0F172A", "#1E293B", "#F1F5F9"))

    def test_only_soft_dark_is_dark(self):
        self.assertEqual([k for k, t in themes.THEMES.items() if themes.is_dark(t)], ["soft-dark"])

    def test_unknown_theme_falls_back_to_classic(self):
        self.assertEqual(themes.get("no-such-theme").name, "Classic")

    def test_contrast(self):
        self.assertAlmostEqual(themes.contrast("#FFFFFF", "#000000"), 21.0, places=1)
        self.assertEqual(themes.readable_on("#2563EB"), "#FFFFFF")   # Classic buttons: white text
        self.assertEqual(themes.readable_on("#60A5FA"), "#111827")   # Soft Dark's light blue: dark text
        self.assertEqual(themes.readable_on("#F59E0B"), "#111827")   # Forest's amber "today": dark text

    def test_text_on_buttons_is_readable_in_every_theme(self):
        for theme in themes.THEMES.values():
            fg = themes.readable_on(theme.primary)
            self.assertGreaterEqual(themes.contrast(fg, theme.primary), 4.5, theme.name)

    def test_css(self):
        classic = themes.THEMES["classic"]
        old = themes.css(classic, css_variables=False)
        new = themes.css(classic, css_variables=True)
        self.assertIn("@define-color accent_bg_color #2563EB;", old)
        self.assertNotIn(":root", old)                              # GTK < 4.16 has no CSS variables
        self.assertIn("--accent-bg-color: #2563EB;", new)
        self.assertIn("--window-bg-color: #FFFFFF;", new)
        self.assertIn(".week-day.today, .month-day.today { background-color: #EF4444;", new)
        for text in (old, new):
            self.assertNotRegex(text, r"destructive", "the Stop talking button must stay red")
            self.assertEqual(text.count("{"), text.count("}"))


class TextColorTests(unittest.TestCase):
    def test_hex_colors(self):
        self.assertEqual(themes.parse_hex("#000000"), "#000000")
        self.assertEqual(themes.parse_hex(" #1A2B3C "), "#1a2b3c")
        self.assertEqual(themes.parse_hex("ffffff"), "#ffffff")
        self.assertEqual(themes.parse_hex("#abc"), "#aabbcc")
        for bad in ("", "#12345", "#1234567", "black", "#ggg000", None, 0):
            self.assertIsNone(themes.parse_hex(bad), bad)

    def test_main_choices(self):
        self.assertEqual(themes.TEXT_COLORS, {"Black": "#000000", "White": "#ffffff"})
        self.assertEqual(themes.DEFAULT_TEXT_COLOR, "#000000")

    def test_readability_on_the_background(self):
        classic, dark = themes.THEMES["classic"], themes.THEMES["soft-dark"]
        self.assertIsNone(themes.text_color_problem("#000000", classic))
        self.assertIsNone(themes.text_color_problem("#ffffff", dark))
        self.assertIn("White is easier", themes.text_color_problem("#000000", dark))
        self.assertIn("Black is easier", themes.text_color_problem("#ffffff", classic))
        for theme in themes.THEMES.values():  # black suits every light theme
            if not themes.is_dark(theme):
                self.assertIsNone(themes.text_color_problem("#000000", theme), theme.name)

    def test_css_only_touches_labels_on_the_background(self):
        self.assertEqual(themes.text_css("#1a2b3c"), "label.on-background { color: #1a2b3c; opacity: 1; }\n")


if __name__ == "__main__":
    unittest.main()
