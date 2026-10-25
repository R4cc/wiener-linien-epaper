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
TOWARDS_FILTER = "Schottenring"

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

FONT_REGULAR_PATH = "/usr/share/fonts/truetype/freefont/FreeSans.ttf"
FONT_BOLD_PATH = "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"

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
    header_time: ImageFont.ImageFont
    header_small: ImageFont.ImageFont
    hero_badge: ImageFont.ImageFont
    hero_destination: ImageFont.ImageFont
    hero_exact: ImageFont.ImageFont
    hero_countdown: ImageFont.ImageFont
    hero_unit: ImageFont.ImageFont
    row_badge: ImageFont.ImageFont
    row_destination: ImageFont.ImageFont
    row_countdown: ImageFont.ImageFont
    row_exact: ImageFont.ImageFont
    footer: ImageFont.ImageFont
    footer_icon: ImageFont.ImageFont


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
    if not TOWARDS_FILTER:
        return True
    return TOWARDS_FILTER.casefold() in actual.casefold()


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
        towards = clean_text(vehicle.get("towards")) or clean_text(line.get("towards")) or TOWARDS_FILTER or "--"

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

    if not snapshot.matching_line_found:
        return f"{LINE_NAME} Richtung {TOWARDS_FILTER} nicht gefunden"

    if snapshot.disruption:
        if snapshot.disruption.casefold().startswith("störung"):
            return snapshot.disruption
        return f"Störung: {snapshot.disruption}"

    if snapshot.traffic_jam:
        return "Stau auf der Strecke"

    if not visible_departures(state, now):
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
            countdown_text(departure, now, state.stale),
            departure.time_text
        )
        for departure in departures
    )

    last_update = state.snapshot.fetched_at.strftime("%H:%M") if state.snapshot else "--:--"
    clock = now.strftime("%H:00") if is_night(now) else now.strftime("%H:%M")

    return clock, last_update, departure_key, status_text(state, now), state.stale


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
        header_time=load_font(FONT_BOLD_PATH, 18),
        header_small=load_font(FONT_REGULAR_PATH, 10),
        hero_badge=load_font(FONT_BOLD_PATH, 30),
        hero_destination=load_font(FONT_BOLD_PATH, 23),
        hero_exact=load_font(FONT_BOLD_PATH, 13),
        hero_countdown=load_font(FONT_BOLD_PATH, 46),
        hero_unit=load_font(FONT_BOLD_PATH, 18),
        row_badge=load_font(FONT_BOLD_PATH, 14),
        row_destination=load_font(FONT_REGULAR_PATH, 16),
        row_countdown=load_font(FONT_BOLD_PATH, 19),
        row_exact=load_font(FONT_REGULAR_PATH, 15),
        footer=load_font(FONT_BOLD_PATH, 12),
        footer_icon=load_font(FONT_BOLD_PATH, 12)
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


def draw_centered(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: int
) -> None:
    left, top, right, bottom = box
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    x = left + ((right - left) - width) // 2 - bbox[0]
    y = top + ((bottom - top) - height) // 2 - bbox[1]
    draw.text((x, y), text, font=font, fill=fill)


def draw_right(draw: ImageDraw.ImageDraw, right: int, y: int, text: str, font: ImageFont.ImageFont) -> int:
    x = right - text_width(draw, text, font)
    draw.text((x, y), text, font=font, fill=BLACK)
    return x


def draw_connection_icon(draw: ImageDraw.ImageDraw, x: int, y: int, connected: bool) -> None:
    draw.arc((x, y, x + 16, y + 16), 215, 325, fill=BLACK, width=2)
    draw.arc((x + 3, y + 4, x + 13, y + 14), 215, 325, fill=BLACK, width=2)
    draw.ellipse((x + 7, y + 11, x + 9, y + 13), fill=BLACK)

    if not connected:
        draw.line((x + 1, y + 1, x + 15, y + 15), fill=BLACK, width=2)


# ----------------------------
# UI
# ----------------------------

