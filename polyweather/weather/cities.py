"""Registry of every city Polymarket runs daily temperature markets on.

Derived from the 56 `*-daily-weather` Gamma series observed in the historical
event dump.  Coordinates target each city's primary reporting station (usually
the airport Polymarket resolves against), since that is what the forecast needs
to match -- a downtown grid point can differ from the airport by a couple of
degrees on a clear night.

`unit` is how Polymarket quotes the ladder: Fahrenheit for US cities, Celsius
everywhere else.  Bucket labels carry the unit too, and those win when present.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class City:
    key: str          # fragment used inside Polymarket slugs
    name: str
    lat: float
    lon: float
    tz: str
    unit: str         # "F" or "C"


_ROWS: list[tuple] = [
    # key, name, lat, lon, tz, unit
    # ---------------- United States (Fahrenheit) ----------------
    ("nyc",           "New York City",  40.7789,  -73.9692, "America/New_York",    "F"),
    ("new-york-city", "New York City",  40.7789,  -73.9692, "America/New_York",    "F"),
    ("dc",            "Washington DC",  38.8512,  -77.0402, "America/New_York",    "F"),
    ("chicago",       "Chicago",        41.9803,  -87.9090, "America/Chicago",     "F"),
    ("los-angeles",   "Los Angeles",    33.9382, -118.3866, "America/Los_Angeles", "F"),
    ("la",            "Los Angeles",    33.9382, -118.3866, "America/Los_Angeles", "F"),
    ("san-francisco", "San Francisco",  37.6197, -122.3647, "America/Los_Angeles", "F"),
    ("seattle",       "Seattle",        47.4444, -122.3139, "America/Los_Angeles", "F"),
    ("denver",        "Denver",         39.8467, -104.6564, "America/Denver",      "F"),
    ("dallas",        "Dallas",         32.8471,  -96.8517, "America/Chicago",     "F"),
    ("houston",       "Houston",        29.9902,  -95.3368, "America/Chicago",     "F"),
    ("austin",        "Austin",         30.1975,  -97.6664, "America/Chicago",     "F"),
    ("miami",         "Miami",          25.7932,  -80.2906, "America/New_York",    "F"),
    ("atlanta",       "Atlanta",        33.6301,  -84.4418, "America/New_York",    "F"),
    ("phoenix",       "Phoenix",        33.4278, -112.0038, "America/Phoenix",     "F"),
    # ---------------- Europe (Celsius) ----------------
    ("london",        "London",         51.4775,   -0.4614, "Europe/London",       "C"),
    ("paris",         "Paris",          49.0097,    2.5479, "Europe/Paris",        "C"),
    ("madrid",        "Madrid",         40.4719,   -3.5626, "Europe/Madrid",       "C"),
    ("milan",         "Milan",          45.6306,    8.7281, "Europe/Rome",         "C"),
    ("munich",        "Munich",         48.3538,   11.7861, "Europe/Berlin",       "C"),
    ("amsterdam",     "Amsterdam",      52.3105,    4.7683, "Europe/Amsterdam",    "C"),
    ("warsaw",        "Warsaw",         52.1657,   20.9671, "Europe/Warsaw",       "C"),
    ("helsinki",      "Helsinki",       60.3172,   24.9633, "Europe/Helsinki",     "C"),
    ("moscow",        "Moscow",         55.4088,   37.9063, "Europe/Moscow",       "C"),
    ("istanbul",      "Istanbul",       41.2753,   28.7519, "Europe/Istanbul",     "C"),
    ("ankara",        "Ankara",         40.1281,   32.9951, "Europe/Istanbul",     "C"),
    # ---------------- Middle East / Africa ----------------
    ("dubai",         "Dubai",          25.2528,   55.3644, "Asia/Dubai",          "C"),
    ("jeddah",        "Jeddah",         21.6796,   39.1565, "Asia/Riyadh",         "C"),
    ("tel-aviv",      "Tel Aviv",       32.0114,   34.8867, "Asia/Jerusalem",      "C"),
    ("cape-town",     "Cape Town",     -33.9715,   18.6021, "Africa/Johannesburg", "C"),
    ("lagos",         "Lagos",           6.5774,    3.3212, "Africa/Lagos",        "C"),
    # ---------------- Asia ----------------
    ("tokyo",         "Tokyo",          35.5533,  139.7811, "Asia/Tokyo",          "C"),
    ("seoul",         "Seoul",          37.5583,  126.7906, "Asia/Seoul",          "C"),
    ("busan",         "Busan",          35.1795,  128.9382, "Asia/Seoul",          "C"),
    ("beijing",       "Beijing",        40.0801,  116.5846, "Asia/Shanghai",       "C"),
    ("shanghai",      "Shanghai",       31.1979,  121.3363, "Asia/Shanghai",       "C"),
    ("guangzhou",     "Guangzhou",      23.3924,  113.2988, "Asia/Shanghai",       "C"),
    ("shenzhen",      "Shenzhen",       22.6393,  113.8108, "Asia/Shanghai",       "C"),
    ("chengdu",       "Chengdu",        30.5785,  103.9471, "Asia/Shanghai",       "C"),
    ("chongqing",     "Chongqing",      29.7192,  106.6417, "Asia/Shanghai",       "C"),
    ("wuhan",         "Wuhan",          30.7838,  114.2081, "Asia/Shanghai",       "C"),
    ("qingdao",       "Qingdao",        36.2661,  120.3744, "Asia/Shanghai",       "C"),
    ("jinan",         "Jinan",          36.8572,  117.2160, "Asia/Shanghai",       "C"),
    ("zhengzhou",     "Zhengzhou",      34.5197,  113.8408, "Asia/Shanghai",       "C"),
    ("hong-kong",     "Hong Kong",      22.3089,  113.9145, "Asia/Hong_Kong",      "C"),
    ("taipei",        "Taipei",         25.0777,  121.2328, "Asia/Taipei",         "C"),
    ("singapore",     "Singapore",       1.3644,  103.9915, "Asia/Singapore",      "C"),
    ("kuala-lumpur",  "Kuala Lumpur",    2.7456,  101.7099, "Asia/Kuala_Lumpur",   "C"),
    ("jakarta",       "Jakarta",        -6.1256,  106.6559, "Asia/Jakarta",        "C"),
    ("manila",        "Manila",         14.5086,  121.0194, "Asia/Manila",         "C"),
    ("karachi",       "Karachi",        24.9065,   67.1608, "Asia/Karachi",        "C"),
    ("lucknow",       "Lucknow",        26.7606,   80.8893, "Asia/Kolkata",        "C"),
    ("delhi",         "Delhi",          28.5665,   77.1031, "Asia/Kolkata",        "C"),
    # ---------------- Americas (non-US) / Oceania ----------------
    ("toronto",       "Toronto",        43.6777,  -79.6248, "America/Toronto",     "C"),
    ("mexico-city",   "Mexico City",    19.4363,  -99.0721, "America/Mexico_City", "C"),
    ("panama-city",   "Panama City",     9.0714,  -79.3835, "America/Panama",      "C"),
    ("panama",        "Panama City",     9.0714,  -79.3835, "America/Panama",      "C"),
    ("sao-paulo",     "Sao Paulo",     -23.4356,  -46.4731, "America/Sao_Paulo",   "C"),
    ("buenos-aires",  "Buenos Aires",  -34.8222,  -58.5358, "America/Argentina/Buenos_Aires", "C"),
    ("wellington",    "Wellington",    -41.3272,  174.8053, "Pacific/Auckland",    "C"),
    ("sydney",        "Sydney",        -33.9399,  151.1753, "Australia/Sydney",    "C"),
]

CITIES: dict[str, City] = {r[0]: City(*r) for r in _ROWS}

# Longest keys first so "san-francisco" wins over "la", and "panama-city" over
# "panama".  Without this, short keys would shadow the specific ones.
_ORDERED = sorted(CITIES.values(), key=lambda c: -len(c.key))


def city_from_slug(slug: str) -> City | None:
    """Extract the city from a slug like `highest-temperature-in-nyc-on-...`."""
    s = (slug or "").lower()
    for c in _ORDERED:
        if f"-in-{c.key}-" in s or f"{c.key}-daily" in s or s.startswith(f"{c.key}-"):
            return c
    return None
