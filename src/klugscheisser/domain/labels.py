"""German source labels and dates, e.g. "[Instagram @alinamueller_, 21.11.]", "[SRF, 28.11.]"."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Zurich")

SOURCE_TYPE_DE = {
    "athlete_post": "eigener Instagram-Post",
    "tagged_post": "Instagram-Post, in dem sie markiert/erwähnt ist",
    "srf": "SRF",
    "official_result": "offizielles Resultat",
    "other_media": "anderes Medium",
}


def local(dt: datetime | None) -> datetime | None:
    return dt.astimezone(TZ) if dt else None


def date_de(dt: datetime | None, with_year: bool | None = None) -> str:
    """'21.11.' in the current year, '21.11.2025' otherwise."""
    d = local(dt)
    if d is None:
        return "ohne Datum"
    if with_year is None:
        with_year = d.year != datetime.now(TZ).year
    return d.strftime("%d.%m.%Y" if with_year else "%d.%m.")


def datetime_de(dt: datetime | None) -> str:
    d = local(dt)
    return d.strftime("%d.%m.%Y, %H:%M") if d else "unbekannt"


def source_label(platform: str, source_name: str | None, published_at: datetime | None) -> str:
    when = date_de(published_at)
    if platform == "instagram":
        return f"[Instagram {source_name or ''}, {when}]".replace("  ", " ")
    if platform == "x":
        return f"[X {source_name or ''}, {when}]".replace("  ", " ")
    return f"[{source_name or platform.upper()}, {when}]"


# IOC code -> ISO 3166 alpha-2 (for the flag emoji next to names); unknown codes get no flag
IOC_TO_ISO2 = {
    "SUI": "CH",
    "SWE": "SE",
    "FIN": "FI",
    "CAN": "CA",
    "USA": "US",
    "CZE": "CZ",
    "GER": "DE",
    "AUT": "AT",
    "JPN": "JP",
    "DEN": "DK",
    "NOR": "NO",
    "FRA": "FR",
    "ITA": "IT",
    "SVK": "SK",
    "HUN": "HU",
    "CHN": "CN",
    "KOR": "KR",
    "GBR": "GB",
    "NED": "NL",
    "POL": "PL",
    "LAT": "LV",
    "SLO": "SI",
    "CRO": "HR",
    "GRE": "GR",
    "POR": "PT",
    "ESP": "ES",
    "BEL": "BE",
    "AND": "AD",
    "LIE": "LI",
    "BUL": "BG",
    "ROU": "RO",
    "UKR": "UA",
    "RUS": "RU",
    "BLR": "BY",
    "EST": "EE",
    "LTU": "LT",
    "KAZ": "KZ",
    "AUS": "AU",
    "NZL": "NZ",
    "ARG": "AR",
    "CHI": "CL",
    "BRA": "BR",
    "MEX": "MX",
    "ISL": "IS",
    "IRL": "IE",
    "ISR": "IL",
    "TUR": "TR",
    "SRB": "RS",
    "BIH": "BA",
    "MKD": "MK",
    "MNE": "ME",
    "GEO": "GE",
    "ARM": "AM",
    "LUX": "LU",
    "MON": "MC",
    "SMR": "SM",
    "PRK": "KP",
    "TPE": "TW",
    "HKG": "HK",
    "MGL": "MN",
    "RSA": "ZA",
}


def flag(ioc: str | None) -> str:
    """'SUI' -> '🇨🇭' (empty string when unknown)."""
    iso = IOC_TO_ISO2.get((ioc or "").upper())
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso) if iso else ""


def with_flag(name: str, ioc: str | None) -> str:
    f = flag(ioc)
    return f"{f} {name}" if f else name


STAFF_LABELS = {
    "Head coach": "Headcoach",
    "Assistant coach": "Assistenztrainer:in",
    "Goalie coach": "Goalie-Trainer:in",
    "General manager": "General Manager:in",
    "Staff": "Staff",
}


def staff_label(role: str | None) -> str | None:
    """German label for a staff role on a roster entry (None for player positions G/D/F)."""
    return STAFF_LABELS.get(role or "")