def draw_hero(
    draw: ImageDraw.ImageDraw,
    departure: Optional[Departure],
    now: datetime,
    stale: bool,
    fonts: Fonts
) -> None:
    badge = (8, 28, 57, 67)
    draw.rectangle(badge, fill=BLACK)
    draw_centered(draw, badge, departure.line if departure else LINE_NAME, fonts.hero_badge, WHITE)

    countdown = countdown_text(departure, now, stale)
    unit_width = text_width(draw, "min", fonts.hero_unit)
    unit_x = 292 - unit_width
    countdown_right = unit_x - 4
    countdown_x = countdown_right - text_width(draw, countdown, fonts.hero_countdown)

    destination = departure.towards if departure else TOWARDS_FILTER or "--"
    destination = fit_text(draw, destination, fonts.hero_destination, max(0, countdown_x - 72))
    draw.text((64, 27), destination, font=fonts.hero_destination, fill=BLACK)

    departure_time = departure.time_text if departure else "--:--"
    draw.text((64, 52), f"Abfahrt: {departure_time}", font=fonts.hero_exact, fill=BLACK)

    draw.text((countdown_x, 20), countdown, font=fonts.hero_countdown, fill=BLACK)
    draw.text((unit_x, 47), "min", font=fonts.hero_unit, fill=BLACK)


def draw_row(
    draw: ImageDraw.ImageDraw,
    departure: Optional[Departure],
    y: int,
    now: datetime,
    stale: bool,
    fonts: Fonts
) -> None:
    badge = (8, y + 2, 41, y + 19)
    draw.rectangle(badge, fill=BLACK)
    draw_centered(draw, badge, departure.line if departure else LINE_NAME, fonts.row_badge, WHITE)

    destination = departure.towards if departure else "--"
    destination = fit_text(draw, destination, fonts.row_destination, 136)
    draw.text((47, y), destination, font=fonts.row_destination, fill=BLACK)

    draw_right(
        draw,
        245,
        y - 1,
        f"{countdown_text(departure, now, stale)} min",
        fonts.row_countdown
    )
    draw_right(
        draw,
        292,
        y,
        departure.time_text if departure else "--:--",
        fonts.row_exact
    )


def draw_footer(draw: ImageDraw.ImageDraw, state: State, now: datetime, fonts: Fonts) -> None:
    text = status_text(state, now)
    healthy = not state.stale and text == "Betrieb normal"
    icon_box = (8, 113, 22, 127)

    if healthy:
        draw.ellipse(icon_box, outline=BLACK, width=2)
        draw_centered(draw, icon_box, "i", fonts.footer_icon, BLACK)
    else:
        draw.ellipse(icon_box, fill=BLACK)
        symbol = "!" if state.stale or "Störung" in text or "Stau" in text else "i"
        draw_centered(draw, icon_box, symbol, fonts.footer_icon, WHITE)

    draw.text((28, 113), fit_text(draw, text, fonts.footer, 263), font=fonts.footer, fill=BLACK)


def render_image(epd, state: State, fonts: Fonts, now: datetime) -> Image.Image:
    width = epd.height
    height = epd.width
    image = Image.new("1", (width, height), WHITE)
    draw = ImageDraw.Draw(image)

    departures = list(visible_departures(state, now))
    while len(departures) < MAX_DEPARTURES:
        departures.append(None)

    draw.text((8, 1), now.strftime("%H:%M"), font=fonts.header_time, fill=BLACK)

    last_update = state.snapshot.fetched_at.strftime("%H:%M") if state.snapshot else "--:--"
    draw_right(
        draw,
        width - 28,
        4,
        f"Letzte Aktualisierung: {last_update}",
        fonts.header_small
    )
    draw_connection_icon(draw, width - 22, 2, not state.stale)

    draw.line((8, 23, width - 8, 23), fill=BLACK)
    draw_hero(draw, departures[0], now, state.stale, fonts)

    draw.line((8, 70, width - 8, 70), fill=BLACK)
    draw_row(draw, departures[1], 72, now, state.stale, fonts)

    draw.line((8, 91, width - 8, 91), fill=BLACK)
    draw_row(draw, departures[2], 93, now, state.stale, fonts)

    draw.line((8, 112, width - 8, 112), fill=BLACK)
    draw_footer(draw, state, now, fonts)

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