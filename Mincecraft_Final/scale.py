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
    """Stand-in for the hardware: same frame rate, same duplicate behaviour.

    Holds a "true" weight; the reader quantises it to the scale's division and
    adds noise, so the panel sees the same dither a real scale produces.
    """

    def __init__(self, division_g=5.0):
        self.true_g = 0.0
        self.division_g = division_g
        self.lifted_g = None        # weight set aside while the tub is off
        self.lock = threading.Lock()

    def add(self, grams):
        with self.lock:
            if self.lifted_g is not None:
                return              # nothing to pour into; the tub is off
            self.true_g = max(0.0, self.true_g + grams)

    def set(self, grams):
        with self.lock:
            if self.lifted_g is None:
                self.true_g = max(0.0, float(grams))

    def zero(self):
        with self.lock:
            self.true_g = 0.0
            self.lifted_g = None

    def lift(self):
        """Take the container off the scale, or put it back — the drop alarm
        is one of the things worth being able to rehearse."""
        with self.lock:
            if self.lifted_g is None:
                self.lifted_g, self.true_g = self.true_g, 0.0
            else:
                self.true_g, self.lifted_g = self.lifted_g, None
            return self.lifted_g is not None

    @property
    def is_lifted(self):
        with self.lock:
            return self.lifted_g is not None

    def reading(self):
        with self.lock:
            return self.true_g


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

