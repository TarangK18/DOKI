#!/usr/bin/env python3
"""Tests for scale.py — no display, no Qt. Run: python3 tests/test_scale.py"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scale import (Config, ScaleState, STABLE_BAND_G, STALE_S,  # noqa: E402
                   parse_frame)


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

    def test_tolerance_floor_is_at_least_two_divisions(self):
        # A floor finer than the scale can resolve is unsatisfiable: the
        # reading physically cannot land inside it.
        self.assertGreaterEqual(self.cfg.tol_of(0), 2 * self.cfg.division_g)

    def test_small_ingredient_tolerance_is_reachable(self):
        salt_target = 3200 * 1.8 / 100      # 1.8 % salt on a 3.2 kg base
        self.assertGreaterEqual(self.cfg.tol_of(salt_target), self.cfg.division_g)

    def test_percentage_takes_over_for_large_targets(self):
        self.assertAlmostEqual(self.cfg.tol_of(10000), 200.0)

    def test_every_product_names_valid_bases(self):
        ids = {b["id"] for b in self.cfg.bases}
        for p in self.cfg.products:
            self.assertTrue(set(p["bases"]) <= ids, p["id"])

    def test_products_for_filters_by_base(self):
        self.assertEqual(len(self.cfg.products_for("chicken")), 4)
        self.assertEqual(len(self.cfg.products_for("fish")), 2)


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
