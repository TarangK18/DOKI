#!/usr/bin/env python3
"""Tests for scale.py — no display, no Qt. Run: python3 tests/test_scale.py"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scale import (MAIN, SMALL, Config, DailyRatio, ScaleState,  # noqa: E402
                   STABLE_BAND_G, STALE_S, parse_frame)


class TestParse(unittest.TestCase):
    def test_real_frame(self):
        self.assertEqual(parse_frame(b"+009.650 kg\r\n"), 9650.0)

    def test_negative(self):
        self.assertEqual(parse_frame(b"-000.020 kg\r\n"), -20.0)

    def test_zero(self):
        self.assertEqual(parse_frame(b"+000.000 kg\r\n"), 0.0)

    def test_no_space_before_unit(self):
        self.assertEqual(parse_frame(b"+012.345kg\r\n"), 12345.0)

    def test_frame_embedded_in_noise(self):
        # A partial frame from mid-stream startup still yields the good part.
        self.assertEqual(parse_frame(b"5 kg\r\n+001.200 kg\r\n"), 1200.0)

    def test_garbage_rejected(self):
        for bad in (b"", b"\r\n", b"kg\r\n", b"+9.65 kg\r\n", b"\x00\xff\x1b[2J"):
            self.assertIsNone(parse_frame(bad), bad)


class TestDedupAndStability(unittest.TestCase):
    def setUp(self):
        self.s = ScaleState()
        self.s.set_connected(True)
        self.t = 1000.0

    def feed(self, grams, dt=0.1):
        self.t += dt
        return self.s.on_frame(grams, now=self.t)

    def test_duplicate_pair_counts_once(self):
        # The scale sends every reading twice.
        self.assertTrue(self.feed(9650.0))
        self.assertFalse(self.feed(9650.0))
        self.assertEqual(self.s.dup_count, 1)
        self.assertEqual(self.s.seq, 1)
        self.assertEqual(len(self.s.log), 1)

    def test_value_returning_after_a_change_is_not_a_duplicate(self):
        self.feed(100.0); self.feed(105.0); self.feed(100.0)
        self.assertEqual(self.s.seq, 3)
        self.assertEqual(self.s.dup_count, 0)

    def test_duplicates_still_count_as_reception(self):
        # A held weight streams identical frames; that must read as alive.
        for _ in range(20):
            self.feed(500.0)
        self.assertTrue(self.s._snapshot(self.t)["fresh"])
        self.assertEqual(self.s.rx_count, 20)

    def test_steady_stream_becomes_stable(self):
        for _ in range(20):
            self.feed(500.0)
        self.assertTrue(self.s.stable)

    def test_dedup_does_not_hide_stability(self):
        # Stability is judged on raw frames; a run of duplicates is the proof.
        for _ in range(20):
            self.feed(500.0)
        self.assertEqual(self.s.seq, 1)      # one accepted reading
        self.assertTrue(self.s.stable)       # but definitely settled

    def test_moving_reading_is_not_stable(self):
        for i in range(20):
            self.feed(500.0 + i * 20)
        self.assertFalse(self.s.stable)

    def test_spread_exactly_at_band_is_stable(self):
        for i in range(20):
            self.feed(500.0 + (STABLE_BAND_G if i % 2 else 0))
        self.assertTrue(self.s.stable)

    def test_spread_one_gram_past_band_is_not_stable(self):
        for i in range(20):
            self.feed(500.0 + (STABLE_BAND_G + 1 if i % 2 else 0))
        self.assertFalse(self.s.stable)

    def test_too_few_samples_is_not_stable(self):
        self.feed(500.0); self.feed(500.0)
        self.assertFalse(self.s.stable)

    def test_stability_window_forgets_old_samples(self):
        for _ in range(20):
            self.feed(500.0)
        self.assertTrue(self.s.stable)
        self.feed(9000.0)            # big step; window still holds the old values
        self.assertFalse(self.s.stable)
        for _ in range(20):          # once settled at the new value, stable again
            self.feed(9000.0)
        self.assertTrue(self.s.stable)

    def test_silence_goes_stale_and_hides_the_number(self):
        for _ in range(20):
            self.feed(500.0)
        snap = self.s._snapshot(self.t + STALE_S + 0.1)
        self.assertFalse(snap["fresh"])
        self.assertIsNone(snap["grams"])          # never show a confident stale value
        self.assertEqual(snap["last_grams"], 500.0)
        self.assertFalse(snap["stable"])

    def test_disconnect_clears_stability(self):
        for _ in range(20):
            self.feed(500.0)
        self.s.set_connected(False, "port gone")
        snap = self.s.snapshot()
        self.assertFalse(snap["stable"])
        self.assertFalse(snap["fresh"])
        self.assertEqual(snap["error"], "port gone")

    def test_bad_lines_counted_not_fatal(self):
        self.feed(500.0)
        self.s.on_bad_line(); self.s.on_bad_line()
        self.assertEqual(self.s.bad_count, 2)
        self.assertEqual(self.s.snapshot()["counts"]["bad"], 2)


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.cfg = Config.load()

    def test_the_floor_scale_reads_to_one_gram(self):
        self.assertEqual(self.cfg.main.division_g, 1.0)

    def test_two_scales_are_configured(self):
        self.assertIsNotNone(self.cfg.small)
        self.assertLess(self.cfg.small.division_g, self.cfg.main.division_g)

    def test_crossover_is_derived_from_the_main_division(self):
        # 2 divisions of the floor scale must fit inside the percentage.
        self.assertAlmostEqual(self.cfg.derived_crossover_g,
                               2 * self.cfg.main.division_g / self.cfg._tol_pct)
        # 1 g floor scale, 2 % tolerance -> 100 g.
        self.assertAlmostEqual(self.cfg.derived_crossover_g, 100.0)

    def test_big_target_goes_to_the_floor_scale(self):
        self.assertEqual(self.cfg.scale_for(576), MAIN)

    def test_small_target_goes_to_the_bench_scale(self):
        self.assertEqual(self.cfg.scale_for(57.6), SMALL)

    def test_routing_at_the_crossover_boundary(self):
        xo = self.cfg.main_min_target_g
        self.assertEqual(self.cfg.scale_for(xo), MAIN)
        self.assertEqual(self.cfg.scale_for(xo - 0.01), SMALL)

    def test_tolerance_follows_the_scale_that_weighs_it(self):
        # 58 g of salt used to get a +-10 g window because the floor scale was
        # the only option. On the bench scale it gets the 2 % the recipe asked
        # for, which is the entire point of the second scale.
        self.assertAlmostEqual(self.cfg.tol_of(57.6), 57.6 * 0.02, places=4)
        self.assertLess(self.cfg.tol_of(57.6), 2.0)

    def test_tolerance_floors_at_two_divisions_of_the_bench_scale(self):
        tiny = 1.0
        self.assertEqual(self.cfg.scale_for(tiny), SMALL)
        self.assertAlmostEqual(self.cfg.tol_of(tiny), 2 * self.cfg.small.division_g)

    def test_degraded_flag_marks_a_floored_tolerance(self):
        self.assertTrue(self.cfg.tolerance_degraded(1.0))     # floor beats 2 %
        self.assertFalse(self.cfg.tolerance_degraded(57.6))   # 2 % applies

    def test_percentage_takes_over_for_large_targets(self):
        self.assertAlmostEqual(self.cfg.tol_of(10000), 200.0)

    def test_below_the_bench_scale_resolution_is_refused(self):
        why = self.cfg.unweighable(self.cfg.small.division_g)
        self.assertIsNotNone(why)
        self.assertIn("under two divisions", why)

    def test_zero_target_is_refused(self):
        self.assertIsNotNone(self.cfg.unweighable(0))

    def test_no_dead_zone_with_the_shipped_specs(self):
        self.assertIsNone(self.cfg.dead_zone)

    def test_dead_zone_is_reported_when_the_bench_scale_is_too_small(self):
        # A 100 g precision scale cannot reach the floor scale's 500 g floor,
        # leaving targets in between unweighable on either.
        import copy
        data = copy.deepcopy(self.cfg.data)
        data["scales"]["small"]["usable_g"] = 40
        cfg = Config(data)
        self.assertEqual(cfg.dead_zone, (40, 100))
        why = cfg.unweighable(70)
        self.assertIn("neither scale", why)

    def test_crossover_override_moves_ingredients_to_the_floor_scale(self):
        import copy
        data = copy.deepcopy(self.cfg.data)
        data["scales"]["crossover_g"] = 20
        cfg = Config(data)
        self.assertEqual(cfg.scale_for(60), MAIN)           # was SMALL
        self.assertTrue(cfg.tolerance_degraded(60))         # and says so
        self.assertAlmostEqual(cfg.tol_of(60), 2.0)         # floor scale's floor

    def test_a_recipe_with_no_second_scale_still_refuses_tiny_targets(self):
        import copy
        data = copy.deepcopy(self.cfg.data)
        del data["scales"]["small"]
        cfg = Config(data)
        self.assertIsNone(cfg.small)
        self.assertIsNotNone(cfg.unweighable(6))

    def test_witness_tolerance_allows_for_main_scale_quantisation(self):
        # Two quantised readings can disagree by a division with nothing wrong.
        self.assertGreaterEqual(self.cfg.witness_tolerance(30),
                                2 * self.cfg.main.division_g)

    def test_can_witness_is_false_below_two_main_divisions(self):
        # At 1 g the floor scale can corroborate almost any bench weighing.
        self.assertFalse(self.cfg.can_witness(1))
        self.assertTrue(self.cfg.can_witness(6))

    def test_products_carry_a_name_and_an_id(self):
        for p in self.cfg.products:
            self.assertTrue(p["id"] and p["name"], p)

    def test_every_shipped_recipe_is_usable(self):
        for p in self.cfg.products:
            self.assertFalse(self.cfg.is_draft(p["id"]), p["id"])

    def test_a_recipe_with_no_weighed_ingredients_is_a_draft(self):
        import copy
        data = copy.deepcopy(self.cfg.data)
        data["products"] = [{"id": "empty", "name": "Empty", "meat": None,
                             "ingredients": []},
                            {"id": "zeros", "name": "Zeros", "meat": None,
                             "ingredients": [["Liquid smoke", 0]]}]
        cfg = Config(data)
        self.assertTrue(cfg.is_draft("empty"))
        # All-zero is a draft too: nothing would be weighed.
        self.assertTrue(cfg.is_draft("zeros"))

    def test_a_zero_quantity_ingredient_is_listed_but_not_weighed(self):
        # Kept on the recipe so nobody wonders whether it was forgotten, but a
        # zero-weight step would refuse to start the batch.
        names = [n for n, _ in self.cfg.reference_ingredients("teriyaki_jerky")]
        self.assertEqual(names, ["liquid smoke"])
        active = [n for n, _ in self.cfg.active_ingredients("teriyaki_jerky")]
        self.assertNotIn("liquid smoke", active)

    def test_zero_quantity_ingredients_do_not_block_a_batch(self):
        from panel_stub import Step
        for pid in ("teriyaki_jerky", "pepper_jerky"):
            steps = [Step(n, pct, 10000 * pct / 100)
                     for n, pct in self.cfg.active_ingredients(pid)]
            self.assertFalse(self.cfg.unweighable_steps(steps), pid)

    def test_an_ingredient_added_twice_is_two_distinct_steps(self):
        # Two weighings at different stages, so the names must differ or the
        # operator cannot tell which is which.
        names = [n for n, _ in self.cfg.active_ingredients("masala_jerky")]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("CRACKED BLACK PEPPER", names)
        self.assertIn("CRACKED BLACK PEPPER (2nd addition)", names)

    def test_meat_is_carried_on_the_product_not_asked(self):
        # None is allowed and means "not decided yet"; the batch records that
        # rather than guessing an animal.
        for p in self.cfg.products:
            meat = self.cfg.meat_of(p["id"])
            self.assertTrue(meat is None or isinstance(meat, str))

    def test_every_recipe_is_weighable_at_its_own_minimum_batch(self):
        from panel_stub import Step
        for p in self.cfg.products:
            if self.cfg.is_draft(p["id"]):
                continue
            floor = self.cfg.min_base_for(p["id"])
            for base in (floor, floor * 1.5, 3200, 8000):
                steps = [Step(n, pct, base * pct / 100)
                         for n, pct in self.cfg.active_ingredients(p["id"])]
                bad = self.cfg.unweighable_steps(steps)
                self.assertFalse(bad, f"{p['id']} at {base:.0f} g: {bad}")

    def test_a_recipe_with_a_tiny_ingredient_needs_a_bigger_batch(self):
        # Bhut jholokia at 2.5 g per 10 kg does not reach two bench-scale
        # divisions until the batch is 800 g, so Teriyaki cannot be made at the
        # 500 g global minimum.
        self.assertGreater(self.cfg.min_base_for("teriyaki_jerky"), self.cfg.min_base_g)
        self.assertAlmostEqual(self.cfg.min_base_for("teriyaki_jerky"), 800.0, places=3)

    def test_quantities_match_the_supplied_sheets(self):
        """recipes.json against the figures as written, in g per 10 kg."""
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from recipe_data import RECIPES, as_percent
        for name, (meat, rows) in RECIPES.items():
            pid = name.lower().replace(" ", "_")
            stored = self.cfg.product(pid)["ingredients"]
            self.assertEqual(len(stored), len(rows), name)
            for (sn, sp), (n, g) in zip(stored, rows):
                self.assertEqual(sn, n, name)
                self.assertAlmostEqual(sp, as_percent(g), places=6,
                                       msg=f"{name}: {n}")
            self.assertEqual(self.cfg.meat_of(pid), meat, name)

    def test_a_recipe_with_no_tiny_ingredient_uses_the_global_minimum(self):
        self.assertEqual(self.cfg.min_base_for("vinegar_bath"), self.cfg.min_base_g)

    def test_every_recipe_can_be_made_at_a_10_kg_batch(self):
        from panel_stub import Step
        for p in self.cfg.products:
            steps = [Step(n, pct, 10000 * pct / 100)
                     for n, pct in self.cfg.active_ingredients(p["id"])]
            self.assertFalse(self.cfg.unweighable_steps(steps), p["id"])


class TestWaterRatio(unittest.TestCase):
    """The day's water/flour ratio, and the gate it drives."""

    GATED = {
        "id": "gated_test", "name": "Gated Test", "meat": "Chicken",
        "ingredients": [["Binder (starch)", 18.0], ["Ice water", 10.0],
                        ["Salt", 1.8]],
        "flour_ingredient": "Binder (starch)",
        "water_ingredient": "Ice water",
    }

    def setUp(self):
        import copy
        import tempfile
        base = Config.load()
        data = copy.deepcopy(base.data)
        # No shipped recipe derives water from flour yet, so the gate is
        # exercised against a recipe that does.
        data["products"] = data["products"] + [copy.deepcopy(self.GATED)]
        self.cfg = Config(data)
        self.dir = tempfile.mkdtemp()
        self.daily = DailyRatio(os.path.join(self.dir, "daily.json"), self.cfg)

    # -- recipe wiring -----------------------------------------------------

    def test_recipes_naming_flour_and_water_are_gated(self):
        self.assertTrue(self.cfg.water_gated("gated_test"))
        self.assertEqual(self.cfg.flour_of("gated_test"), "Binder (starch)")
        self.assertEqual(self.cfg.water_of("gated_test"), "Ice water")

    def test_nominal_ratio_comes_from_the_recipe_percentages(self):
        # 10 % water over 18 % binder.
        self.assertAlmostEqual(self.cfg.nominal_water_ratio("gated_test"),
                               10 / 18, places=6)

    def test_shipped_recipes_validate(self):
        self.assertEqual(Config.load().validate_products(), [])

    def test_nothing_shipped_is_water_gated_yet(self):
        # Teriyaki and the vinegar bath use no flour, so the daily ratio does
        # not lock the line today. It will once a flour recipe is entered.
        self.assertFalse(Config.load().any_water_gated)

    def test_the_gate_applies_once_a_flour_recipe_exists(self):
        self.assertTrue(self.cfg.any_water_gated)

    def test_half_specified_pair_is_refused(self):
        import copy
        data = copy.deepcopy(self.cfg.data)
        for p in data["products"]:
            if p["id"] == "gated_test":
                del p["water_ingredient"]
        problems = Config(data).validate_products()
        self.assertTrue(any("not the other" in p for p in problems))

    def test_flour_naming_a_missing_ingredient_is_refused(self):
        import copy
        data = copy.deepcopy(self.cfg.data)
        for p in data["products"]:
            if p["id"] == "gated_test":
                p["flour_ingredient"] = "Unobtainium"
        problems = Config(data).validate_products()
        self.assertTrue(any("not in its ingredient list" in p for p in problems))

    # -- the derivation ----------------------------------------------------

    def test_water_target_is_ratio_times_flour(self):
        base, ratio = 3200, 0.55
        flour = base * self.cfg.pct_of("gated_test", "Binder (starch)") / 100
        self.assertAlmostEqual(flour * ratio, 316.8, places=4)

    # -- guards ------------------------------------------------------------

    def test_ratio_inside_bounds_is_accepted(self):
        self.assertIsNone(self.daily.validate(0.55))

    def test_decimal_point_slip_is_rejected(self):
        self.assertIsNotNone(self.daily.validate(5.5))     # 0.55 mistyped
        self.assertIsNotNone(self.daily.validate(0.055))

    def test_non_numeric_is_rejected(self):
        self.assertIsNotNone(self.daily.validate("abc"))

    def test_set_refuses_an_out_of_bounds_ratio(self):
        with self.assertRaises(ValueError):
            self.daily.set(9.9)
        self.assertFalse(self.daily.is_set())

    def test_plausible_but_wrong_ratio_is_flagged_off_nominal(self):
        # Inside the hard bounds, so only the nominal check can catch it.
        self.assertIsNone(self.daily.validate(0.75))
        off = self.daily.off_nominal(0.75, "gated_test")
        self.assertGreater(off, self.cfg.water_off_nominal_warn)

    def test_a_normal_ratio_is_not_flagged(self):
        off = self.daily.off_nominal(0.55, "gated_test")
        self.assertLess(abs(off), self.cfg.water_off_nominal_warn)

    # -- the daily gate ----------------------------------------------------

    def test_starts_unset(self):
        self.assertFalse(self.daily.is_set())
        self.assertIsNone(self.daily.ratio())

    def test_set_then_read_back(self):
        self.daily.set(0.55)
        self.assertTrue(self.daily.is_set())
        self.assertAlmostEqual(self.daily.ratio(), 0.55)

    def test_yesterdays_entry_does_not_count_as_today(self):
        import time as _t
        self.daily.set(0.55)
        tomorrow = _t.localtime(_t.mktime(_t.localtime()) + 24 * 3600)
        self.assertFalse(self.daily.is_set(tomorrow))
        self.assertIsNone(self.daily.ratio(tomorrow))

    def test_yesterdays_entry_is_still_offered_as_the_previous_value(self):
        self.daily.set(0.55)
        self.assertAlmostEqual(self.daily.previous()["ratio"], 0.55)

    def test_day_start_hour_moves_the_rollover_for_a_night_shift(self):
        import copy, time as _t
        data = copy.deepcopy(self.cfg.data)
        data["water"]["day_start_hour"] = 6
        cfg = Config(data)
        night = DailyRatio(os.path.join(self.dir, "night.json"), cfg)
        one_am = _t.struct_time((2026, 8, 25, 1, 0, 0, 0, 237, -1))
        eleven_pm = _t.struct_time((2026, 8, 24, 23, 0, 0, 0, 236, -1))
        # 23:00 and 01:00 either side of midnight are the same production day.
        self.assertEqual(night.production_day(eleven_pm),
                         night.production_day(one_am))
        self.assertEqual(night.production_day(one_am), "2026-08-24")

    def test_plain_calendar_day_splits_at_midnight(self):
        import time as _t
        one_am = _t.struct_time((2026, 8, 25, 1, 0, 0, 0, 237, -1))
        eleven_pm = _t.struct_time((2026, 8, 24, 23, 0, 0, 0, 236, -1))
        self.assertNotEqual(self.daily.production_day(eleven_pm),
                            self.daily.production_day(one_am))

    def test_a_corrupt_daily_file_reads_as_unset_rather_than_crashing(self):
        with open(self.daily.path, "w") as fh:
            fh.write("{not json")
        self.assertFalse(self.daily.is_set())


