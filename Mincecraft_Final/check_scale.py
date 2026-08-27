#!/usr/bin/env python3
"""Measure the scale, rather than assuming things about it.

  python3 check_scale.py                       # /dev/ttyUSB0, 20 seconds
  python3 check_scale.py --port /dev/ttyUSB1 --seconds 30

Everything the station does rests on three facts about the hardware: the frame
format, the smallest step it reads, and how much a settled reading dithers.
All three have been taken on trust so far. This reads the port for a while and
reports what is actually true, then says what to put in recipes.json.

Run it twice: once with **nothing on the scale**, and once with a **steady
known weight** on it. The first tells you the zero behaviour, the second tells
you the division and the dither, which are the numbers that matter.
"""

import argparse
import collections
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from scale import FRAME_RE, parse_frame          # noqa: E402


def window_spreads(stamped, window_s=1.5):
    """Spread within each rolling window.

    The spread across a whole run only measures dither if nothing was touched.
    Per-window spreads survive someone changing the weight halfway through:
    the quiet windows still show what a settled reading does.
    """
    out = []
    for i, (t0, _) in enumerate(stamped):
        chunk = [v for t, v in stamped[i:] if t - t0 <= window_s]
        if len(chunk) >= 5:
            out.append(max(chunk) - min(chunk))
    return sorted(out)


def gcd_of(values):
    """Smallest step that divides every gap between readings — the division."""
    if len(values) < 2:
        return None
    # Work in tenths of a gram so a 0.1 g scale is not rounded away.
    ticks = sorted({int(round(v * 10)) for v in values})
    gaps = [b - a for a, b in zip(ticks, ticks[1:]) if b != a]
    if not gaps:
        return None
    g = 0
    for gap in gaps:
        g = math.gcd(g, gap)
    return g / 10.0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--seconds", type=float, default=20.0)
    args = ap.parse_args(argv)

    try:
        import serial
    except ImportError:
        print("pyserial is not installed:  sudo apt install python3-serial")
        return 2

    print(f"listening to {args.port} at {args.baud} 8N1 for {args.seconds:.0f} s")
    print("leave the scale alone while this runs\n")

    try:
        ser = serial.Serial(args.port, baudrate=args.baud, bytesize=8,
                            parity="N", stopbits=1, timeout=1)
    except Exception as exc:
        print(f"could not open the port: {exc}")
        print("\n  ls -l /dev/ttyUSB*        does it exist?")
        print("  groups                    are you in 'dialout'?")
        print("  dmesg | grep -i tty       did the converter enumerate?")
        return 1

    raw, values, stamped, unparsed = [], [], [], []
    started = time.time()
    last = None
    dups = 0
    with ser:
        while time.time() - started < args.seconds:
            line = ser.readline()
            if not line:
                continue
            raw.append((time.time(), line))
            grams = parse_frame(line)
            if grams is None:
                unparsed.append(line)
                continue
            values.append(grams)
            stamped.append((time.time(), grams))
            if last is not None and abs(grams - last) < 1e-9:
                dups += 1
            last = grams
    elapsed = time.time() - started

    if not raw:
        print("nothing arrived at all.")
        print("  · wrong port, or the scale is not set to stream continuously")
        print("  · try another baud rate: 1200, 2400, 4800, 19200")
        return 1

    print(f"{len(raw)} lines in {elapsed:.1f} s "
          f"= {len(raw)/elapsed:.1f} per second")
    print(f"{len(values)} parsed, {len(unparsed)} did not match the expected frame")

    sample = raw[len(raw)//2][1]
    print(f"\nframe as received:  {sample!r}")
    if FRAME_RE.search(sample):
        print("  matches the +NNN.NNN kg pattern the station expects")
    if unparsed:
        odd = collections.Counter(unparsed).most_common(3)
        print("\n  lines that did NOT parse — these would be counted as bad:")
        for line, n in odd:
            print(f"    x{n:<4} {line!r}")
        print("  If these are real readings in another format, send me one and")
        print("  I will widen the parser.")

    if not values:
        return 1

    distinct = sorted(set(values))
    div = gcd_of(values)
    spread = max(values) - min(values)
    print(f"\nreadings: {min(values):.1f} to {max(values):.1f} g, "
          f"{len(distinct)} distinct values")
    print(f"duplicate frames: {dups} of {len(values)} "
          f"({dups/len(values):.0%}) — the station de-duplicates these")

    print("\n--- what this tells you ---")
    if div:
        print(f"smallest step between readings: {div:g} g")
        print(f"  -> set scales.main.division_g to {div:g} in recipes.json")
    else:
        print("the reading never changed, so the step cannot be measured here.")
        print("  -> run again with a weight on the scale, or nudge it")

    spreads = window_spreads(stamped)
    if spreads:
        quiet = spreads[len(spreads) // 4]        # a quarter of the way up
        typical = spreads[len(spreads) // 2]
        print(f"dither in a settled 1.5 s window: {quiet:g} g "
              f"(median across all windows {typical:g} g)")
        band = max(2 * (div or 1), quiet)
        print(f"  -> the stability band should be about {band:g} g; "
              f"station.py uses 2 x division = {2 * (div or 1):g} g")
        if band > 2 * (div or 1):
            print("     that is wider than two divisions, so raise it or the "
                  "panel will rarely call a reading settled")
        if spread > typical * 3:
            print(f"  (the full range was {spread:.0f} g, so the weight changed "
                  f"during the run — that is fine, the figures above come from "
                  f"the quiet windows)")
    elif spread == 0:
        print("the reading did not move at all — rock steady.")

    if div:
        crossover = 2 * div / 0.02
        print(f"\nwith a 2 % tolerance, this scale can only hold that above "
              f"{crossover:.0f} g.")
        print(f"  anything smaller belongs on the bench scale.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
