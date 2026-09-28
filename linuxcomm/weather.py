"""Current conditions and a short forecast from Open-Meteo (free, no API key)."""

from __future__ import annotations

import datetime as dt
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlencode

from . import APP_NAME, __version__

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
IP_LOCATION_URLS = ("https://ipinfo.io/json", "http://ip-api.com/json/?fields=status,city,regionName,countryCode,lat,lon")
TIMEOUT_S = 10


@dataclass
class Location:
    name: str
    latitude: float
    longitude: float


@dataclass
class DayForecast:
    date: dt.date
    code: int
    high: float
    low: float


@dataclass
class Weather:
    location: str
    temperature: float
    apparent: float
    humidity: float
    wind: float
    code: int
    is_day: bool
    high: float
    low: float
    temp_unit: str
    wind_unit: str
    days: list[DayForecast]


# WMO weather interpretation codes -> (description, day icon, night icon, emoji)
_CLEAR = ("weather-clear-symbolic", "weather-clear-night-symbolic")
_FEW = ("weather-few-clouds-symbolic", "weather-few-clouds-night-symbolic")
_CLOUD = ("weather-overcast-symbolic",) * 2
_FOG = ("weather-fog-symbolic",) * 2
_SCATTERED = ("weather-showers-scattered-symbolic",) * 2
_SHOWERS = ("weather-showers-symbolic",) * 2
_SNOW = ("weather-snow-symbolic",) * 2
_STORM = ("weather-storm-symbolic",) * 2
_CODES = {
    0: ("Clear sky", _CLEAR, "☀️"),
    1: ("Mainly clear", _FEW, "🌤️"),
    2: ("Partly cloudy", _FEW, "⛅"),
    3: ("Overcast", _CLOUD, "☁️"),
    45: ("Fog", _FOG, "🌫️"),
    48: ("Freezing fog", _FOG, "🌫️"),
    51: ("Light drizzle", _SCATTERED, "🌦️"),
    53: ("Drizzle", _SCATTERED, "🌦️"),
    55: ("Heavy drizzle", _SHOWERS, "🌧️"),
    56: ("Freezing drizzle", _SHOWERS, "🌧️"),
    57: ("Heavy freezing drizzle", _SHOWERS, "🌧️"),
    61: ("Light rain", _SCATTERED, "🌦️"),
    63: ("Rain", _SHOWERS, "🌧️"),
    65: ("Heavy rain", _SHOWERS, "🌧️"),
    66: ("Freezing rain", _SHOWERS, "🌧️"),
    67: ("Heavy freezing rain", _SHOWERS, "🌧️"),
    71: ("Light snow", _SNOW, "🌨️"),
    73: ("Snow", _SNOW, "🌨️"),
    75: ("Heavy snow", _SNOW, "❄️"),
    77: ("Snow grains", _SNOW, "🌨️"),
    80: ("Rain showers", _SCATTERED, "🌦️"),
    81: ("Heavy rain showers", _SHOWERS, "🌧️"),
    82: ("Violent rain showers", _SHOWERS, "🌧️"),
    85: ("Snow showers", _SNOW, "🌨️"),
    86: ("Heavy snow showers", _SNOW, "❄️"),
    95: ("Thunderstorm", _STORM, "⛈️"),
    96: ("Thunderstorm with hail", _STORM, "⛈️"),
    99: ("Severe thunderstorm with hail", _STORM, "⛈️"),
}

_US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california",
    "co": "colorado", "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia",
    "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa",
    "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada", "nh": "new hampshire",
    "nj": "new jersey", "nm": "new mexico", "ny": "new york", "nc": "north carolina",
    "nd": "north dakota", "oh": "ohio", "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania",
    "ri": "rhode island", "sc": "south carolina", "sd": "south dakota", "tn": "tennessee",
    "tx": "texas", "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington",
    "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
}


