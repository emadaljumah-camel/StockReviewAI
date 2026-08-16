import asyncio
import base64
import binascii
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import httpx
from PIL import Image

from models import GeocodingSettings, LocationContext


@dataclass(frozen=True)
class ReverseGeocodeResult:
    context: LocationContext | None
    status: str
    error: str = ""


@dataclass(frozen=True)
class ResolvedLocationContext:
    context: LocationContext | None
    source: str
    gps_metadata_present: bool
    reverse_geocoder_status: str
    reverse_geocoder_error: str = ""


_reverse_geocode_cache: dict[
    tuple[float, float], ReverseGeocodeResult
] = {}
_reverse_geocode_lock = asyncio.Lock()
_last_reverse_geocode_started = 0.0


def _trimmed(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _first_nonempty(mapping: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _trimmed(mapping.get(key))
        if value:
            return value
    return ""


def _ratio_to_float(value: Any) -> float:
    numerator = getattr(value, "numerator", None)
    denominator = getattr(value, "denominator", None)
    if numerator is not None and denominator is not None:
        return float(numerator) / float(denominator)
    if isinstance(value, tuple) and len(value) == 2:
        return float(value[0]) / float(value[1])
    return float(value)


def _coordinate_to_decimal(value: Any, ref: Any) -> float | None:
    if not value:
        return None
    try:
        degrees = _ratio_to_float(value[0])
        minutes = _ratio_to_float(value[1])
        seconds = _ratio_to_float(value[2])
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        return None

    if isinstance(ref, bytes):
        ref = ref.decode("ascii", errors="ignore")
    decimal = degrees + minutes / 60.0 + seconds / 3600.0
    if str(ref or "").upper() in ("S", "W"):
        decimal *= -1
    return decimal


def gps_from_ifd(gps_info: dict[int, Any]) -> tuple[float, float] | None:
    latitude = _coordinate_to_decimal(gps_info.get(2), gps_info.get(1))
    longitude = _coordinate_to_decimal(gps_info.get(4), gps_info.get(3))
    if latitude is None or longitude is None:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude


def extract_gps_from_jpeg_base64(value: str) -> tuple[float, float] | None:
    raw_value = value.strip()
    if raw_value.startswith("data:image/"):
        _, _, raw_value = raw_value.partition(",")
    try:
        image_bytes = base64.b64decode(raw_value, validate=True)
        with Image.open(BytesIO(image_bytes)) as image:
            gps_info = image.getexif().get_ifd(0x8825)
    except (binascii.Error, OSError, SyntaxError, ValueError):
        return None
    if not gps_info:
        return None
    return gps_from_ifd(gps_info)


def strip_jpeg_metadata_base64(value: str) -> str:
    raw_value = value.strip()
    if raw_value.startswith("data:image/"):
        _, _, raw_value = raw_value.partition(",")
    try:
        image_bytes = base64.b64decode(raw_value, validate=True)
    except (binascii.Error, ValueError):
        return raw_value
    if len(image_bytes) < 4 or not image_bytes.startswith(b"\xff\xd8"):
        return raw_value

    output = bytearray(image_bytes[:2])
    position = 2
    while position + 1 < len(image_bytes):
        if image_bytes[position] != 0xFF:
            return raw_value

        marker_start = position
        while position < len(image_bytes) and image_bytes[position] == 0xFF:
            position += 1
        if position >= len(image_bytes):
            return raw_value
        marker = image_bytes[position]
        position += 1

        if marker == 0xDA:
            output.extend(image_bytes[marker_start:])
            break
        if marker == 0xD9:
            output.extend(image_bytes[marker_start:position])
            break
        if marker in (0x01, *range(0xD0, 0xD8)):
            output.extend(image_bytes[marker_start:position])
            continue
        if position + 2 > len(image_bytes):
            return raw_value

        segment_length = int.from_bytes(
            image_bytes[position : position + 2], "big"
        )
        segment_end = position + segment_length
        if segment_length < 2 or segment_end > len(image_bytes):
            return raw_value

        if marker not in (0xE1, 0xED):
            output.extend(image_bytes[marker_start:segment_end])
        position = segment_end

    return base64.b64encode(bytes(output)).decode("ascii")


def _context_from_nominatim(data: Any) -> LocationContext | None:
    if not isinstance(data, dict):
        return None
    address = data.get("address")
    if not isinstance(address, dict):
        return None

    values = {
        "country": _trimmed(address.get("country")),
        "state_or_province": _first_nonempty(
            address,
            ("state", "region", "state_district", "county"),
        ),
        "city": _first_nonempty(
            address,
            ("city", "town", "village", "municipality", "hamlet"),
        ),
        "sublocation": _first_nonempty(
            address,
            ("suburb", "neighbourhood", "quarter", "city_district"),
        ),
    }
    supplied = {key: value for key, value in values.items() if value}
    return LocationContext(**supplied) if supplied else None


async def reverse_geocode_gps(
    latitude: float,
    longitude: float,
    settings: GeocodingSettings,
) -> ReverseGeocodeResult:
    global _last_reverse_geocode_started

    key = (round(float(latitude), 6), round(float(longitude), 6))
    cached = _reverse_geocode_cache.get(key)
    if cached is not None:
        return ReverseGeocodeResult(cached.context, "cache_hit")
    if not settings.reverse_url.strip():
        return ReverseGeocodeResult(None, "disabled")

    async with _reverse_geocode_lock:
        cached = _reverse_geocode_cache.get(key)
        if cached is not None:
            return ReverseGeocodeResult(cached.context, "cache_hit")

        remaining = (
            settings.minimum_interval_seconds
            - (time.monotonic() - _last_reverse_geocode_started)
        )
        if remaining > 0:
            await asyncio.sleep(remaining)
        _last_reverse_geocode_started = time.monotonic()

        try:
            async with httpx.AsyncClient(
                timeout=settings.timeout_seconds
            ) as client:
                response = await client.get(
                    settings.reverse_url,
                    params={
                        "format": "jsonv2",
                        "lat": f"{latitude:.6f}",
                        "lon": f"{longitude:.6f}",
                        "zoom": "18",
                        "addressdetails": "1",
                        "namedetails": "1",
                        "accept-language": "en",
                    },
                    headers={
                        "User-Agent": settings.user_agent,
                        "Accept": "application/json",
                        "Accept-Language": "en",
                    },
                )
                response.raise_for_status()
                context = _context_from_nominatim(response.json())
        except httpx.HTTPStatusError as exc:
            return ReverseGeocodeResult(
                None,
                "error",
                f"HTTP {exc.response.status_code}",
            )
        except (httpx.HTTPError, ValueError) as exc:
            return ReverseGeocodeResult(
                None,
                "error",
                type(exc).__name__,
            )

        result = ReverseGeocodeResult(
            context,
            "ok" if context is not None else "no_location_fields",
        )
        _reverse_geocode_cache[key] = result
        return result


def _catalog_context_is_complete(context: LocationContext | None) -> bool:
    return bool(
        context
        and context.country
        and context.state_or_province
        and context.city
        and context.sublocation
    )


def _merge_contexts(
    catalog: LocationContext | None,
    geocoded: LocationContext | None,
) -> LocationContext | None:
    if catalog is None:
        return geocoded
    if geocoded is None:
        return catalog
    return LocationContext(
        country=catalog.country or geocoded.country,
        state_or_province=(
            catalog.state_or_province or geocoded.state_or_province
        ),
        city=catalog.city or geocoded.city,
        sublocation=catalog.sublocation or geocoded.sublocation,
    )


async def resolve_location_context(
    image_base64: str,
    catalog_context: LocationContext | None,
    settings: GeocodingSettings,
) -> ResolvedLocationContext:
    gps = extract_gps_from_jpeg_base64(image_base64)
    gps_present = gps is not None

    if _catalog_context_is_complete(catalog_context):
        return ResolvedLocationContext(
            catalog_context,
            "lightroom_catalog",
            gps_present,
            "not_needed",
        )
    if gps is None:
        return ResolvedLocationContext(
            catalog_context,
            "lightroom_catalog" if catalog_context else "none",
            False,
            "not_run",
        )

    geocode = await reverse_geocode_gps(gps[0], gps[1], settings)
    effective = _merge_contexts(catalog_context, geocode.context)
    if catalog_context and geocode.context:
        source = "lightroom_catalog+gps_reverse_geocode"
    elif catalog_context:
        source = "lightroom_catalog"
    elif geocode.context:
        source = "gps_reverse_geocode"
    else:
        source = "none"

    return ResolvedLocationContext(
        effective,
        source,
        True,
        geocode.status,
        geocode.error,
    )


def clear_reverse_geocode_cache() -> None:
    global _last_reverse_geocode_started
    _reverse_geocode_cache.clear()
    _last_reverse_geocode_started = 0.0
