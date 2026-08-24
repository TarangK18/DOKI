#!/usr/bin/env python3
"""
DOKI weighing station — the scale, with no GUI dependency.

Everything in this module runs and is tested without a display: frame parsing,
de-duplication, stability, staleness, the serial reader and its reconnect
loop, the batch log and the recipe config.

Deliberately Qt-free. The panel imports this; this imports nothing of the
panel. Same split as doki_probe.ino, where the cook state machine is plain
C++ so the test harness can lift it out verbatim.
"""

import json
import os
import re
import threading
import time
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- constants

# Frame the scale streams: b'+009.650 kg\r\n'
FRAME_RE = re.compile(rb"([+-]\d{3}\.\d{3})\s*kg")

STALE_S = 2.0             # no fresh frame for this long -> not trustworthy
STABILITY_WINDOW_S = 1.5  # window used to judge whether the reading settled
STABILITY_MIN_SAMPLES = 8
STABLE_BAND_G = 5.0       # spread allowed in the window; scale division is 5 g
RECONNECT_S = 2.0
LOG_MAX = 3600            # rolling in-memory log of accepted readings


# ------------------------------------------------------------------- state

class ScaleState:
    """Everything the panel needs to know about the scale, behind one lock.

    De-duplication and stability deliberately use different inputs: duplicates
    are dropped from what we *emit and log*, but stability is judged on the
    *raw* stream, because a run of identical frames is exactly what a settled
    scale looks like and throwing it away would hide that.

    There is no pub/sub here. The panel polls snapshot() on a timer, and
    _snapshot() computes staleness at call time, so a scale that has gone
    silent is detected by the reader of the state rather than announced by it.
    """

    def __init__(self):
        self.lock = threading.Lock()

        self.connected = False        # serial port is open
        self.port_error = None
        self.grams = None             # last accepted reading
        self.last_rx = 0.0            # last frame of any kind, incl. duplicates
        self.last_change = 0.0
        self.stable = False
        self.seq = 0                  # bumps on every accepted (distinct) reading

        self.rx_count = 0             # frames parsed
        self.dup_count = 0            # frames dropped as consecutive repeats
        self.bad_count = 0            # lines that did not parse

        self._raw = deque()           # (ts, grams) inside the stability window
        self.log = deque(maxlen=LOG_MAX)   # (ts, grams) accepted readings

    # -- ingest ------------------------------------------------------------

    def on_frame(self, grams, now=None):
        """A frame parsed cleanly. Returns True if it was a new distinct value."""
        now = now if now is not None else time.time()
        with self.lock:
            self.rx_count += 1
            self.last_rx = now

            self._raw.append((now, grams))
            cutoff = now - STABILITY_WINDOW_S
            while self._raw and self._raw[0][0] < cutoff:
                self._raw.popleft()
            self.stable = self._compute_stable()

            changed = (self.grams is None) or (abs(grams - self.grams) > 1e-9)
            if changed:
                self.grams = grams
                self.last_change = now
                self.seq += 1
                self.log.append((now, grams))
            else:
                self.dup_count += 1
        return changed

    def on_bad_line(self):
        with self.lock:
            self.bad_count += 1

    def set_connected(self, connected, error=None):
        with self.lock:
            self.connected = connected
            self.port_error = error
            if not connected:
                self._raw.clear()
                self.stable = False

    def _compute_stable(self):
        if len(self._raw) < STABILITY_MIN_SAMPLES:
            return False
        values = [g for _, g in self._raw]
        return (max(values) - min(values)) <= STABLE_BAND_G

    # -- read out ----------------------------------------------------------

    def _snapshot(self, now):
        age = (now - self.last_rx) if self.last_rx else None
        fresh = self.connected and age is not None and age <= STALE_S
        return {
            "connected": self.connected,
            "fresh": fresh,
            "stable": bool(self.stable and fresh),
            "grams": self.grams if fresh else None,
            "last_grams": self.grams,
            "age_ms": int(age * 1000) if age is not None else None,
            "seq": self.seq,
            "counts": {"rx": self.rx_count, "dup": self.dup_count, "bad": self.bad_count},
            "error": self.port_error,
            "t": now,
        }

    def snapshot(self):
        with self.lock:
            return self._snapshot(time.time())

    def recent_log(self, limit=600):
        with self.lock:
            items = list(self.log)[-limit:]
        return [{"t": round(t, 3), "g": g} for t, g in items]


