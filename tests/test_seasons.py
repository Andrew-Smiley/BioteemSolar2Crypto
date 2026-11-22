"""Offline regression tests: no miner, Telegram, or Solis requests."""
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
config = types.ModuleType("config")
for key, value in dict(LOG_FILE="/dev/null", TELEGRAM_BOT_TOKEN="test",
                       SOLIS_KEY_ID="", SOLIS_KEY_SECRET="", SOLIS_PLANT_ID="",
                       RIG_IPS=["rig1", "rig2"], POLL_INTERVAL_SEC=30).items():
    setattr(config, key, value)
sys.modules["config"] = config
import controller
import state_store
import telegram_listener as telegram


class StopLoop(BaseException):
    pass


class SeasonalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, file in (("SEASON_PATH", "season.json"),
                           ("OVERRIDE_PATH", "override.json")):
            p = patch.object(state_store, name, str(Path(self.temp.name) / file))
            p.start()
            self.addCleanup(p.stop)

    def run_controller(self, season, readings, override=None, initial="idle"):
        states = []
        commands = Mock()
        client = Mock()
        client.get_current_power_kw.side_effect = readings
        def write(*args, **kwargs):
            states.append((args, kwargs))
        def sleep(_):
            if len(states) >= len(readings):
                raise StopLoop()
        with patch.object(state_store, "get_season", return_value=season), \
             patch.object(state_store, "get_override", return_value=override), \
             patch.object(state_store, "write_state", side_effect=write), \
             patch.object(controller.solis_api, "SolisClient", return_value=client), \
             patch.object(controller, "probe_actual_level", return_value=initial), \
             patch.object(controller, "set_all_rigs_level", commands), \
             patch.object(controller, "collect_rig_statuses", return_value={}), \
             patch.object(controller, "notifier"), \
             patch.object(controller, "daily_stats") as stats, \
             patch.object(controller.time, "sleep", side_effect=sleep):
            stats.should_send_daily_summary.return_value = False
            with self.assertRaises(StopLoop):
                controller.main()
        return commands, states

    def test_default_and_persistence(self):
        self.assertEqual(state_store.get_season(), "summer")
        state_store.set_season("winter")
        self.assertEqual(state_store.get_season(), "winter")
        Path(state_store.SEASON_PATH).write_text("broken")
        self.assertEqual(state_store.get_season(), "summer")
        with self.assertRaises(ValueError):
            state_store.set_season("autumn")

    def test_telegram_keeps_override_and_auto_keeps_season(self):
        state_store.set_override("idle")
        self.assertIn("remains active", telegram.handle_text("/winter"))
        self.assertEqual(state_store.get_override()["mode"], "idle")
        self.assertIn("WINTER", telegram.handle_text("auto"))
        self.assertIsNone(state_store.get_override())
        self.assertEqual(state_store.get_season(), "winter")
        telegram.handle_text("/summer")
        self.assertEqual(state_store.get_season(), "summer")

    def test_winter_ignores_low_solar_and_cloud_outage(self):
        commands, states = self.run_controller(
            "winter", [0.0] + [TimeoutError()] * 6)
        self.assertEqual([c.args[0] for c in commands.call_args_list], ["super"])
        self.assertTrue(all(s[0][1] == "super" for s in states))
        self.assertEqual(states[0][0][0], 0.0)
        self.assertFalse(states[-1][0][3])
        self.assertEqual(states[-1][1]["season"], "winter")

    def test_winter_commands_both_on_entry_even_if_one_already_super(self):
        commands, _ = self.run_controller("winter", [0.0], initial="super")
        self.assertEqual(commands.call_args.args[0], "super")

    def test_manual_idle_wins_in_winter(self):
        commands, states = self.run_controller(
            "winter", [0.0], override={"mode": "idle"}, initial="super")
        self.assertEqual(commands.call_args.args[0], "idle")
        self.assertEqual(states[0][0][1], "idle")

    def test_summer_five_failures_idle(self):
        commands, states = self.run_controller(
            "summer", [TimeoutError()] * 5, initial="super")
        self.assertEqual([c.args[0] for c in commands.call_args_list], ["idle"])
        self.assertEqual(states[-1][0][1], "idle")

    def test_summer_low_solar_idle(self):
        commands, _ = self.run_controller("summer", [0.0], initial="super")
        self.assertEqual(commands.call_args.args[0], "idle")

    def test_summer_high_solar_steps_up(self):
        commands, _ = self.run_controller("summer", [100.0])
        self.assertEqual(commands.call_args.args[0], "eco")

    def test_icons_fit_header_and_are_distinct(self):
        from PIL import Image, ImageDraw
        from epaper.solar_dashboard import draw_season
        images = []
        for season in ("summer", "winter"):
            image = Image.new("1", (264, 176), 255)
            draw_season(ImageDraw.Draw(image), season)
            bbox = image.point(lambda x: 255 - x).getbbox()
            self.assertGreaterEqual(bbox[0], 234)
            self.assertLessEqual(bbox[3], 30)
            images.append(image.tobytes())
        self.assertNotEqual(*images)


if __name__ == "__main__":
    unittest.main()
