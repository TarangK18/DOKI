#!/usr/bin/env python3
"""fake_scale.py driven through a real serial connection.

`station.py --sim` bypasses the serial code, so it proves nothing about the
reader. This runs fake_scale.py as a separate process, opens the virtual port
it creates with pyserial, and checks the whole path — frames, parsing,
de-duplication, staleness, recovery — behaves as it will on the bench.

Run: python3 tests/test_fake_scale.py
"""

import os
import re
import subprocess
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scale import STABILITY_WINDOW_S, ScaleState, serial_reader  # noqa: E402


class TestFakeScale(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import serial  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("pyserial not installed")
        if not hasattr(os, "openpty"):
            raise unittest.SkipTest("no pty support on this platform")

        cls.proc = subprocess.Popen(
            [sys.executable, "-u", os.path.join(ROOT, "fake_scale.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)

        cls.port = None
        deadline = time.time() + 10
        while time.time() < deadline and cls.port is None:
            line = cls.proc.stdout.readline()
            if not line:
                break
            # Anchored: the usage text inside the docstring mentions a port too.
            m = re.match(r"^virtual port: (\S+)", line)
            if m:
                cls.port = m.group(1)
        if not cls.port:
            cls.proc.kill()
            raise unittest.SkipTest("fake_scale.py did not report a port")

        cls.state = ScaleState()
        cls.stop = threading.Event()
        threading.Thread(target=serial_reader,
                         args=(cls.state, cls.port, 9600, cls.stop),
                         daemon=True).start()
        time.sleep(1.2)

    @classmethod
    def tearDownClass(cls):
        cls.stop.set()
        try:
            cls.proc.stdin.write("quit\n")
            cls.proc.stdin.flush()
            cls.proc.wait(timeout=5)
        except Exception:
            cls.proc.kill()

    # -- helpers -----------------------------------------------------------

    @classmethod
    def send(cls, cmd, wait=1.2):
        cls.proc.stdin.write(cmd + "\n")
        cls.proc.stdin.flush()
        time.sleep(wait)

    @classmethod
    def settle(cls, timeout=6):
        """Wait for the reading to actually stop moving, rather than guessing."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cls.state.snapshot()["stable"]:
                return True
            time.sleep(0.1)
        return False

    # -- the path ----------------------------------------------------------

    def test_01_port_opened_and_frames_flow(self):
        snap = self.state.snapshot()
        self.assertTrue(snap["connected"], snap["error"])
        self.assertGreater(snap["counts"]["rx"], 0)

    def test_02_weight_set_arrives_over_the_wire(self):
        self.send("3200")
        self.assertAlmostEqual(self.state.snapshot()["grams"], 3200, delta=10)

    def test_03_a_settled_reading_reads_stable(self):
        self.send("noise 0")
        self.send("3200")
        self.assertTrue(self.settle(),
                        "a steady scale should read stable within the "
                        f"{STABILITY_WINDOW_S}s window")
        self.send("noise 1.5")   # the default dither for a 1 g scale

    def test_04_increment_arrives(self):
        self.send("3200")
        self.send("+50")
        self.assertAlmostEqual(self.state.snapshot()["grams"], 3250, delta=10)

    def test_05_frames_are_doubled_and_deduplicated(self):
        # The real scale sends every reading twice; so does this one.
        self.assertGreater(self.state.snapshot()["counts"]["dup"], 0)

    def test_06_no_malformed_frames(self):
        self.assertEqual(self.state.snapshot()["counts"]["bad"], 0)

    def test_07_lifting_the_tub_reads_as_empty(self):
        self.send("3200")
        self.send("lift")
        self.assertLessEqual(self.state.snapshot()["grams"], 10)
        self.send("lift")

    def test_08_stopped_frames_go_stale_and_hide_the_number(self):
        self.send("3200")
        self.send("drop", wait=2.6)
        snap = self.state.snapshot()
        self.assertFalse(snap["fresh"])
        self.assertIsNone(snap["grams"])          # never a confident stale value
        self.assertAlmostEqual(snap["last_grams"], 3200, delta=10)

    def test_09_resumed_frames_come_back_live(self):
        self.send("resume")
        snap = self.state.snapshot()
        self.assertTrue(snap["fresh"])
        self.assertAlmostEqual(snap["grams"], 3200, delta=10)

    def test_10_pouring_moves_the_reading_and_is_not_stable(self):
        self.send("zero")
        self.send("pour", wait=1.5)
        moving = self.state.snapshot()
        self.send("", wait=1.8)                   # blank line stops the pour
        settled = self.state.snapshot()
        self.assertGreater(moving["grams"], 0)
        self.assertFalse(moving["stable"], "a scale being poured into is not stable")
        self.assertTrue(self.settle(), "it should settle once pouring stops")
        self.assertGreaterEqual(settled["grams"], moving["grams"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
