"""Color themes (Preferences → Appearance).

A theme recolors libadwaita (CSS variables on libadwaita 1.6+, named colors before that)
and a few LinuxComm-specific parts. Destructive colors are never touched, so buttons
like "Stop talking" stay red in every theme.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    name: str
    background: str      # window
    surface: str         # cards, lists, popovers, the captions bar
    primary: str         # buttons, switches, level meters
    accent: str          # highlights: links, focus, "live" status dots
    text: str
    text_secondary: str
    border: str          # card outlines, list dividers
    today: str           # today in the 7-day strip and the month calendar


THEMES: dict[str, Theme] = {
    "classic": Theme("Classic", "#FFFFFF", "#F8F9FA", "#2563EB", "#3B82F6", "#111827", "#6B7280", "#E5E7EB", "#EF4444"),
    "soft-dark": Theme("Soft Dark", "#0F172A", "#1E293B", "#60A5FA", "#38BDF8", "#F1F5F9", "#94A3B8", "#334155", "#F87171"),
    "forest": Theme("Forest", "#F0FDF4", "#FFFFFF", "#166534", "#22C55E", "#14532D", "#4B5563", "#BBF7D0", "#F59E0B"),
    "ocean": Theme("Ocean", "#F0F9FF", "#FFFFFF", "#0369A1", "#0EA5E9", "#0C4A6E", "#64748B", "#BAE6FD", "#F97316"),
    "sunset": Theme("Sunset", "#FFF7ED", "#FFFFFF", "#C2410C", "#F97316", "#9A3412", "#78716C", "#FED7AA", "#DC2626"),
    "monochrome": Theme("Monochrome", "#FAFAFA", "#FFFFFF", "#171717", "#525252", "#0A0A0A", "#737373", "#E5E5E5", "#DC2626"),
    "lavender": Theme("Lavender", "#FAF5FF", "#FFFFFF", "#7C3AED", "#A78BFA", "#4C1D95", "#6B7280", "#E9D5FF", "#EC4899"),
}
DEFAULT_THEME = "classic"


def get(key: str) -> Theme:
    return THEMES.get(key) or THEMES[DEFAULT_THEME]


# -- color math -------------------------------------------------------------------------

def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def luminance(hex_color: str) -> float:
    """WCAG relative luminance."""
    def channel(c: int) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in _rgb(hex_color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def readable_on(background: str, dark: str = "#111827", light: str = "#FFFFFF") -> str:
    """White text if it is clearly readable (WCAG AA, 4.5:1), otherwise whichever reads better."""
    if contrast(light, background) >= 4.5:
        return light
    return dark if contrast(dark, background) > contrast(light, background) else light


def is_dark(theme: Theme) -> bool:
    return luminance(theme.background) < 0.2


def rgba(hex_color: str, alpha: float) -> str:
    r, g, b = _rgb(hex_color)
    return f"rgba({r}, {g}, {b}, {alpha})"


# -- CSS ---------------------------------------------------------------------------------

def _palette(t: Theme) -> dict[str, str]:
    """libadwaita color names (as CSS variable names) and their values in this theme."""
    return {
        "window-bg-color": t.background, "window-fg-color": t.text,
        "view-bg-color": t.surface, "view-fg-color": t.text,
        "headerbar-bg-color": t.background, "headerbar-fg-color": t.text,
        "headerbar-backdrop-color": t.background,
        "card-bg-color": t.surface, "card-fg-color": t.text,
        "popover-bg-color": t.surface, "popover-fg-color": t.text,
        "dialog-bg-color": t.background, "dialog-fg-color": t.text,
        "sidebar-bg-color": t.surface, "sidebar-fg-color": t.text,
        "accent-bg-color": t.primary, "accent-fg-color": readable_on(t.primary),
        "accent-color": t.accent,
    }


def css(t: Theme, css_variables: bool) -> str:
    """The stylesheet for a theme. css_variables: libadwaita 1.6+ (GTK 4.16+) styles with them."""
    palette = _palette(t)
    parts = [f"@define-color {name.replace('-', '_')} {value};" for name, value in palette.items()]
    if css_variables:
        parts.append(":root { " + " ".join(f"--{name}: {value};" for name, value in palette.items()) + " }")
    on_today = readable_on(t.today)
    shadow = rgba("#000000", 0.25 if is_dark(t) else 0.06)
    parts.append(f"""
.card, list.boxed-list, .boxed-list {{ box-shadow: 0 0 0 1px {t.border}, 0 1px 3px 1px {shadow}; }}
list.boxed-list > row, .boxed-list > row {{ border-color: {t.border}; }}
separator {{ background-color: {t.border}; }}
.captions {{ border-top-color: {t.border}; }}
.dim-label, row .subtitle, .caption-line.placeholder, .week-day .weekday, .month-weekday {{
  color: {t.text_secondary}; opacity: 1; }}
.week-day.today, .month-day.today {{ background-color: {t.today}; color: {on_today}; }}
.week-day.today .weekday {{ color: {on_today}; }}
.status-dot.live, .status-dot.incoming, .status-dot.connecting {{ background-color: {t.accent}; }}
""")
    return "\n".join(parts)
