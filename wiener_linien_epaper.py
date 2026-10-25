#!/usr/bin/env python3

from __future__ import annotations

import logging
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

sys.path.append("/home/lxadm/wiener_linien/e-Paper/RaspberryPi_JetsonNano/python/lib")
from waveshare_epd import epd2in9_V2


# ----------------------------
# Configuration
# ----------------------------

STOP_ID = "2044"
LINE_NAME = "71"
TOWARDS_FILTERS = ("Schottenring", "Schwarzenbergplatz")

API_URL = "https://www.wienerlinien.at/ogd_realtime/monitor"
LOCAL_TZ = ZoneInfo("Europe/Vienna")

DAY_POLL_INTERVAL_SECONDS = 30
NIGHT_START = dtime(0, 0)
NIGHT_END = dtime(6, 0)
REQUEST_TIMEOUT_SECONDS = 8
HTTP_RETRIES = 3
HTTP_BACKOFF_FACTOR = 0.5

MAX_DEPARTURES = 3
MAX_CACHE_AGE = timedelta(minutes=90)
FULL_REFRESH_EVERY_REDRAWS = 20
CLEAR_ON_EXIT = False

FONT_BOLD_PATH = "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"
FONT_MONO_PATH = "/usr/share/fonts/truetype/freefont/FreeMonoBold.ttf"

WHITE = 255
BLACK = 0


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


# ----------------------------
# Data model
# ----------------------------

@dataclass(frozen=True)
class Departure:
    line: str
    towards: str
    departure_at: Optional[datetime]
    api_countdown: Optional[int]
    realtime: bool
    traffic_jam: bool
    cooling: Optional[bool]

    def countdown(self, now: datetime, prefer_api: bool) -> Optional[int]:
        if prefer_api and self.api_countdown is not None:
            return max(0, self.api_countdown)

        if self.departure_at is not None:
            seconds = (self.departure_at - now).total_seconds()
            return max(0, math.ceil(seconds / 60))

        return self.api_countdown

    @property
    def time_text(self) -> str:
        if self.departure_at is None:
            return "--:--"
        return self.departure_at.astimezone(LOCAL_TZ).strftime("%H:%M")


@dataclass(frozen=True)
class Snapshot:
    departures: tuple[Departure, ...]
    disruption: Optional[str]
    traffic_jam: bool
    realtime_supported: bool
    matching_line_found: bool
    fetched_at: datetime


@dataclass(frozen=True)
class State:
    snapshot: Optional[Snapshot]
    stale: bool


@dataclass(frozen=True)
class Fonts:
    header: ImageFont.ImageFont
    clock: ImageFont.ImageFont
    label: ImageFont.ImageFont
    destination: ImageFont.ImageFont
    countdown: ImageFont.ImageFont
    unit: ImageFont.ImageFont
    footer: ImageFont.ImageFont


# ----------------------------
# Generic helpers
# ----------------------------

def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def is_night(now: datetime) -> bool:
    return NIGHT_START <= now.time() < NIGHT_END


def next_sleep_seconds(now: datetime) -> int:
    if not is_night(now):
        return DAY_POLL_INTERVAL_SECONDS

    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return max(1, int((next_hour - now).total_seconds()))


def clean_text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def optional_bool(value: object) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def optional_int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def matches_towards(actual: str) -> bool:
    return not TOWARDS_FILTERS or any(
        destination.casefold() in actual.casefold() for destination in TOWARDS_FILTERS
    )


def parse_api_time(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None

    raw = value.strip().replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        parsed = None

    if parsed is None:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)

    return parsed.astimezone(LOCAL_TZ)


# ----------------------------
# Wiener Linien API
# ----------------------------

