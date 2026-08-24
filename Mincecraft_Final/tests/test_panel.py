#!/usr/bin/env python3
"""
Headless walk-through of the Qt panel. Runs under QT_QPA_PLATFORM=offscreen,
drives the real app in --sim mode, and saves a screenshot of every screen.

Run: python3 tests/test_panel.py
"""

import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QDialog, QPushButton  # noqa: E402

from scale import BatchLog, Config, ScaleState, SimScale, start_reader  # noqa: E402
from panel import Panel, PauseDialog, PinDialog  # noqa: E402

SHOTS = os.path.join(ROOT, "screenshots")
LOG = os.path.join(ROOT, "_test_batches.jsonl")

checks = []


def check(label, cond):
    checks.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label)


def pump(app, seconds=0.3):
    """Let the 100 ms panel timer run."""
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def settle(app, state, seconds=8):
    """Wait for the service to declare the reading stable, three polls running."""
    end = time.time() + seconds
    run = 0
    while time.time() < end:
        run = run + 1 if state.snapshot()["stable"] else 0
        if run >= 3:
            pump(app, 0.4)
            return True
        pump(app, 0.1)
    return False


def shot(widget, name):
    widget.grab().save(os.path.join(SHOTS, name))


def click(btn):
    btn.click()


def find_button(widget, text):
    for b in widget.findChildren(QPushButton):
        if b.text().strip().startswith(text):
            return b
    return None