# ------------------------------------------------------------------ reader

def parse_frame(line):
    """bytes -> grams, or None if the line is not a valid reading."""
    m = FRAME_RE.search(line)
    if not m:
        return None
    return round(float(m.group(1)) * 1000.0, 1)


def serial_reader(state, port, baud, stop):
    """Read the scale forever, reconnecting on its own if the port goes away."""
    import serial   # imported here so --sim works without pyserial installed

    while not stop.is_set():
        ser = None
        try:
            ser = serial.Serial(port, baudrate=baud, bytesize=8,
                                parity="N", stopbits=1, timeout=1)
            state.set_connected(True)
            while not stop.is_set():
                line = ser.readline()
                if not line:
                    continue          # timeout; staleness is judged from last_rx
                grams = parse_frame(line)
                if grams is None:
                    state.on_bad_line()
                else:
                    state.on_frame(grams)
        except Exception as exc:      # port vanished, unplugged, permissions...
            state.set_connected(False, str(exc))
            if not stop.is_set():
                time.sleep(RECONNECT_S)
        finally:
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass


class SimScale:
    """Stand-in for the hardware: same frame rate, same duplicate behaviour."""

    def __init__(self, division_g=5.0):
        self.true_g = 0.0
        self.division_g = division_g
        self.lock = threading.Lock()

    def add(self, grams):
        with self.lock:
            self.true_g = max(0.0, self.true_g + grams)

    def zero(self):
        with self.lock:
            self.true_g = 0.0


def sim_reader(state, sim, stop):
    import random
    state.set_connected(True)
    while not stop.is_set():
        with sim.lock:
            true_g, div = sim.true_g, sim.division_g
        noise = (random.random() - 0.5) * 4.0 if true_g > 0 else 0.0
        grams = max(0.0, round((true_g + noise) / div) * div)
        # The real scale sends each reading twice; mirror that so the
        # de-duplication path is exercised in simulation too.
        state.on_frame(grams)
        state.on_frame(grams)
        time.sleep(0.2)


# ----------------------------------------------------------------- batches

class BatchLog:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def append(self, record):
        record = dict(record)
        record["logged_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with self.lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        return record

    def recent(self, limit=50):
        if not os.path.exists(self.path):
            return []
        with self.lock:
            with open(self.path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out


# ------------------------------------------------------------------ config

class Config:
    """recipes.json, plus the handful of derived helpers the panel needs."""

    def __init__(self, data):
        self.data = data
        self.min_base_g = data["min_base_g"]
        self.drop_alarm_g = data["drop_alarm_g"]
        self.rebalance_limit = data["rebalance_limit"]
        self.division_g = data.get("scale_division_g", 5)
        self.pin = str(data["pin"])
        self.bases = data["bases"]
        self.products = data["products"]
        self._tol_pct = data["tolerance"]["percent"]
        self._tol_floor = data["tolerance"]["floor_g"]

    @classmethod
    def load(cls, path=None):
        path = path or os.path.join(HERE, "recipes.json")
        with open(path, "r", encoding="utf-8") as fh:
            return cls(json.load(fh))

    def tol_of(self, target):
        """Tolerance for a target. The floor must never be finer than the
        scale can resolve, or the reading physically cannot land inside it."""
        return max(self._tol_floor, self._tol_pct * target)

    def base(self, base_id):
        return next(b for b in self.bases if b["id"] == base_id)

    def base_name(self, base_id):
        return self.base(base_id)["name"]

    def base_icon(self, base_id):
        return self.base(base_id)["icon"]

    def product(self, product_id):
        return next(p for p in self.products if p["id"] == product_id)

    def product_name(self, product_id):
        return self.product(product_id)["name"]

    def products_for(self, base_id):
        return [p for p in self.products if base_id in p["bases"]]


# ------------------------------------------------------------------ format

def fmt(grams):
    return f"{grams/1000:.2f} kg" if grams >= 1000 else f"{round(grams)} g"


def fmt_g(grams):
    return str(round(grams))


# ------------------------------------------------------------------- start

def start_reader(state, port="/dev/ttyUSB0", baud=9600, sim=None):
    """Launch the reader thread. Returns (thread, stop_event)."""
    stop = threading.Event()
    if sim is not None:
        target, args = sim_reader, (state, sim, stop)
    else:
        target, args = serial_reader, (state, port, baud, stop)
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()
    return thread, stop