def describe(code: int, is_day: bool = True) -> tuple[str, str, str]:
    """Return (description, symbolic icon name, emoji) for a WMO weather code."""
    text, icons, emoji = _CODES.get(code, ("Unknown", ("weather-severe-alert-symbolic",) * 2, "🌡️"))
    if not is_day and code in (0, 1):
        emoji = "🌙"
    return text, icons[0 if is_day else 1], emoji


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, LookupError):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return f"Weather service error (HTTP {exc.code})"
    if isinstance(exc, (urllib.error.URLError, OSError)):
        return "No connection to the weather service"
    return "Weather data could not be read"


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{__version__}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.load(resp)


def locate_by_ip() -> Location:
    """Approximate location of this network's public IP address."""
    last_error: Exception = LookupError("Could not detect your location")
    for url in IP_LOCATION_URLS:
        try:
            data = _get_json(url)
            if "loc" in data:  # ipinfo.io
                lat, lon = (float(v) for v in data["loc"].split(","))
                name = ", ".join(p for p in (data.get("city"), data.get("region")) if p)
            elif data.get("status") == "success":  # ip-api.com
                lat, lon = float(data["lat"]), float(data["lon"])
                name = ", ".join(p for p in (data.get("city"), data.get("regionName")) if p)
            else:
                continue
            return Location(name or f"{lat:.2f}, {lon:.2f}", lat, lon)
        except (OSError, ValueError, KeyError) as e:
            last_error = e
    raise last_error


def geocode(query: str) -> Location:
    """Resolve "City", "City, Region/Country" or "lat, lon" to a Location."""
    query = query.strip()
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*", query)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return Location(f"{lat:.2f}, {lon:.2f}", lat, lon)
    parts = [p.strip() for p in query.split(",") if p.strip()]
    if not parts:
        raise LookupError("Enter a city name")
    data = _get_json(GEOCODE_URL + "?" + urlencode({"name": parts[0], "count": 10, "language": "en", "format": "json"}))
    results = data.get("results") or []
    if not results:
        raise LookupError(f"Couldn't find “{parts[0]}”")
    qualifiers = [q.lower() for q in parts[1:]]
    qualifiers = [_US_STATES.get(q, q) for q in qualifiers]

    def matches(r: dict) -> bool:
        fields = [str(r.get(k) or "").lower() for k in ("admin1", "admin2", "country", "country_code")]
        return all(any(f and (f == q or f.startswith(q)) for f in fields) for q in qualifiers)

    best = next((r for r in results if matches(r)), results[0])
    name_parts = [best.get("name"), best.get("admin1"), best.get("country_code")]
    name = ", ".join(dict.fromkeys(str(p) for p in name_parts if p))
    return Location(name, float(best["latitude"]), float(best["longitude"]))


def fetch_weather(location: Location, fahrenheit: bool = False) -> Weather:
    params = {
        "latitude": f"{location.latitude:.4f}",
        "longitude": f"{location.longitude:.4f}",
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,is_day,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min",
        "timezone": "auto",
        "forecast_days": 5,
        "temperature_unit": "fahrenheit" if fahrenheit else "celsius",
        "wind_speed_unit": "mph" if fahrenheit else "kmh",
    }
    data = _get_json(FORECAST_URL + "?" + urlencode(params))
    cur, units, daily = data["current"], data.get("current_units", {}), data["daily"]
    days = [
        DayForecast(dt.date.fromisoformat(day), int(code if code is not None else -1), float(hi), float(lo))
        for day, code, hi, lo in zip(daily["time"], daily["weather_code"],
                                     daily["temperature_2m_max"], daily["temperature_2m_min"])
        if hi is not None and lo is not None
    ]
    return Weather(
        location=location.name,
        temperature=float(cur["temperature_2m"]),
        apparent=float(cur["apparent_temperature"]),
        humidity=float(cur["relative_humidity_2m"]),
        wind=float(cur["wind_speed_10m"]),
        code=int(cur["weather_code"]),
        is_day=bool(cur.get("is_day", 1)),
        high=days[0].high if days else float(cur["temperature_2m"]),
        low=days[0].low if days else float(cur["temperature_2m"]),
        temp_unit=units.get("temperature_2m", "°F" if fahrenheit else "°C"),
        wind_unit=units.get("wind_speed_10m", "mph" if fahrenheit else "km/h"),
        days=days,
    )