class WienerLinienClient:
    def __init__(self) -> None:
        retry = Retry(
            total=HTTP_RETRIES,
            connect=HTTP_RETRIES,
            read=HTTP_RETRIES,
            status=HTTP_RETRIES,
            backoff_factor=HTTP_BACKOFF_FACTOR,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False
        )

        self.session = requests.Session()
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "wiener-linien-epaper/2.0"
        })

    def fetch(self) -> Snapshot:
        params = [
            ("stopId", STOP_ID),
            ("activateTrafficInfo", "stoerungkurz"),
            ("activateTrafficInfo", "stoerunglang")
        ]

        response = self.session.get(
            API_URL,
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("Wiener Linien returned invalid JSON") from exc

        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected Wiener Linien response structure")

        self._raise_api_error(payload)

        data = payload.get("data") or {}
        monitors = data.get("monitors") or []
        fetched_at = now_local()

        matching_lines: list[tuple[dict, dict]] = []
        traffic_info_refs: set[str] = set()

        for monitor in monitors:
            if not isinstance(monitor, dict):
                continue

            for line in monitor.get("lines") or []:
                if not isinstance(line, dict):
                    continue
                if clean_text(line.get("name")) != LINE_NAME:
                    continue
                if not matches_towards(clean_text(line.get("towards"))):
                    continue

                matching_lines.append((monitor, line))

                for ref in monitor.get("refTrafficInfoNames") or []:
                    ref = clean_text(ref)
                    if ref:
                        traffic_info_refs.add(ref)

        departures: list[Departure] = []
        line_realtime_flags: list[bool] = []
        traffic_jam = False

        for _monitor, line in matching_lines:
            line_realtime = optional_bool(line.get("realtimeSupported"))
            if line_realtime is not None:
                line_realtime_flags.append(line_realtime)

            traffic_jam = traffic_jam or bool(optional_bool(line.get("trafficjam")))

            raw_departures = (line.get("departures") or {}).get("departure") or []
            for raw_departure in raw_departures:
                if not isinstance(raw_departure, dict):
                    continue

                departure = self._parse_departure(raw_departure, line, fetched_at)
                if not matches_towards(departure.towards):
                    continue

                departures.append(departure)
                traffic_jam = traffic_jam or departure.traffic_jam

        departures = self._deduplicate(departures)
        departures.sort(key=self._departure_sort_key)
        departures = departures[:MAX_DEPARTURES]

        realtime_supported = any(line_realtime_flags) if line_realtime_flags else any(
            departure.realtime for departure in departures
        )

        return Snapshot(
            departures=tuple(departures),
            disruption=self._find_disruption(data, traffic_info_refs),
            traffic_jam=traffic_jam,
            realtime_supported=realtime_supported,
            matching_line_found=bool(matching_lines),
            fetched_at=fetched_at
        )

    @staticmethod
    def _raise_api_error(payload: dict) -> None:
        message = payload.get("message")
        if not isinstance(message, dict):
            return

        code = optional_int(message.get("messageCode"))
        if code is None or code < 300:
            return

        text = clean_text(message.get("value")) or "Unknown API error"
        raise RuntimeError(f"Wiener Linien API error {code}: {text}")

    @staticmethod
    def _parse_departure(raw: dict, line: dict, fetched_at: datetime) -> Departure:
        departure_time = raw.get("departureTime") or {}
        vehicle = raw.get("vehicle") or {}

        countdown = optional_int(departure_time.get("countdown"))
        time_real = parse_api_time(departure_time.get("timeReal"))
        time_planned = parse_api_time(departure_time.get("timePlanned"))
        departure_at = time_real or time_planned

        if departure_at is None and countdown is not None:
            departure_at = fetched_at + timedelta(minutes=max(0, countdown))

        line_name = clean_text(vehicle.get("name")) or clean_text(line.get("name")) or LINE_NAME
        towards = clean_text(vehicle.get("towards")) or clean_text(line.get("towards")) or "--"

        realtime_supported = optional_bool(vehicle.get("realtimeSupported"))
        if realtime_supported is None:
            realtime_supported = optional_bool(line.get("realtimeSupported"))

        vehicle_traffic = optional_bool(vehicle.get("trafficjam"))
        line_traffic = optional_bool(line.get("trafficjam"))
        traffic_jam = bool(vehicle_traffic if vehicle_traffic is not None else line_traffic)

        return Departure(
            line=line_name,
            towards=towards,
            departure_at=departure_at,
            api_countdown=countdown,
            realtime=time_real is not None or realtime_supported is True,
            traffic_jam=traffic_jam,
            cooling=optional_bool(vehicle.get("cooling"))
        )

    @staticmethod
    def _departure_sort_key(departure: Departure) -> tuple[datetime, int]:
        if departure.departure_at is not None:
            return departure.departure_at, departure.api_countdown or 0

        return now_local() + timedelta(days=3650), departure.api_countdown or 9999

    @staticmethod
    def _deduplicate(departures: list[Departure]) -> list[Departure]:
        result: list[Departure] = []
        seen: set[tuple] = set()

        for departure in departures:
            key = (
                departure.line.casefold(),
                departure.towards.casefold(),
                departure.departure_at.isoformat() if departure.departure_at else None,
                departure.api_countdown
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(departure)

        return result

    @staticmethod
    def _find_disruption(data: dict, referenced_names: set[str]) -> Optional[str]:
        categories_by_id: dict[int, str] = {}
        for category in data.get("trafficInfoCategories") or []:
            category_id = optional_int(category.get("id"))
            if category_id is not None:
                categories_by_id[category_id] = clean_text(category.get("name")).casefold()

        candidates: list[tuple[int, str]] = []

        for info in data.get("trafficInfos") or []:
            if not isinstance(info, dict):
                continue
            if clean_text(info.get("status")).casefold() == "resolved":
                continue

            info_name = clean_text(info.get("name"))
            if referenced_names and info_name not in referenced_names:
                continue

            related_lines = info.get("relatedLines") or []
            if isinstance(related_lines, str):
                related_lines = [item.strip() for item in related_lines.split(",") if item.strip()]

            if related_lines:
                related = {clean_text(item).casefold() for item in related_lines}
                if LINE_NAME.casefold() not in related:
                    continue

            title = clean_text(info.get("title")) or clean_text(info.get("description"))
            if not title:
                continue

            category_id = optional_int(info.get("refTrafficInfoCategoryId"))
            category = categories_by_id.get(category_id or -1, "")
            rank = {"stoerungkurz": 0, "stoerunglang": 1, "information": 2}.get(category, 3)
            candidates.append((rank, title))

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]


# ----------------------------
# State / caching
# ----------------------------

def fetch_state(client: WienerLinienClient, last_good: Optional[Snapshot]) -> tuple[State, Optional[Snapshot]]:
    try:
        snapshot = client.fetch()
        return State(snapshot=snapshot, stale=False), snapshot
    except (requests.RequestException, RuntimeError) as exc:
        logging.warning("API request failed: %s", exc)
        if last_good is not None:
            return State(snapshot=last_good, stale=True), last_good
        return State(snapshot=None, stale=True), None


def visible_departures(state: State, now: datetime) -> tuple[Departure, ...]:
    if state.snapshot is None:
        return tuple()

    if state.stale and now - state.snapshot.fetched_at > MAX_CACHE_AGE:
        return tuple()

    result = []
    for departure in state.snapshot.departures:
        if departure.departure_at is not None and departure.departure_at < now - timedelta(minutes=1):
            continue
        result.append(departure)

    return tuple(result[:MAX_DEPARTURES])


def countdown_text(departure: Optional[Departure], now: datetime, stale: bool) -> str:
    if departure is None:
        return "--"

    value = departure.countdown(now=now, prefer_api=not stale)
    return str(value) if value is not None else "--"


def status_text(state: State, now: datetime) -> str:
    snapshot = state.snapshot

    if snapshot is None:
        return "API nicht erreichbar"

    if state.stale:
        if now - snapshot.fetched_at > MAX_CACHE_AGE:
            return f"Keine aktuellen Daten - Stand {snapshot.fetched_at:%H:%M}"
        return f"Daten veraltet - Stand {snapshot.fetched_at:%H:%M}"

    if snapshot.disruption:
        if snapshot.disruption.casefold().startswith("störung"):
            return snapshot.disruption
        return f"Störung: {snapshot.disruption}"

    if snapshot.traffic_jam:
        return "Stau auf der Strecke"

    if not snapshot.matching_line_found or not visible_departures(state, now):
        return "Keine Abfahrten"

    if not snapshot.realtime_supported:
        return "Nur Fahrplandaten"

    return "Betrieb normal"


def render_key(state: State, now: datetime) -> tuple:
    departures = visible_departures(state, now)
    departure_key = tuple(
        (
            departure.line,
            departure.towards,
            countdown_text(departure, now, state.stale)
        )
        for departure in departures
    )

    clock = now.strftime("%H:%M")

    return clock, departure_key, status_text(state, now), state.stale


# ----------------------------
# Rendering helpers
# ----------------------------

def load_font(path: str, size: int) -> ImageFont.ImageFont:
    requested = Path(path)
    fallback = Path(FONT_BOLD_PATH)

    if requested.exists():
        return ImageFont.truetype(str(requested), size)
    if fallback.exists():
        return ImageFont.truetype(str(fallback), size)

    return ImageFont.load_default()


def load_fonts() -> Fonts:
    return Fonts(
        header=load_font(FONT_BOLD_PATH, 22),
        clock=load_font(FONT_MONO_PATH, 19),
        label=load_font(FONT_BOLD_PATH, 11),
        destination=load_font(FONT_BOLD_PATH, 18),
        countdown=load_font(FONT_MONO_PATH, 28),
        unit=load_font(FONT_BOLD_PATH, 12),
        footer=load_font(FONT_BOLD_PATH, 14)
    )


def text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def fit_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    text = clean_text(text)
    if text_width(draw, text, font) <= max_width:
        return text

    ellipsis = "..."
    low = 0
    high = len(text)

    while low < high:
        mid = (low + high + 1) // 2
        candidate = text[:mid].rstrip() + ellipsis
        if text_width(draw, candidate, font) <= max_width:
            low = mid
        else:
            high = mid - 1

    return text[:low].rstrip() + ellipsis


# ----------------------------
# UI
# ----------------------------

def draw_tram_icon(draw: ImageDraw.ImageDraw, x: int, y: int) -> None:
    # Draw at native resolution so icons remain crisp on a monochrome panel.
    draw.line((x + 4, y, x + 10, y, x + 7, y + 3), fill=WHITE)
    draw.rounded_rectangle((x, y + 4, x + 14, y + 15), radius=2, outline=WHITE)
    draw.rectangle((x + 3, y + 6, x + 11, y + 10), fill=WHITE)
    draw.point((x + 3, y + 13), fill=WHITE)
    draw.point((x + 11, y + 13), fill=WHITE)
    draw.line((x + 3, y + 16, x + 1, y + 18), fill=WHITE)
    draw.line((x + 11, y + 16, x + 13, y + 18), fill=WHITE)


def draw_status(draw: ImageDraw.ImageDraw, state: State, now: datetime,
                fonts: Fonts, width: int) -> None:
    text = status_text(state, now)
    warning = state.stale or bool(state.snapshot and (
        state.snapshot.disruption or state.snapshot.traffic_jam
    ))
    ink = WHITE if warning else BLACK
    draw.rectangle((0, 96, width - 1, 127), fill=BLACK if warning else WHITE)
    if warning:
        draw.polygon(((6, 118), (14, 103), (22, 118)), outline=ink)
        draw.line((14, 108, 14, 112), fill=ink, width=2)
        draw.rectangle((14, 115, 15, 116), fill=ink)
    else:
        draw.ellipse((6, 103, 22, 119), outline=ink)
        if text == "Betrieb normal":
            draw.line((10, 111, 13, 114, 18, 108), fill=ink, width=2)
        else:
            draw.line((14, 107, 14, 111, 18, 111), fill=ink)

    # Two lines keep service alerts readable even without any departures.
    words = text.split()
    first_line = ""
    while words and text_width(draw, (first_line + " " + words[0]).strip(),
                               fonts.footer) <= width - 36:
        first_line = (first_line + " " + words.pop(0)).strip()
    if not first_line and words:
        first_line = fit_text(draw, words.pop(0), fonts.footer, width - 36)
    y = 99 if words else 106
    draw.text((30, y), first_line, font=fonts.footer, fill=ink, anchor="lt")
    if words:
        draw.text((30, 113), fit_text(draw, " ".join(words), fonts.footer, width - 36),
                  font=fonts.footer, fill=ink, anchor="lt")


def render_image(epd, state: State, fonts: Fonts, now: datetime) -> Image.Image:
    width, height = epd.height, epd.width
    image = Image.new("1", (width, height), WHITE)
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle((6, 3, 69, 26), radius=4, fill=BLACK)
    draw_tram_icon(draw, 12, 5)
    draw.text((34, 6), LINE_NAME, font=fonts.header, fill=WHITE, anchor="lt")
    draw.text((81, 10), "ABFAHRTEN", font=fonts.label, fill=BLACK, anchor="lt")
    draw.text((width - 6, 7), now.strftime("%H:%M"), font=fonts.clock,
              fill=BLACK, anchor="rt")
    draw.line((6, 29, width - 6, 29), fill=BLACK)

    departures = visible_departures(state, now)
    draw.line((12, 42, 12, 86), fill=BLACK)
    for index in range(MAX_DEPARTURES):
        departure = departures[index] if index < len(departures) else None
        y = 33 + index * 22
        draw.ellipse((9, y + 6, 15, y + 12), outline=BLACK,
                     fill=BLACK if index == 0 else WHITE)
        countdown = countdown_text(departure, now, state.stale)
        available = width - 57 - text_width(draw, countdown, fonts.countdown)
        destination = departure.towards if departure else "--"
        draw.text((23, y + 1), fit_text(draw, destination, fonts.destination, available),
                  font=fonts.destination, fill=BLACK, anchor="lt")
        draw.text((width - 31, y - 2), countdown, font=fonts.countdown,
                  fill=BLACK, anchor="rt")
        draw.text((width - 6, y + 7), "min", font=fonts.unit, fill=BLACK, anchor="rt")

    draw.line((6, 95, width - 6, 95), fill=BLACK)
    draw_status(draw, state, now, fonts, width)
    return image


# ----------------------------
# E-paper control
# ----------------------------

def show_image(epd, image: Image.Image, full_refresh: bool) -> None:
    epd.init()
    buffer = epd.getbuffer(image)

    if full_refresh:
        epd.display(buffer)
    else:
        try:
            epd.displayPartial(buffer)
        except AttributeError:
            epd.display(buffer)

    epd.sleep()


def shutdown_display(epd) -> None:
    try:
        if CLEAR_ON_EXIT:
            epd.init()
            epd.Clear(0xFF)
        epd.sleep()
    except Exception:
        logging.exception("Failed to shut down e-paper cleanly")


# ----------------------------
# Main
# ----------------------------

def main() -> None:
    epd = epd2in9_V2.EPD()
    fonts = load_fonts()
    client = WienerLinienClient()

    last_good: Optional[Snapshot] = None
    last_rendered_key: Optional[tuple] = None
    redraw_count = 0

    try:
        while True:
            now = now_local()
            state, last_good = fetch_state(client, last_good)
            key = render_key(state, now)

            if key != last_rendered_key:
                full_refresh = redraw_count == 0 or redraw_count % FULL_REFRESH_EVERY_REDRAWS == 0
                image = render_image(epd, state, fonts, now)
                show_image(epd, image, full_refresh)

                last_rendered_key = key
                redraw_count += 1

                logging.info(
                    "Updated display: full=%s stale=%s departures=%d status=%r",
                    full_refresh,
                    state.stale,
                    len(visible_departures(state, now)),
                    status_text(state, now)
                )
            else:
                logging.debug("No visible change; skipped redraw")

            time.sleep(next_sleep_seconds(now))

    except KeyboardInterrupt:
        logging.info("Stopping departure display")
        shutdown_display(epd)


if __name__ == "__main__":
    main()