def main():
    os.makedirs(SHOTS, exist_ok=True)
    if os.path.exists(LOG):
        os.remove(LOG)

    cfg = Config.load()
    state = ScaleState()
    batches = BatchLog(LOG)
    sim = SimScale(division_g=cfg.division_g)
    _t, stop = start_reader(state, sim=sim)

    app = QApplication(sys.argv[:1])
    win = Panel(state, cfg, batches, sim=sim)
    win.resize(1024, 600)
    win.show()
    pump(app, 1.0)

    # ---------------------------------------------------------------- home
    check("link indicator shows LIVE", "LIVE" in win.link_lbl.text())
    check("simulation bar rendered", find_button(win, "+3 kg") is not None)
    check("START BATCH armed with a live scale", win.screens["HOME"].start_btn.isEnabled())
    shot(win, "01-home.png")

    # ---------------------------------------------------------------- base
    win.screens["HOME"].start_btn.click()
    pump(app)
    check("base screen offers all five bases",
          len([b for b in win.screens["BASE"].findChildren(QPushButton)
               if b.property("variant") == "primary"]) == 5)
    shot(win, "02-base.png")
    win.pick_base("chicken")
    pump(app)

    # ------------------------------------------------------------- capture
    cap = win.screens["CAPTURE"]
    check("CAPTURE disabled with an empty scale", not cap.cap_btn.isEnabled())
    sim.add(3200)
    check("scale settles after loading the tub", settle(app, state))
    check("CAPTURE enabled once stable above the minimum", cap.cap_btn.isEnabled())
    shot(win, "03-capture.png")
    cap.cap_btn.click()
    pump(app)

    # ------------------------------------------------------------- product
    check("only products valid for chicken are offered",
          win.screens["PRODUCT"].grid.count() == 4)
    shot(win, "04-product.png")
    win.pick_product("chips_masala")
    pump(app)

    # -------------------------------------------------------------- review
    rev = win.screens["REVIEW"]
    check("recipe review lists five ingredients plus a total",
          rev.table.rowCount() == 6)
    check("targets scaled from the captured base",
          rev.table.item(0, 2).text() == f"{round(3200 * 18 / 100)} g")
    shot(win, "05-review.png")
    find_button(rev, "START ADDING").click()
    pump(app, 0.5)

    # --------------------------------------------------- ingredient stepping
    add = win.screens["ADD"]
    targets = [s.target for s in win.st.steps]
    check("five ingredient steps computed", len(targets) == 5)

    shot_under = shot_ok = False
    for i, target in enumerate(targets):
        if win.current != "ADD":
            break
        sim.add(target * 0.5)
        settle(app, state)
        if not shot_under:
            check("under target — panel asks for more", "more" in add.guide.text())
            check("software tare shows only this ingredient",
                  abs(float(add.big.text().split("/")[0]) - target * 0.5) <= 10)
            shot(win, "06-under-target.png")
            shot_under = True
        sim.add(target * 0.5)
        if not shot_ok:
            pump(app, 1.0)
            check("in tolerance — panel counts down to auto-accept",
                  "hold steady" in add.guide.text() or "OK" in add.guide.text())
            shot(win, "07-in-tolerance.png")
            shot_ok = True
        end = time.time() + 15
        while time.time() < end and win.current == "ADD" and win.st.idx == i:
            pump(app, 0.1)
        if i == 0:
            check("auto-advance fired after holding in tolerance",
                  win.st.idx != 0 or win.current == "DONE")

    end = time.time() + 10
    while time.time() < end and win.current != "DONE":
        pump(app, 0.1)
    check("batch reached the DONE screen", win.current == "DONE")
    shot(win, "08-done.png")

    check("every ingredient captured an actual",
          all(s.actual is not None for s in win.st.steps))
    check("all ingredients landed inside tolerance",
          all(abs(s.actual - s.target) <= cfg.tol_of(s.target) for s in win.st.steps))

    rows = batches.recent()
    check("batch written to the log on the Pi", len(rows) == 1)
    check("logged batch carries all five actuals",
          rows and len(rows[0]["steps"]) == 5
          and all(s["actual_g"] is not None for s in rows[0]["steps"]))

    # ------------------------------------------------------------- overlays
    # The pause dialog owns its own timer, so it can be exercised non-modally.
    sim.zero()
    pump(app, 1.5)
    dlg = PauseDialog(win, step_zero=3200)
    dlg.show()
    pump(app, 0.5)
    check("RESUME stays locked while the container is off the scale",
          not dlg.resume_btn.isEnabled())
    shot(dlg, "09-container-removed.png")
    sim.add(3200)
    pump(app, 1.5)
    check("RESUME arms only once the weight is actually back",
          dlg.resume_btn.isEnabled())
    dlg.close()

    # The ADD screen must *raise* that dialog when the weight drops.
    raised = []
    win.st.step_zero = 3200
    win.st.idx = 0
    win.show_screen("ADD")
    win.container_removed = lambda: raised.append(True)
    sim.zero()
    end = time.time() + 4
    while time.time() < end and not raised:
        pump(app, 0.1)
    check("a dropped weight raises the container-removed alarm", bool(raised))

    # Over-target prompt.
    prompted = []
    win.over_target = lambda added: prompted.append(added)
    win.st.step_zero = 0.0
    add.over_prompted = False
    sim.zero(); pump(app, 0.5)
    sim.add(win.st.steps[0].target * 1.5)
    end = time.time() + 6
    while time.time() < end and not prompted:
        pump(app, 0.1)
    check("well over target raises the over-target prompt", bool(prompted))

    # PIN pad.
    pin = PinDialog(win, "test")
    pin.show(); pump(app, 0.2)
    for k in "9999":
        pin.key(k)
    check("PIN entry masks the digits", pin.entry.text() == "••••")
    pin.key("OK")
    check("wrong PIN is rejected and clears",
          pin.entered == "" and pin.result() != QDialog.Accepted)
    shot(pin, "10-pin.png")
    for k in cfg.pin:
        pin.key(k)
    pin.key("OK")
    check("correct PIN is accepted", pin.result() == QDialog.Accepted)

    # ------------------------------------------------------- stale and dead
    stop.set()
    end = time.time() + 6
    while time.time() < end and "LIVE" in win.link_lbl.text():
        pump(app, 0.2)
    win.show_screen("HOME")
    pump(app, 0.5)
    check("a dead feed stops claiming LIVE", "LIVE" not in win.link_lbl.text())
    check("stale reading shows a dash, not the last number",
          win.screens["HOME"].live.text() == "—")
    check("stale reading is dimmed", not win.screens["HOME"].live.live)
    check("START BATCH disarms without a live scale",
          not win.screens["HOME"].start_btn.isEnabled())
    shot(win, "11-no-scale.png")

    # ------------------------------------------------------------- scaling
    win.resize(1920, 1080)
    pump(app, 0.5)
    check("type scales up on a larger monitor", win.scale > 1.4)
    check("layout still fits the larger screen",
          win.centralWidget().sizeHint().height() <= 1080)
    shot(win, "12-1920x1080.png")

    win.close()
    if os.path.exists(LOG):
        os.remove(LOG)

    failed = [c for c, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    print(f"screenshots in {SHOTS}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
