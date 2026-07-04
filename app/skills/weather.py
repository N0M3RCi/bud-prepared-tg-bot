from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TOOL_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "Get live weather data including temperature, feels-like, humidity, "
            "wind speed, and precipitation probability for a given city. "
            "Uses the free Open-Meteo API (no API key required). "
            "Works for any city worldwide. Use this when the user asks about "
            "weather, temperature, rain, or conditions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name (e.g. 'Accra', 'London', 'New York')",
                },
                "country": {
                    "type": "string",
                    "description": "Country name or code (optional, helps disambiguate cities)",
                },
                "days": {
                    "type": "integer",
                    "description": "Number of days to forecast (1=today only, 3=3-day, 7=week). Default 1.",
                    "default": 1,
                },
            },
            "required": ["city"],
        },
    },
}

# Simple geocoding cache (city -> lat/lon)
_GEO_CACHE: dict[str, tuple[float, float]] = {}


async def _geocode(city: str, country: str | None = None) -> tuple[float, float]:
    """Resolve city name to (latitude, longitude) using Open-Meteo geocoding API."""
    cache_key = f"{city.lower()}_{country.lower() if country else ''}"
    if cache_key in _GEO_CACHE:
        return _GEO_CACHE[cache_key]

    query = f"{city}"
    if country:
        query += f" {country}"

    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {"name": city, "count": 5, "language": "en", "format": "json"}
    if country:
        params["country"] = country

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results", [])
    if not results:
        raise ValueError(f"Could not find coordinates for '{city}'")

    result = results[0]
    lat, lon = result["latitude"], result["longitude"]
    _GEO_CACHE[cache_key] = (lat, lon)
    logger.info(
        "Geocoded '%s' -> %.4f, %.4f (%s, %s)",
        city, lat, lon,
        result.get("country", ""),
        result.get("admin1", ""),
    )
    return lat, lon


def _format_weather(data: dict[str, Any], city: str) -> str:
    """Format weather API response into a readable string."""
    current = data.get("current", {})
    daily = data.get("daily", {})

    lines = [f"Weather for {city}"]

    if current:
        temp = current.get("temperature_2m")
        feels = current.get("apparent_temperature")
        humidity = current.get("relative_humidity_2m")
        wind = current.get("wind_speed_10m")
        weather_code = current.get("weather_code", 0)

        weather_desc = {
            0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
            45: "Foggy", 48: "Depositing rime fog",
            51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
            61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
            71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
            80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
            95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
        }.get(weather_code, f"Code {weather_code}")

        lines.append(f"Condition: {weather_desc}")
        if temp is not None:
            lines.append(f"Temperature: {temp}degC")
        if feels is not None and feels != temp:
            lines.append(f"Feels like: {feels}degC")
        if humidity is not None:
            lines.append(f"Humidity: {humidity}%")
        if wind is not None:
            lines.append(f"Wind: {wind} km/h")

    if daily:
        precip = daily.get("precipitation_probability_mean")
        if precip:
            days = precip
            day_labels = daily.get("time", [])
            lines.append("")
            lines.append("Precipitation probability:")
            for i, prob in enumerate(days):
                label = day_labels[i] if i < len(day_labels) else f"Day {i+1}"
                lines.append(f"  {label}: {prob}%")

    return "\n".join(lines)


async def get_weather(city: str, country: str | None = None, days: int = 1) -> str:
    """Get live weather for a city."""
    days = max(1, min(days, 7))
    lat, lon = await _geocode(city, country)

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code",
        "daily": "precipitation_probability_mean",
        "timezone": "auto",
        "forecast_days": days,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()

    return _format_weather(data, f"{city}{', ' + country if country else ''}")
