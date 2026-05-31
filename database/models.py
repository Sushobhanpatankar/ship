"""
Domain dataclasses — typed representations of all DB rows.
No ORM; plain Python dataclasses with dict serialization.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Literal

CargoCategory = Literal["CRUDE", "LNG", "CNG", "PETROLEUM", "OTHER"]
NavStatusStr  = Literal["UNDERWAY", "ANCHORED", "MOORED", "STOPPED", "UNKNOWN"]
ActivityStr   = Literal["LOADING", "UNLOADING", "ANCHORED", "BERTHED", "MANEUVERING", "UNKNOWN"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Map AIS navigational status codes → readable strings
NAV_STATUS_MAP: dict[int, NavStatusStr] = {
    0: "UNDERWAY",
    1: "ANCHORED",
    2: "STOPPED",    # not under command
    3: "STOPPED",    # restricted manoeuvrability
    4: "STOPPED",    # constrained by draught
    5: "MOORED",
    6: "ANCHORED",   # aground
    7: "UNDERWAY",   # fishing
    8: "UNDERWAY",   # sailing
    15: "UNKNOWN",
}


def nav_status_str(code: int | None) -> NavStatusStr:
    if code is None:
        return "UNKNOWN"
    return NAV_STATUS_MAP.get(code, "UNDERWAY")


@dataclass
class Vessel:
    mmsi: str
    imo: str = ""
    ship_name: str = ""
    ship_type: int = 0
    cargo_category: CargoCategory = "OTHER"
    flag: str = ""
    length: float = 0.0
    width: float = 0.0
    draft: float = 0.0
    deadweight: float = 0.0
    first_seen: str = field(default_factory=_now)
    last_seen: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Position:
    mmsi: str
    latitude: float
    longitude: float
    speed: float = 0.0
    course: float = 0.0
    heading: float = 0.0
    nav_status: int = 15
    timestamp: str = field(default_factory=_now)
    source: str = "AIS"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class InboundShip:
    mmsi: str
    destination: str = ""
    eta: str = ""
    origin_port: str = ""
    origin_country: str = ""
    cargo_category: CargoCategory = "OTHER"
    ship_name: str = ""
    current_lat: float = 0.0
    current_lon: float = 0.0
    speed: float = 0.0
    course: float = 0.0
    distance_to_port: float = 0.0
    nearest_port: str = ""
    status: NavStatusStr = "UNDERWAY"
    first_detected: str = field(default_factory=_now)
    last_updated: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OutboundShip:
    mmsi: str
    departure_port: str = ""
    departure_time: str = field(default_factory=_now)
    destination: str = ""
    cargo_category: CargoCategory = "OTHER"
    ship_name: str = ""
    current_lat: float = 0.0
    current_lon: float = 0.0
    speed: float = 0.0
    course: float = 0.0
    distance_from_port: float = 0.0
    nav_status: int = 0
    ballast_confirmed: int = 0
    first_detected: str = field(default_factory=_now)
    last_updated: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PortActivity:
    mmsi: str
    port_name: str
    berth: str = ""
    activity: ActivityStr = "UNKNOWN"
    cargo_category: CargoCategory = "OTHER"
    ship_name: str = ""
    arrival_time: str = field(default_factory=_now)
    expected_departure: str = ""
    current_lat: float = 0.0
    current_lon: float = 0.0
    source: str = "AIS"
    last_updated: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AggregatedStats:
    computed_at: str = field(default_factory=_now)
    total_inbound: int = 0
    total_outbound: int = 0
    total_in_port: int = 0
    crude_count: int = 0
    lng_count: int = 0
    cng_count: int = 0
    petroleum_count: int = 0
    busiest_port: str = ""
    stats_json: str = "{}"

    def to_dict(self) -> dict:
        return asdict(self)
