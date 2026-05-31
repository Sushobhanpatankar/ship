"""
Central configuration — all constants, port definitions, and shared utilities.
Every other module imports from here; nothing is hard-coded elsewhere.
"""
import math
import os

# ---------------------------------------------------------------------------
# AISstream WebSocket
# ---------------------------------------------------------------------------
AISSTREAM_WS_URL = "wss://stream.aisstream.io/v0/stream"
AIS_RECONNECT_DELAY_SECONDS = 5
AIS_RECONNECT_MAX_SECONDS = 120

# AIS ship-type codes that cover all energy tankers / gas carriers
AIS_ENERGY_SHIP_TYPES = [80, 81, 82, 83, 84, 85, 86, 87, 88, 89]

# Map AIS integer type → human cargo category
CARGO_TYPE_MAP: dict[int, str] = {
    80: "CRUDE",      # Tanker
    81: "PETROLEUM",  # Tanker, Hazardous category X
    82: "PETROLEUM",  # Tanker, Hazardous category Y
    83: "LNG",        # Tanker, Hazardous category Z / Gas carrier
    84: "CNG",        # Tanker, Hazardous category OS / Gas carrier
    85: "PETROLEUM",  # Tanker, Reserved
    86: "PETROLEUM",
    87: "PETROLEUM",
    88: "CRUDE",      # Tanker, Reserved (often used for VLCCs)
    89: "OTHER",      # Tanker, No additional information
}

# ---------------------------------------------------------------------------
# Indian ports to monitor
# ---------------------------------------------------------------------------
INDIAN_PORTS: list[dict] = [
    {"name": "Vadinar",   "lat": 22.90, "lon": 69.61, "types": ["CRUDE"],                 "radius_nm": 15},
    {"name": "Mundra",    "lat": 22.84, "lon": 69.99, "types": ["CRUDE", "PETROLEUM"],    "radius_nm": 20},
    {"name": "JNPT",      "lat": 18.93, "lon": 72.94, "types": ["PETROLEUM", "LNG"],      "radius_nm": 25},
    {"name": "Hazira",    "lat": 21.08, "lon": 72.64, "types": ["LNG", "CNG"],            "radius_nm": 15},
    {"name": "Dahej",     "lat": 21.72, "lon": 72.58, "types": ["LNG"],                   "radius_nm": 15},
    {"name": "Kochi",     "lat":  9.96, "lon": 76.27, "types": ["LNG", "CRUDE"],          "radius_nm": 20},
    {"name": "Mangalore", "lat": 12.92, "lon": 74.82, "types": ["CRUDE", "PETROLEUM"],    "radius_nm": 15},
    {"name": "Chennai",   "lat": 13.09, "lon": 80.29, "types": ["PETROLEUM", "CRUDE"],    "radius_nm": 20},
    {"name": "Paradip",   "lat": 20.32, "lon": 86.62, "types": ["CRUDE"],                 "radius_nm": 20},
    {"name": "Vizag",     "lat": 17.69, "lon": 83.28, "types": ["CRUDE", "PETROLEUM"],    "radius_nm": 20},
]

# Port names / LOCODEs that may appear in AIS destination fields
INDIA_DESTINATION_KEYWORDS: list[str] = [
    # Port names
    "vadinar", "mundra", "jnpt", "nhava sheva", "hazira", "dahej",
    "kochi", "cochin", "mangalore", "chennai", "madras", "paradip",
    "visakhapatnam", "vizag", "haldia", "kandla", "sikka",
    # Country / region
    "india", "indian",
    # UN LOCODEs (5-char)
    "injai", "inmun", "innsa", "inhza", "indah", "incok",
    "inmrm", "inmaa", "inprt", "invtz", "inhal", "inknd",
]

# ---------------------------------------------------------------------------
# AISstream subscription bounding box
# Covers Arabian Sea + Bay of Bengal + Red Sea approaches + Strait of Malacca
# ---------------------------------------------------------------------------
INDIA_BOUNDING_BOX: list[list[list[float]]] = [
    [[0.0, 50.0], [30.0, 100.0]]
]