class DailyRatio:
    """Today's water ÷ flour ratio, set by a supervisor.

    How much water a batch needs depends on the flour in use that day, so it
    cannot live in the recipe. The supervisor sets it once per production day
    and the station will not start a batch without it.
    """

    def __init__(self, path, cfg):
        self.path = path
        self.cfg = cfg
        self.lock = threading.Lock()

    # -- what counts as "today" -------------------------------------------

    def production_day(self, now=None):
        """The date this moment belongs to.

        `day_start_hour` exists so a night shift is not locked out mid-shift:
        with it set to 6, everything before 06:00 still belongs to the previous
        day. Default 0 gives the plain calendar day.
        """
        now = now or time.localtime()
        if isinstance(now, float):
            now = time.localtime(now)
        stamp = time.mktime(now) - self.cfg.day_start_hour * 3600
        return time.strftime("%Y-%m-%d", time.localtime(stamp))

    # -- read / write ------------------------------------------------------

    def _read(self):
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}

    def current(self, now=None):
        """Today's entry, or None if today has not been set yet."""
        with self.lock:
            data = self._read()
        if data.get("date") == self.production_day(now):
            return data
        return None

    def previous(self):
        """The last entry on file, whatever day it belongs to — shown to the
        supervisor for reference when setting today's."""
        with self.lock:
            return self._read() or None

    def ratio(self, now=None):
        entry = self.current(now)
        return entry["ratio"] if entry else None

    def is_set(self, now=None):
        return self.current(now) is not None

    def set(self, ratio, now=None):
        """Record today's ratio. Raises ValueError if out of bounds."""
        problem = self.validate(ratio)
        if problem:
            raise ValueError(problem)
        entry = {
            "date": self.production_day(now),
            "ratio": round(float(ratio), 4),
            "set_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        with self.lock:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(entry, fh, indent=2)
                fh.write("\n")
        return entry

    # -- guards ------------------------------------------------------------

    def validate(self, ratio):
        """Why this ratio is not acceptable, or None. The hard bound catches
        the decimal-point slip — 5.5 typed for 0.55."""
        try:
            ratio = float(ratio)
        except (TypeError, ValueError):
            return "not a number"
        lo, hi = self.cfg.water_ratio_min, self.cfg.water_ratio_max
        if not (lo <= ratio <= hi):
            return f"outside the permitted range {lo:g} to {hi:g}"
        return None

    def off_nominal(self, ratio, product_id):
        """How far this ratio puts the water from the recipe's own figure, as a
        fraction, or None if the recipe has no nominal to compare against.

        Catches the plausible-but-wrong entry the hard bound cannot: 0.75 where
        0.55 was meant."""
        nominal = self.cfg.nominal_water_ratio(product_id)
        if not nominal:
            return None
        return (ratio - nominal) / nominal


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

MAIN, SMALL = "main", "small"


class ScaleSpec:
    """One physical scale."""

    def __init__(self, key, data):
        self.key = key
        self.name = data.get("name", key)
        self.division_g = float(data["division_g"])
        self.capacity_g = float(data.get("capacity_g", 0))
        # What can actually be put on it, once the container's tare is allowed for.
        self.usable_g = float(data.get("usable_g", self.capacity_g))
        self.port = data.get("port")
        self.connected = bool(data.get("connected", False))

    def can_resolve(self, target, percent):
        """True if this scale can hold `percent` on `target`.

        Two divisions is the least that can be called a measurement: one
        division is the quantisation step, and a reading dithers across two
        adjacent steps. If the tolerance band is narrower than that, the
        reading physically cannot be held inside it.
        """
        return percent * target >= 2 * self.division_g

    def __repr__(self):
        return f"<{self.name} {self.capacity_g:.0f}g x {self.division_g}g>"


class Config:
    """recipes.json, plus the handful of derived helpers the panel needs."""

    def __init__(self, data):
        self.data = data
        self.min_base_g = data["min_base_g"]
        self.drop_alarm_g = data["drop_alarm_g"]
        self.rebalance_limit = data["rebalance_limit"]

        scales = data.get("scales") or {}
        self.main = ScaleSpec(MAIN, scales.get(MAIN) or
                              {"division_g": data.get("scale_division_g", 5)})
        self.small = ScaleSpec(SMALL, scales[SMALL]) if SMALL in scales else None
        self.division_g = self.main.division_g   # kept: "the station's scale"
        self._crossover = scales.get("crossover_g")
        self.pin = str(data["pin"])
        self.products = data["products"]
        self._tol_pct = data["tolerance"]["percent"]
        self._tol_floor = data["tolerance"]["floor_g"]

        water = data.get("water", {})
        self.water_ratio_min = float(water.get("ratio_min", 0.2))
        self.water_ratio_max = float(water.get("ratio_max", 1.5))
        self.water_off_nominal_warn = float(water.get("off_nominal_warn", 0.25))
        self.day_start_hour = int(water.get("day_start_hour", 0))

    @classmethod
    def load(cls, path=None):
        path = path or os.path.join(HERE, "recipes.json")
        with open(path, "r", encoding="utf-8") as fh:
            return cls(json.load(fh))

    def tol_of(self, target):
        """Tolerance for a target: the recipe's percentage, floored at two
        divisions of whichever scale weighs it. A band finer than the scale
        can resolve is one the reading physically cannot land inside."""
        return max(self.floor_for(target), self._tol_pct * target)

    # -- which scale weighs what ------------------------------------------

    @property
    def derived_crossover_g(self):
        """Where the main scale stops being able to hold the recipe's percentage.

        Derived, not chosen: exactly the target at which two divisions of the
        main scale stop fitting inside `percent`. With a 5 g division and 2 %,
        that is 500 g. The old global `floor_g` existed only because there was
        nowhere else for smaller ingredients to go.
        """
        return 2 * self.main.division_g / self._tol_pct

    @property
    def main_min_target_g(self):
        """The crossover actually in force.

        Physics sets the derived value; operations may want a lower one — it
        is easier to pour 320 g of water into the tub on the floor scale than
        to carry it from the bench, even though the floor scale can only hold
        3 % at that size rather than 2 %. Setting `scales.crossover_g` below
        the derived value is allowed and honest: those ingredients then show a
        degraded tolerance at recipe review instead of a silent one.
        """
        return self._crossover if self._crossover is not None else self.derived_crossover_g

    def assign_scale(self, target):
        """(scale_key, reason) for a target, or (None, why) if neither will do."""
        if target <= 0:
            return None, "target is zero"
        if target >= self.main_min_target_g:
            if target > self.main.usable_g:
                return None, (f"{target:.0f} g exceeds the {self.main.name}'s "
                              f"{self.main.usable_g:.0f} g usable capacity")
            return MAIN, f"{self.main.name} holds {self._tol_pct:.0%} at this size"
        if self.small is None:
            return None, (f"{target:.1f} g is below the {self.main.name}'s "
                          f"{self.main_min_target_g:.0f} g floor and there is no "
                          f"second scale configured")
        if target > self.small.usable_g:
            return None, (f"{target:.1f} g is above the {self.small.name}'s "
                          f"{self.small.usable_g:.0f} g usable capacity but below "
                          f"the {self.main.name}'s {self.main_min_target_g:.0f} g "
                          f"floor — neither scale can weigh it")
        if target < 2 * self.small.division_g:
            return None, (f"{target:.2f} g is under two divisions of the "
                          f"{self.small.name} ({2 * self.small.division_g:g} g)")
        return SMALL, f"too small for the {self.main.name}; use the {self.small.name}"

    def floor_for(self, target):
        """Tolerance floor imposed by whichever scale weighs this target.

        Two divisions of that scale. A single global floor was the wrong shape:
        it forced 58 g of salt to a ±10 g window because the floor scale was
        the only option. Routed to the bench scale the same salt is held to
        ±1.2 g, which is what the recipe actually asked for."""
        key = self.scale_for(target)
        if key is None:
            return self._tol_floor
        return 2 * self.spec(key).division_g

    def tolerance_degraded(self, target):
        """The scale's floor is coarser than the recipe's percentage, so this
        ingredient is held to a looser band than intended. Allowed, but the
        operator should be told rather than left to assume otherwise."""
        return self.floor_for(target) > self._tol_pct * target

    def scale_for(self, target):
        return self.assign_scale(target)[0]

    def spec(self, key):
        return self.main if key == MAIN else self.small

    @property
    def dead_zone(self):
        """(low, high) targets no scale can weigh, or None.

        Exists when the small scale's usable capacity stops below the main
        scale's floor. Worth surfacing at load time rather than discovering it
        mid-batch.
        """
        if self.small is None:
            return None
        low, high = self.small.usable_g, self.main_min_target_g
        return (low, high) if low < high else None

    def unweighable(self, target):
        """Why this target cannot be weighed at all, or None if some scale can.

        The dangerous case this exists to catch: a tolerance as wide as the
        target means zero is inside tolerance, so an operator who adds nothing
        passes the step. The station must refuse the recipe, not wave it
        through.
        """
        return self.assign_scale(target)[1] if self.scale_for(target) is None else None

    def unweighable_steps(self, steps):
        """[(name, target, reason)] for every step no scale can enforce."""
        out = []
        for s in steps:
            why = self.unweighable(s.target)
            if why:
                out.append((s.name, s.target, why))
        return out

    def witness_tolerance(self, typed_g):
        """How far the main scale's observed delta may differ from a value the
        operator read off the small scale, before it is worth questioning.

        The main scale quantises to its own division and dithers across two
        steps, so two readings can disagree by a division even when nothing is
        wrong. Anything tighter than that would cry wolf.
        """
        return max(2 * self.main.division_g, 0.1 * typed_g)

    def can_witness(self, target):
        """Whether the main scale can meaningfully confirm an addition of this
        size at all. Below two of its divisions it simply cannot see it, and
        saying so beats implying a check that never happened."""
        return target >= 2 * self.main.division_g

    def product(self, product_id):
        return next(p for p in self.products if p["id"] == product_id)

    def product_name(self, product_id):
        return self.product(product_id)["name"]

    def meat_of(self, product_id):
        """Which animal this product is made from.

        Asked of nobody: the product implies it. None where it has not been
        decided, in which case the batch records it as unspecified rather than
        guessing.
        """
        return self.product(product_id).get("meat")

    def is_draft(self, product_id):
        """A product with no ingredients yet. Shown, but cannot be started —
        an empty recipe reaching the floor is worse than a greyed-out button."""
        p = self.product(product_id)
        return bool(p.get("draft")) or not p.get("ingredients")

    def min_base_for(self, product_id):
        """Smallest batch of meat this product can actually be made in.

        The smallest ingredient sets it: bhut jholokia at 0.025 % needs an
        800 g batch before it reaches two divisions of the bench scale, so a
        500 g Teriyaki batch is not a tolerance problem, it is unmakeable.
        Better to say so at the scale than to fail at recipe review.
        """
        floor = self.min_base_g
        if self.small is None:
            return floor
        for _, pct in self.product(product_id)["ingredients"]:
            if pct > 0:
                floor = max(floor, 2 * self.small.division_g / (pct / 100.0))
        return floor

    @property
    def any_water_gated(self):
        """Whether the daily ratio locks production at all.

        If no recipe derives its water from flour, holding the line for a
        ratio nobody uses is friction with no safety behind it.
        """
        return any(self.water_gated(p["id"]) for p in self.products)

    # -- water, which the recipe cannot fix ---------------------------------

    def water_gated(self, product_id):
        """True if this product's water comes from the daily ratio.

        A recipe that names both a flour and a water ingredient is gated. One
        that names neither runs on its recipe percentages as before.
        """
        p = self.product(product_id)
        return bool(p.get("flour_ingredient") and p.get("water_ingredient"))

    def flour_of(self, product_id):
        return self.product(product_id).get("flour_ingredient")

    def water_of(self, product_id):
        return self.product(product_id).get("water_ingredient")

    def pct_of(self, product_id, ingredient):
        for name, pct in self.product(product_id)["ingredients"]:
            if name == ingredient:
                return pct
        return None

    def nominal_water_ratio(self, product_id):
        """The water ÷ flour ratio implied by the recipe's own percentages.

        Only a reference point: it is what the ratio usually comes out at, so a
        supervisor's entry can be sanity-checked against it.
        """
        if not self.water_gated(product_id):
            return None
        flour = self.pct_of(product_id, self.flour_of(product_id))
        water = self.pct_of(product_id, self.water_of(product_id))
        if not flour or water is None:
            return None
        return water / flour

    def validate_products(self):
        """Recipe problems worth refusing at load rather than mid-batch."""
        problems = []
        for p in self.products:
            names = {n for n, _ in p["ingredients"]}
            flour, water = p.get("flour_ingredient"), p.get("water_ingredient")
            if bool(flour) != bool(water):
                problems.append(
                    f"{p['id']}: names {'a flour' if flour else 'a water'} "
                    f"ingredient but not the other — water cannot be derived")
            for role, ing in (("flour", flour), ("water", water)):
                if ing and ing not in names:
                    problems.append(
                        f"{p['id']}: {role} ingredient '{ing}' is not in its "
                        f"ingredient list")
        return problems


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