class TestStationEntryPoint(unittest.TestCase):
    """station.py wiring — the path no unit test reaches into."""

    def run_station(self, *args, timeout=12):
        import subprocess
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.Popen(
            [sys.executable, "-u", os.path.join(root, "station.py"), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        try:
            out, err = proc.communicate(timeout=timeout)
            return proc.returncode, out, err
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            return None, out, err          # still running == started cleanly

    def test_help_works_without_qt(self):
        code, out, err = self.run_station("--help")
        self.assertEqual(code, 0, err)
        self.assertIn("--demo", out)

    def test_demo_starts_and_reports_what_is_usable(self):
        code, out, err = self.run_station("--demo", timeout=8)
        self.assertIsNone(code, f"exited early:\n{err}")
        self.assertIn("recipes with ingredients", out)
        self.assertNotIn("Traceback", err)

    def test_sim_starts(self):
        code, out, err = self.run_station("--sim", "--windowed", timeout=8)
        self.assertIsNone(code, f"exited early:\n{err}")
        self.assertNotIn("Traceback", err)


class TestSerialEndToEnd(unittest.TestCase):
    """Feed real frames through a pty acting as /dev/ttyUSB0."""

    def test_reader_against_virtual_port(self):
        import pty
        import threading
        from scale import serial_reader

        try:
            import serial  # noqa: F401
        except ImportError:
            self.skipTest("pyserial not installed")

        master, slave = pty.openpty()
        slave_name = os.ttyname(slave)
        state = ScaleState()
        stop = threading.Event()
        threading.Thread(target=serial_reader,
                         args=(state, slave_name, 9600, stop), daemon=True).start()
        time.sleep(0.3)

        frames = (
            b"+000.000 kg\r\n" * 2 +
            b"garbage line no unit\r\n" +
            b"+003.500 kg\r\n" * 2 +
            b"+009.650 kg\r\n" * 6      # settling and held
        )
        os.write(master, frames)
        time.sleep(0.6)
        stop.set()

        snap = state.snapshot()
        self.assertTrue(snap["connected"])
        self.assertEqual(snap["grams"], 9650.0)
        self.assertEqual(snap["seq"], 3)          # three distinct values
        self.assertGreaterEqual(snap["counts"]["dup"], 6)
        self.assertEqual(snap["counts"]["bad"], 1)
        os.close(master); os.close(slave)

    def test_reader_survives_a_vanishing_port(self):
        import threading
        from scale import serial_reader

        try:
            import serial  # noqa: F401
        except ImportError:
            self.skipTest("pyserial not installed")

        state = ScaleState()
        stop = threading.Event()
        threading.Thread(target=serial_reader,
                         args=(state, "/dev/does-not-exist", 9600, stop),
                         daemon=True).start()
        time.sleep(0.4)
        snap = state.snapshot()
        self.assertFalse(snap["connected"])
        self.assertIsNotNone(snap["error"])       # reports rather than crashing
        stop.set()


if __name__ == "__main__":
    unittest.main(verbosity=2)