# ---------------------------------------------------------------------------
# Classification thresholds
# ---------------------------------------------------------------------------
PORT_ZONE_RADIUS_NM: float = 5.0          # within this → "in port"
INBOUND_NEAR_RADIUS_NM: float = 300.0     # proximity check for inbound
INBOUND_FAR_HORIZON_HOURS: float = 48.0   # course-projection horizon
OUTBOUND_TRACK_HOURS: float = 6.0         # window after port departure
OUTBOUND_MAX_AGE_HOURS: float = 72.0      # max time to track outbound
STALE_PORT_HOURS: float = 2.0             # remove from port if not seen

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
SCRAPER_INTERVAL_SECONDS: int  = int(os.getenv("SCRAPER_INTERVAL_SECONDS",  "1800"))
AGGREGATOR_INTERVAL_SECONDS: int = int(os.getenv("AGGREGATOR_INTERVAL_SECONDS", "300"))
POSITION_RETENTION_HOURS: int  = int(os.getenv("POSITION_RETENTION_HOURS",  "48"))
AGENT_LOOP_SECONDS: int = 60

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
DATABASE_PATH: str = os.getenv("DATABASE_PATH", "ship_tracking.db")

# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in nautical miles between two lat/lon points."""
    R_NM = 3440.065  # Earth radius in nautical miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R_NM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def nearest_indian_port(lat: float, lon: float) -> tuple[str, float]:
    """Return (port_name, distance_nm) for the closest Indian port."""
    best_name, best_dist = "", float("inf")
    for port in INDIAN_PORTS:
        d = haversine(lat, lon, port["lat"], port["lon"])
        if d < best_dist:
            best_dist, best_name = d, port["name"]
    return best_name, best_dist


def is_in_port_zone(lat: float, lon: float) -> tuple[bool, str]:
    """Return (True, port_name) if position is within any port's zone radius."""
    for port in INDIAN_PORTS:
        d = haversine(lat, lon, port["lat"], port["lon"])
        if d <= port["radius_nm"]:
            return True, port["name"]
    return False, ""


def classify_cargo(ship_type: int) -> str:
    """Map AIS ship type integer to cargo category string."""
    return CARGO_TYPE_MAP.get(ship_type, "OTHER")


def destination_is_india(destination: str) -> bool:
    """Return True if the AIS destination field suggests India."""
    if not destination:
        return False
    dest = destination.lower().strip()
    return any(kw in dest for kw in INDIA_DESTINATION_KEYWORDS)


def project_position(lat: float, lon: float, course_deg: float,
                     speed_kn: float, hours: float) -> tuple[float, float]:
    """
    Dead-reckoning: project a vessel's position forward by `hours` at constant
    course and speed. Returns (lat, lon). Uses flat-earth approximation valid
    for ≤2000nm distances.
    """
    dist_nm = speed_kn * hours
    dist_rad = dist_nm / 3440.065
    course_rad = math.radians(course_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    lat2 = math.asin(
        math.sin(lat1) * math.cos(dist_rad)
        + math.cos(lat1) * math.sin(dist_rad) * math.cos(course_rad)
    )
    lon2 = lon1 + math.atan2(
        math.sin(course_rad) * math.sin(dist_rad) * math.cos(lat1),
        math.cos(dist_rad) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def is_heading_toward_india(lat: float, lon: float,
                             course_deg: float, speed_kn: float) -> tuple[bool, str]:
    """
    Return (True, nearest_port_name) if the vessel's projected position in
    INBOUND_FAR_HORIZON_HOURS is within INBOUND_NEAR_RADIUS_NM of any Indian port.
    """
    if speed_kn < 1.0:
        return False, ""
    proj_lat, proj_lon = project_position(lat, lon, course_deg, speed_kn,
                                           INBOUND_FAR_HORIZON_HOURS)
    port_name, dist = nearest_indian_port(proj_lat, proj_lon)
    if dist <= INBOUND_NEAR_RADIUS_NM:
        return True, port_name
    return False, ""
