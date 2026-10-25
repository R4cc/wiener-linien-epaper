import sys
import unittest
from dataclasses import replace
from itertools import combinations
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from PIL import ImageDraw

# The display driver needs Raspberry Pi GPIO; parsing and rendering do not.
driver = ModuleType("waveshare_epd")
driver.epd2in9_V2 = SimpleNamespace()
with patch.dict(sys.modules, {"waveshare_epd": driver}):
    import wiener_linien_epaper as monitor


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.now = monitor.datetime(2026, 10, 25, 12, tzinfo=monitor.LOCAL_TZ)

    def fetch(self, lines, **data):
        client = monitor.WienerLinienClient()
        self.addCleanup(client.session.close)
        response = Mock()
        response.json.return_value = {
            "data": {"monitors": [{"lines": lines}], **data}
        }
        with patch.object(client.session, "get", return_value=response), \
                patch.object(monitor, "now_local", return_value=self.now):
            return client.fetch()

    @staticmethod
    def line(destination, countdown=3, vehicle=None, name="71"):
        return {
            "name": name, "towards": destination, "realtimeSupported": True,
            "departures": {"departure": [{
                "departureTime": {"countdown": countdown},
                "vehicle": vehicle or {}
            }]}
        }

    def test_short_turn_line_is_displayed(self):
        snapshot = self.fetch([self.line("Schwarzenbergplatz")])
        self.assertTrue(snapshot.matching_line_found)
        self.assertEqual(snapshot.departures[0].towards, "Schwarzenbergplatz")
        self.assertEqual(monitor.status_text(monitor.State(snapshot, False), self.now),
                         "Betrieb normal")

    def test_mixed_destinations_are_sorted_and_opposite_direction_excluded(self):
        snapshot = self.fetch([
            self.line("Schottenring", 8),
            self.line("Schwarzenbergplatz", 3),
            self.line("Kaiserebersdorf", 1),
            self.line("Schottenring", 2, name="D")
        ])
        self.assertEqual([d.towards for d in snapshot.departures],
                         ["Schwarzenbergplatz", "Schottenring"])

    def test_short_turn_vehicle_under_regular_line(self):
        snapshot = self.fetch([self.line(
            "Schottenring", vehicle={"towards": "Schwarzenbergplatz"}
        )])
        self.assertEqual(snapshot.departures[0].towards, "Schwarzenbergplatz")

    def test_disruption_survives_missing_departures(self):
        snapshot = self.fetch([], trafficInfos=[{
            "title": "Betrieb unterbrochen", "relatedLines": ["71"]
        }])
        self.assertEqual(monitor.status_text(monitor.State(snapshot, False), self.now),
                         "Störung: Betrieb unterbrochen")

    def test_rendering_normal_alert_offline_and_expired_cache(self):
        snapshot = self.fetch([
            self.line("Schwarzenbergplatz", 3), self.line("Schottenring", 12),
            self.line("Schwarzenbergplatz", 120)
        ])
        states = [
            monitor.State(snapshot, False),
            monitor.State(replace(snapshot, disruption=(
                "Betrieb zwischen Schwarzenbergplatz und Schottenring unterbrochen"
            )), False),
            monitor.State(None, True),
            monitor.State(replace(snapshot, fetched_at=self.now - monitor.timedelta(hours=2)), True)
        ]
        fonts = monitor.load_fonts()
        original_text = ImageDraw.ImageDraw.text
        text_boxes = []

        def checked_text(draw, xy, text, *args, **kwargs):
            left, top, right, bottom = draw.textbbox(
                xy, text, font=kwargs["font"], anchor=kwargs["anchor"]
            )
            self.assertGreaterEqual(left, 0, text)
            self.assertGreaterEqual(top, 0, text)
            self.assertLessEqual(right, 296, text)
            self.assertLessEqual(bottom, 128, text)
            text_boxes.append((text, (left, top, right, bottom)))
            return original_text(draw, xy, text, *args, **kwargs)

        for state in states:
            with self.subTest(state=state), \
                    patch.object(ImageDraw.ImageDraw, "text", checked_text):
                text_boxes.clear()
                image = monitor.render_image(SimpleNamespace(width=128, height=296),
                                             state, fonts, self.now)
                self.assertEqual(image.size, (296, 128))
                self.assertEqual(image.mode, "1")
                self.assertIsNotNone(image.getbbox())
                for (first, a), (second, b) in combinations(text_boxes, 2):
                    overlaps = (max(a[0], b[0]) < min(a[2], b[2]) and
                                max(a[1], b[1]) < min(a[3], b[3]))
                    self.assertFalse(overlaps, (first, second))
        draw = ImageDraw.Draw(image)
        available = 296 - 57 - monitor.text_width(draw, "120", fonts.countdown)
        self.assertEqual(monitor.fit_text(draw, "Schwarzenbergplatz", fonts.destination,
                                          available), "Schwarzenbergplatz")


if __name__ == "__main__":
    unittest.main()
