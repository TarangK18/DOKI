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
from PyQt5.QtWidgets import (QApplication, QDialog, QLabel,  # noqa: E402
                             QPushButton)

from scale import (MAIN, SMALL, BatchLog, Config, DailyRatio,  # noqa: E402
                   ScaleState, SimScale, start_reader)
from panel import Panel, PauseDialog, PinDialog  # noqa: E402

SHOTS = os.path.join(ROOT, "screenshots")
LOG = os.path.join(ROOT, "_test_batches.jsonl")
DAILY = os.path.join(ROOT, "_test_daily.json")

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
    for f in (LOG, DAILY):
        if os.path.exists(f):
            os.remove(f)

    cfg = Config.load()
    state = ScaleState()
    batches = BatchLog(LOG)
    daily = DailyRatio(DAILY, cfg)
    sim = SimScale(division_g=cfg.division_g)
    _t, stop = start_reader(state, sim=sim)

    app = QApplication(sys.argv[:1])
    win = Panel(state, cfg, batches, daily, sim=sim)
    win.resize(1024, 600)
    win.show()
    pump(app, 1.0)

    # ---------------------------------------------------------------- home
    check("link indicator shows LIVE", "LIVE" in win.link_lbl.text())
    check("the simulator rig is present",
          find_button(win, "▼ POUR") is not None
          and find_button(win, "→ TARGET") is not None)
    home = win.screens["HOME"]
    check("START BATCH is locked with no water ratio set",
          not home.start_btn.isEnabled())
    check("the home screen says the water ratio is why",
          "water ratio" in home.prompt.text().lower())
    shot(win, "16-locked-no-ratio.png")

    # Supervisor sets the day's ratio.
    win.show_screen("WATER")
    pump(app, 0.4)
    water = win.screens["WATER"]
    check("SAVE is locked before a value is keyed in",
          not water.save_btn.isEnabled())
    for ch in "5.5":
        water.pad.press(ch)
    pump(app, 0.3)
    check("a decimal-point slip is refused outright",
          not water.save_btn.isEnabled() and "outside" in water.guide.text())
    water.pad.clear()
    for ch in "0.75":
        water.pad.press(ch)
    pump(app, 0.3)
    check("a plausible but off-nominal ratio is flagged, not blocked",
          water.save_btn.isEnabled() and "away from the usual" in water.guide.text())
    shot(win, "17-water-ratio.png")
    water.pad.clear()
    for ch in "0.55":
        water.pad.press(ch)
    pump(app, 0.3)
    check("a normal ratio previews the water it implies",
          "g water" in water.preview.text())
    water.save_btn.click()
    pump(app, 0.5)
    check("saving returns to the home screen", win.current == "HOME")
    check("today's ratio is now on file", daily.ratio() == 0.55)
    check("START BATCH armed once the ratio is set and the scale is live",
          home.start_btn.isEnabled())
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

    # --------------------------------------------------- water and ordering
    flour = 3200 * cfg.pct_of("chips_masala", "Binder (starch)") / 100
    water_step = next(s for s in win.st.steps if s.name == "Ice water")
    check("water target came from the day's ratio, not the recipe",
          abs(water_step.target - flour * 0.55) < 1e-6 and water_step.from_ratio)
    check("water differs from what the recipe percentage would have given",
          abs(water_step.target - 3200 * 10 / 100) > 1)
    routes_in_order = [s.scale for s in win.st.steps]
    check("floor-scale ingredients are ordered before bench-scale ones",
          routes_in_order == sorted(routes_in_order,
                                    key=lambda k: 0 if k == MAIN else 1))
    check("the batch captured the ratio it was planned on",
          win.st.water_ratio == 0.55)

    # -------------------------------------------------------------- review
    rev = win.screens["REVIEW"]
    heads = [rev.table.item(r, 0).text() for r in range(rev.table.rowCount())
             if rev.table.item(r, 0) and "—" in rev.table.item(r, 0).text()]
    check("the pick list is grouped by scale, floor first",
          len(heads) == 2 and heads[0].startswith(cfg.main.name.upper()))
    check("each group states its count and subtotal",
          "ingredient" in heads[0] and "g" in heads[0])
    check("the day's ratio is shown on the pick list",
          "0.550" in rev.crumb.text())
    shot(win, "05-review.png")
    find_button(rev, "START ADDING").click()
    pump(app, 0.5)

    # --------------------------------------------------- ingredient stepping
    add = win.screens["ADD"]
    man = win.screens["MANUAL"]
    targets = [s.target for s in win.st.steps]
    routes = [s.scale for s in win.st.steps]
    check("five ingredient steps computed", len(targets) == 5)
    check("the recipe is split across both scales",
          MAIN in routes and SMALL in routes)
    print(f"         routing: " + ", ".join(
        f"{s.name}={s.scale}" for s in win.st.steps))

    shot_under = shot_ok = shot_manual = False
    for i, (target, route) in enumerate(zip(targets, routes)):
        if win.current not in ("ADD", "MANUAL"):
            break

        if route == SMALL:
            # Bench scale: the operator keys the value in, and the floor scale
            # sees it arrive when it is tipped into the tub.
            check_once = not shot_manual
            if check_once:
                check("a bench-scale ingredient opens the manual screen",
                      win.current == "MANUAL")
                check("the manual screen names the bench scale",
                      "Bench scale" in man.instruction.text())
                check("CONFIRM is locked before anything is keyed in",
                      not man.confirm_btn.isEnabled())
                for ch in "1":
                    man.pad.press(ch)
                pump(app, 0.2)
                check("an under-target entry keeps CONFIRM locked",
                      not man.confirm_btn.isEnabled() and "under" in man.guide.text())
                man.pad.clear()
            # tip it in, then key the bench-scale reading
            sim.add(target)
            settle(app, state)
            for ch in f"{target:.1f}":
                man.pad.press(ch)
            pump(app, 0.3)
            if check_once:
                check("an in-tolerance entry arms CONFIRM",
                      man.confirm_btn.isEnabled())
                check("the floor scale reports what it witnessed",
                      "has seen" in man.witness.text()
                      or "Too small" in man.witness.text())
                shot(win, "14-manual-entry.png")
                shot_manual = True
            man.confirm_btn.click()
            pump(app, 0.4)
            continue

        # Floor scale: the existing guided flow.
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

    end = time.time() + 10
    while time.time() < end and win.current != "DONE":
        pump(app, 0.1)
    check("batch reached the DONE screen", win.current == "DONE")
    shot(win, "08-done.png")

    check("every ingredient captured an actual",
          all(s.actual is not None for s in win.st.steps))
    check("all ingredients landed inside tolerance",
          all(abs(s.actual - s.target) <= cfg.tol_of(s.target) for s in win.st.steps))
    check("bench-scale entries were witnessed by the floor scale where possible",
          all(s.verified is not False for s in win.st.steps))
    check("the batch total reconciles", win.reconcile().get("ok"))

    rows = batches.recent()
    check("batch written to the log on the Pi", len(rows) == 1)
    check("logged batch carries all five actuals",
          rows and len(rows[0]["steps"]) == 5
          and all(s["actual_g"] is not None for s in rows[0]["steps"]))
    check("the log records which scale weighed each ingredient",
          rows and all(s["weighed_on"] in (MAIN, SMALL) for s in rows[0]["steps"]))
    check("the log carries the reconciliation",
          rows and rows[0]["reconciliation"].get("ok") is True)
    check("the log records the water ratio the batch used",
          rows and rows[0]["water_ratio"] == 0.55)
    check("the log records the production day",
          rows and rows[0]["production_day"] == daily.production_day())

    import consumption
    cpath = os.path.join(ROOT, "_test_consumption.xlsx")
    nb, nrows, ning = consumption.build(LOG, cpath)
    check("the consumption workbook rebuilds from the batch log",
          nb == 1 and nrows == 5 and ning == 5)
    os.path.exists(cpath) and os.remove(cpath)

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

    # ------------------------------------------- the witness catching a lie
    from panel import WitnessDialog
    from panel import Step as PStep
    sim.zero(); pump(app, 1.5)
    win.st.steps = [PStep("Masala spice mix", 4, 128.0, scale=SMALL)]
    win.st.idx = 0
    win.st.step_zero = state.snapshot()["grams"] or 0.0
    win.show_screen("MANUAL")
    pump(app, 0.5)
    # Key in 128 g without ever tipping anything in.
    for ch in "128":
        man.pad.press(ch)
    pump(app, 0.4)
    observed = (state.snapshot()["grams"] or 0) - win.st.step_zero
    typed = man.pad.value()
    check("the floor scale can witness an addition this size",
          cfg.can_witness(128.0))
    check("an entry with nothing tipped in exceeds the witness tolerance",
          abs(observed - typed) > cfg.witness_tolerance(typed))
    wd = WitnessDialog(win, win.st.steps[0], typed, observed, cfg.main.name)
    wd.show(); pump(app, 0.3)
    check("the witness dialog says the floor scale saw nothing",
          "saw nothing" in wd.findChild(QLabel, "dialogTitle").text())
    shot(wd, "15-witness.png")
    wd.close()
    man.pad.clear()

    # And the honest case: too small for the floor scale to judge at all.
    win.st.steps = [PStep("Bhut jholokia", 0.01, 3.0, scale=SMALL)]
    win.show_screen("MANUAL")
    pump(app, 0.5)
    check("a tiny ingredient admits it cannot be cross-checked",
          not cfg.can_witness(3.0) and "unverified" in man.witness.text())

    # ------------------------------- recipe the scale cannot actually weigh
    from panel import Step
    # 0.05 g is under two divisions of the bench scale, so neither will do.
    win.st.steps = [Step("Binder", 18, 576), Step("Bhut jholokia", 0.001, 0.05)]
    win.show_screen("REVIEW")
    pump(app, 0.3)
    rev2 = win.screens["REVIEW"]
    check("an unweighable ingredient blocks START ADDING",
          not rev2.start_btn.isEnabled())
    check("the review screen says which ingredient and why",
          rev2.warning.isVisible() and "Bhut jholokia" in rev2.warning.text())
    shot(win, "13-unweighable.png")
    win.st.steps = [Step("Binder", 18, 576)]
    win.show_screen("REVIEW")
    pump(app, 0.3)
    check("a weighable recipe still starts normally", rev2.start_btn.isEnabled())

    # ------------------------------------------------------- the sim rig
    # The stale-feed checks above killed the reader; the rig needs it back.
    _t2, stop = start_reader(state, sim=sim)
    pump(app, 1.0)
    win.st.reset()
    win.show_screen("HOME")
    sim.zero()
    pump(app, 0.6)
    check("the rig shows its own true weight", "RIG" in win.sim_readout.text())

    win.sim_pour_start()
    pump(app, 1.2)
    win.sim_pour_stop()
    poured = sim.reading()
    check("press-and-hold POUR ramps weight in", poured > 20)
    check("the pour rate accelerates rather than trickling",
          poured > 1.2 * 10 * 2)     # faster than the opening 2 g/tick rate

    before = sim.reading()
    win.sim_lift()
    pump(app, 0.3)
    check("LIFT TUB takes the weight off", sim.reading() == 0 and sim.is_lifted)
    check("the rig readout says the tub is off", "tub off" in win.sim_readout.text())
    check("pouring into a lifted tub does nothing",
          (sim.add(100), sim.reading())[1] == 0)
    win.sim_lift()
    pump(app, 0.3)
    check("putting it back restores the weight", abs(sim.reading() - before) < 1e-6)

    # FILL TO TARGET should land the reading on whatever the screen wants.
    sim.zero()
    win.st.base, win.st.product, win.st.base_wt = "chicken", "chips_masala", 3200
    win.st.steps = [PStep("Binder", 18, 576.0)]
    win.st.idx = 0
    win.st.step_zero = 0.0
    win.show_screen("ADD")
    pump(app, 0.3)
    win.sim_fill_target()
    settle(app, state)
    add2 = win.screens["ADD"]
    check("FILL TO TARGET lands inside tolerance",
          abs(sim.reading() - 576.0) <= cfg.tol_of(576.0))
    # It may already have auto-advanced by the time we look, which is itself
    # the panel agreeing.
    check("and the panel agrees it is in tolerance",
          "OK" in add2.guide.text() or win.st.steps[0].actual is not None)
    check("the rig shows how far from target it is",
          "to go" in win.sim_readout.text())
    shot(win, "18-sim-rig.png")

    # SCOOP OUT is the other direction.
    win.sim_pour_start(-1)
    pump(app, 1.0)
    win.sim_pour_stop()
    check("SCOOP OUT takes weight back off", sim.reading() < 576.0)
    sim.zero()

    # ------------------------------------------------------------- scaling
    win.resize(1920, 1080)
    pump(app, 0.5)
    check("type scales up on a larger monitor", win.scale > 1.4)
    check("layout still fits the larger screen",
          win.centralWidget().sizeHint().height() <= 1080)
    shot(win, "12-1920x1080.png")

    win.close()
    for f in (LOG, DAILY):
        if os.path.exists(f):
            os.remove(f)

    failed = [c for c, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    print(f"screenshots in {SHOTS}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
