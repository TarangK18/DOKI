#!/usr/bin/env python3
"""
DOKI weighing station — entry point.

  python3 station.py                    # real scale on /dev/ttyUSB0, fullscreen
  python3 station.py --sim              # simulated scale, on-screen +/- buttons
  python3 station.py --windowed         # windowed, for development
  python3 station.py --port /dev/ttyUSB1

One process: a reader thread owns the serial port, the Qt panel polls it.
No server, no browser, no network.
"""

import argparse
import os
import sys

from scale import BatchLog, Config, DailyRatio, ScaleState, SimScale, start_reader

HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv=None):
    ap = argparse.ArgumentParser(description="DOKI weighing station")
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--sim", action="store_true", help="simulated scale, no hardware")
    ap.add_argument("--demo", action="store_true",
                    help="--sim, windowed, today's water ratio pre-set, and "
                         "scratch log files — for trying the flow end to end "
                         "with no hardware and nothing to clean up afterwards")
    ap.add_argument("--windowed", action="store_true", help="do not go fullscreen")
    ap.add_argument("--recipes", default=os.path.join(HERE, "recipes.json"))
    ap.add_argument("--batch-log", default=os.path.join(HERE, "batches.jsonl"))
    ap.add_argument("--daily", default=os.path.join(HERE, "daily.json"))
    args = ap.parse_args(argv)

    if args.demo:
        # A demo should not touch the real batch log, and should not stop at
        # the water gate before the operator has seen anything.
        import tempfile
        args.sim = args.windowed = True
        scratch = os.path.join(tempfile.gettempdir(), "doki-demo")
        os.makedirs(scratch, exist_ok=True)
        args.batch_log = os.path.join(scratch, "batches.jsonl")
        args.daily = os.path.join(scratch, "daily.json")

    cfg = Config.load(args.recipes)
    state = ScaleState()
    batches = BatchLog(args.batch_log)
    daily = DailyRatio(args.daily, cfg)

    problems = cfg.validate_products()
    if problems:
        print("recipes.json is not usable:")
        for p in problems:
            print(f"  {p}")
        return 2
    if args.demo and not daily.is_set():
        daily.set(round(cfg.nominal_water_ratio("chips_masala") or 0.55, 2))
        print(f"demo: today's water ratio pre-set to {daily.ratio()}")
        print(f"demo: logs in {os.path.dirname(args.batch_log)}")

    sim = SimScale(division_g=cfg.division_g) if args.sim else None
    _thread, stop = start_reader(state, port=args.port, baud=args.baud, sim=sim)

    # Imported after the arg parse so --help works on a machine with no Qt.
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication
    from panel import Panel

    app = QApplication(sys.argv[:1])
    app.setApplicationName("MINCECRAFT Weighing Station")

    window = Panel(state, cfg, batches, daily, sim=sim)
    if args.windowed:
        window.resize(1024, 600)
        window.show()
    else:
        window.setCursor(Qt.BlankCursor)
        window.showFullScreen()

    source = "SIMULATED scale" if sim else f"{args.port} @ {args.baud} 8N1"
    print(f"weighing station: {source}")

    try:
        return app.exec_()
    finally:
        stop.set()


if __name__ == "__main__":
    sys.exit(main())
