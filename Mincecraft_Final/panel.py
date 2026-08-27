#!/usr/bin/env python3
"""
DOKI weighing station — the MINCECRAFT operator panel, PyQt5.

All Qt lives here. The scale itself is scale.py, which knows nothing about
this module. The panel never touches the serial port: a reader thread owns it
and the panel polls ScaleState.snapshot() on a 100 ms timer.
"""

import os
import re
import time
from dataclasses import dataclass, field

from PyQt5.QtCore import Qt, QTimer, QRectF
from PyQt5.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import (
    QAbstractItemView, QDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QMainWindow, QPushButton, QSizePolicy, QStackedWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from scale import MAIN, SMALL, fmt, fmt_g

HERE = os.path.dirname(os.path.abspath(__file__))

INK    = "#e9eef4"
MUTED  = "#8fa0b0"
BLUE   = "#4a90d9"
GREEN  = "#3fbf7f"
AMBER  = "#e8b339"
RED    = "#e05d5d"
TRACK  = "#1b2530"

HOLD_TO_ACCEPT_S = 2.0     # in tolerance and stable this long -> auto-advance
DESIGN_W, DESIGN_H = 1024, 600


# --------------------------------------------------------------- batch state

@dataclass
class Step:
    name: str
    pct: float
    target: float
    actual: float = None
    skipped: bool = False
    scale: str = MAIN          # which scale weighs this one
    from_ratio: bool = False   # target came from the day's water ratio
    assumed: bool = False      # recorded as the target; bench scale not readable
    witness_g: float = None    # what the main scale saw arrive, if it could see it
    verified: bool = None      # None = the main scale is too coarse to tell


@dataclass
class BatchState:
    base: str = None
    product: str = None
    base_wt: float = 0.0
    steps: list = field(default_factory=list)
    idx: int = 0
    step_zero: float = 0.0        # software tare taken when the step opened
    rebalanced: bool = False
    batch_no: int = 1
    started_at: str = None
    water_ratio: float = None     # the ratio this batch was planned on

    def reset(self):
        self.base = None
        self.product = None
        self.base_wt = 0.0
        self.steps = []
        self.idx = 0
        self.step_zero = 0.0
        self.rebalanced = False
        self.started_at = None
        self.water_ratio = None


# ------------------------------------------------------------------ widgets

def button(text, variant=None, on_click=None, parent=None):
    b = QPushButton(text, parent)
    if variant:
        b.setProperty("variant", variant)
    b.setCursor(Qt.BlankCursor)
    if on_click:
        b.clicked.connect(on_click)
    return b


def label(text="", obj=None, align=Qt.AlignLeft, wrap=False):
    lb = QLabel(text)
    if obj:
        lb.setObjectName(obj)
    lb.setAlignment(align)
    lb.setWordWrap(wrap)
    return lb


def grams(value):
    """Show sub-gram precision only where the bench scale provides it."""
    if value >= 100:
        return f"{value:.0f} g"
    if value >= 10:
        return f"{value:.1f} g".replace(".0 g", " g")
    return f"{value:.2f} g"


def restyle(widget):
    """Re-apply the stylesheet after changing a property selectors depend on."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class LiveWeight(QWidget):
    """The big readout. Shows a dash, dimmed, whenever the reading is not live —
    never the last known number, which would read as current."""

    def __init__(self, scale=1.0):
        super().__init__()
        self.grams = None
        self.live = False
        self.stable = False
        self.scale = scale
        self.setMinimumHeight(int(110 * scale))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def update_from(self, grams, live, stable):
        if (grams, live, stable) != (self.grams, self.live, self.stable):
            self.grams, self.live, self.stable = grams, live, stable
            self.update()

    def text(self):
        return f"{self.grams:.0f}" if (self.live and self.grams is not None) else "—"

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        s = self.scale
        big = QFont(self.font()); big.setPixelSize(int(72 * s)); big.setBold(True)
        small = QFont(self.font()); small.setPixelSize(int(24 * s)); small.setBold(True)

        txt = self.text()
        p.setFont(big)
        tw = p.fontMetrics().horizontalAdvance(txt)
        p.setFont(small)
        uw = p.fontMetrics().horizontalAdvance(" g")
        dot_r = int(9 * s)
        gap = int(14 * s)
        total = dot_r * 2 + gap + tw + uw
        x = (self.width() - total) / 2
        cy = self.height() / 2

        p.setOpacity(1.0 if self.live else 0.35)

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(GREEN if (self.live and self.stable) else RED))
        p.drawEllipse(int(x), int(cy - dot_r), dot_r * 2, dot_r * 2)

        x += dot_r * 2 + gap
        p.setFont(big)
        p.setPen(QColor(INK))
        fm = p.fontMetrics()
        base_y = cy + fm.capHeight() / 2
        p.drawText(int(x), int(base_y), txt)

        x += tw
        p.setFont(small)
        p.setPen(QColor(MUTED))
        p.drawText(int(x), int(base_y), " g")


class ToleranceBar(QWidget):
    """Progress toward the target with the green tolerance band drawn on it.
    Port of #bar / #tolBand / #barFill."""

    def __init__(self, scale=1.0):
        super().__init__()
        self.added = 0.0
        self.target = 1.0
        self.tol = 1.0
        self.tone = "under"
        self.setFixedHeight(int(42 * scale))
        self.radius = int(8 * scale)

    def set_values(self, added, target, tol, tone):
        self.added, self.target, self.tol, self.tone = added, target, tol, tone
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), self.radius, self.radius)
        p.setClipPath(path)

        p.fillRect(0, 0, w, h, QColor(TRACK))

        span = max(self.target * 1.3, 1e-6)
        lo = (self.target - self.tol) / span * w
        hi = (self.target + self.tol) / span * w
        p.fillRect(QRectF(lo, 0, max(hi - lo, 1), h), QColor(63, 191, 127, 64))

        fill = min(1.0, self.added / span) * w
        colour = {"under": BLUE, "ok": GREEN, "over": RED}.get(self.tone, MUTED)
        p.fillRect(QRectF(0, 0, fill, h), QColor(colour))

        # Band edges last, so they stay readable once the fill covers them.
        pen = QPen(QColor(GREEN), 3)
        p.setPen(pen)
        p.drawLine(int(lo), 0, int(lo), h)
        p.drawLine(int(hi), 0, int(hi), h)


# ------------------------------------------------------------------ dialogs

class PanelDialog(QDialog):
    """Frameless modal card, centred on the panel."""

    def __init__(self, panel, title, body="", tone=None, width=440):
        super().__init__(panel)
        self.panel = panel
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setModal(True)
        self.setFixedWidth(int(width * panel.scale))
        self.setStyleSheet(panel.qss + f"""
            QDialog {{ border: 1px solid {
                {'alarm': RED, 'warn': AMBER}.get(tone, '#2a3440')}; border-radius: 12px; }}""")

        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(*[int(22 * panel.scale)] * 4)
        self.box.setSpacing(int(10 * panel.scale))

        t = label(title, "dialogTitle", wrap=True)
        if tone:
            t.setProperty("tone", tone)
        self.box.addWidget(t)
        if body:
            self.box.addWidget(label(body, "dialogBody", wrap=True))

    def showEvent(self, e):
        super().showEvent(e)
        g = self.panel.geometry()
        self.move(g.center().x() - self.width() // 2,
                  g.center().y() - self.height() // 2)


class PinDialog(PanelDialog):
    def __init__(self, panel, why):
        super().__init__(panel, "Supervisor PIN", why, width=380)
        self.entered = ""
        self.entry = label("", "pinEntry", align=Qt.AlignCenter)
        self.entry.setMinimumHeight(int(46 * panel.scale))
        self.box.addWidget(self.entry)

        grid = QGridLayout()
        grid.setSpacing(int(8 * panel.scale))
        for i, k in enumerate([1, 2, 3, 4, 5, 6, 7, 8, 9, "←", 0, "OK"]):
            grid.addWidget(button(str(k), on_click=lambda _, key=k: self.key(key)),
                           i // 3, i % 3)
        self.box.addLayout(grid)
        self.box.addWidget(button("CANCEL", "ghost", self.reject))

    def key(self, k):
        if k == "←":
            self.entered = self.entered[:-1]
        elif k == "OK":
            if self.entered == self.panel.cfg.pin:
                self.accept()
                return
            self.entered = ""
        elif len(self.entered) < 4:
            self.entered += str(k)
        self.entry.setText("•" * len(self.entered))


class AbortDialog(PanelDialog):
    def __init__(self, panel, idx, total):
        super().__init__(panel, "Abort batch?",
                         f"Logged as aborted at ingredient {idx} of {total}.",
                         tone="alarm")
        row = QHBoxLayout()
        row.addWidget(button("CANCEL", "ghost", self.reject))
        row.addWidget(button("YES, ABORT", "danger", self.accept))
        self.box.addLayout(row)


class PauseDialog(PanelDialog):
    """Container lifted off. RESUME only arms once the weight is actually back —
    the panel does not take the operator's word for it."""

    ABORT = 2

    def __init__(self, panel, step_zero):
        super().__init__(panel, "⚠ Weight dropped — container removed?",
                         "Put the container back on the scale. "
                         "The batch resumes at the same ingredient.", tone="alarm")
        self.step_zero = step_zero
        row = QHBoxLayout()
        self.abort_btn = button("ABORT", "danger", lambda: self.done(self.ABORT))
        self.resume_btn = button("RESUME (waiting…)", "primary", self.accept)
        self.resume_btn.setEnabled(False)
        row.addWidget(self.abort_btn)
        row.addWidget(self.resume_btn, 1)
        self.box.addLayout(row)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(100)

    def tick(self):
        snap = self.panel.state.snapshot()
        back = snap["fresh"] and snap["grams"] is not None \
            and snap["grams"] >= self.step_zero - 5
        self.resume_btn.setEnabled(back)
        self.resume_btn.setText("RESUME" if back else "RESUME (waiting…)")


class OverTargetDialog(PanelDialog):
    REMOVE, REBALANCE, ACCEPT = 2, 3, 4

    def __init__(self, panel, step, over_g, added_g, can_rebalance):
        super().__init__(panel, f"Over target by {fmt_g(over_g)} g",
                         f"{step.name}: target {fmt_g(step.target)} g, "
                         f"added {fmt_g(added_g)} g.", tone="warn")
        self.box.addWidget(button("REMOVE EXCESS — scoop some out", "primary",
                                  lambda: self.done(self.REMOVE)))
        rb = button("REBALANCE remaining ingredients" if can_rebalance
                    else "REBALANCE remaining ingredients (limit exceeded)",
                    "warn", lambda: self.done(self.REBALANCE))
        rb.setEnabled(can_rebalance)
        self.box.addWidget(rb)
        self.box.addWidget(button("SUPERVISOR ACCEPT (PIN, logged)", "ghost",
                                  lambda: self.done(self.ACCEPT)))


class WitnessDialog(PanelDialog):
    """The floor scale disagrees with what was keyed in."""

    REENTER, ACCEPT = 2, 3

    def __init__(self, panel, step, typed, observed, main_name):
        gap = observed - typed
        if abs(observed) < panel.cfg.main.division_g:
            headline = f"The {main_name} saw nothing go into the tub"
            body = (f"You entered {typed:.2f} g of {step.name}, but the "
                    f"{main_name} has not moved. Was it actually tipped in?")
        else:
            headline = f"The {main_name} saw {observed:+.0f} g, you entered {typed:.2f} g"
            body = (f"{step.name}: a difference of {gap:+.1f} g. Either the "
                    f"entry is wrong, or something else went into the tub.")
        super().__init__(panel, headline, body, tone="alarm", width=470)
        self.box.addWidget(button("RE-ENTER THE WEIGHT", "primary",
                                  lambda: self.done(self.REENTER)))
        self.box.addWidget(button("ACCEPT ANYWAY (PIN, logged)", "ghost",
                                  lambda: self.done(self.ACCEPT)))


class BatchLogDialog(PanelDialog):
    def __init__(self, panel, rows):
        super().__init__(panel, "Batch log",
                         f"Last {len(rows)} batches recorded on this station.",
                         width=620)
        table = QTableWidget(len(rows) or 1, 4)
        table.setHorizontalHeaderLabels(["Batch", "Product", "Base", "Logged"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setFocusPolicy(Qt.NoFocus)
        thead = table.horizontalHeader()
        thead.setSectionResizeMode(QHeaderView.ResizeToContents)
        thead.setSectionResizeMode(1, QHeaderView.Stretch)
        table.setMinimumHeight(int(300 * panel.scale))
        if not rows:
            item = QTableWidgetItem("No batches logged yet.")
            item.setForeground(QColor(MUTED))
            table.setItem(0, 0, item)
        for r, b in enumerate(reversed(rows)):
            cells = [f"#MC-{str(b.get('batch_no', 0)).zfill(4)}",
                     b.get("product") or "—",
                     f"{b.get('base_weight_g', 0)} g",
                     (b.get("logged_at") or "")[:16].replace("T", " ")]
            for c, text in enumerate(cells):
                table.setItem(r, c, QTableWidgetItem(text))
        self.box.addWidget(table)
        self.box.addWidget(button("CLOSE", "ghost", self.accept))


# ------------------------------------------------------------------ screens

class Screen(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.cfg = panel.cfg
        self.st = panel.st
        self.box = QVBoxLayout(self)
        m = int(16 * panel.scale)
        self.box.setContentsMargins(m, m, m, m)
        self.box.setSpacing(int(9 * panel.scale))
        self.build()

    def build(self):
        pass

    def enter(self):
        pass

    def tick(self, snap, live):
        pass

    def action_row(self, *widgets):
        row = QHBoxLayout()
        row.setSpacing(int(8 * self.panel.scale))
        for w, stretch in widgets:
            row.addWidget(w, stretch)
        self.box.addLayout(row)
        return row


class HomeScreen(Screen):
    def build(self):
        self.live = LiveWeight(self.panel.scale)
        self.box.addStretch(1)
        self.box.addWidget(self.live)
        self.prompt = label("", "prompt", Qt.AlignCenter, wrap=True)
        self.box.addWidget(self.prompt)
        self.ratio_line = label("", "guide", Qt.AlignCenter, wrap=True)
        self.box.addWidget(self.ratio_line)
        self.box.addStretch(1)
        self.start_btn = button("START BATCH", "primary", self.panel.start_batch)
        self.water_btn = button("SET TODAY'S WATER RATIO", "warn",
                                self.panel.open_water_ratio)
        self.action_row((button("MENU", "ghost", self.panel.open_menu), 0),
                        (self.water_btn, 0),
                        (self.start_btn, 1))

    def tick(self, snap, live):
        self.live.update_from(snap["grams"], live, snap["stable"])
        entry = self.panel.daily.current()

        # Two independent locks. Say which one is holding, not just "blocked".
        gated = self.cfg.any_water_gated

        if not live:
            self.prompt.setText("Waiting for the scale…")
        elif gated and entry is None:
            self.prompt.setText("Today's water ratio has not been set.")
        else:
            self.prompt.setText("Ready. Put the empty container on the scale "
                                "and press START BATCH.")
        self.start_btn.setEnabled(live and not (gated and entry is None))

        if not gated:
            # No recipe derives water from flour, so holding the line for a
            # ratio nobody uses would be friction with no safety behind it.
            tone, text = "dead", ("No recipe uses the water / flour ratio, "
                                  "so it is not required today.")
        elif entry is None:
            tone, text = "over", ("⚠ A supervisor must set the water / flour "
                                  "ratio for today before the first batch.")
        else:
            tone, text = "ok", (f"Water / flour ratio for "
                                f"{entry['date']}: {entry['ratio']:.3f}")
        if self.ratio_line.property("tone") != tone:
            self.ratio_line.setProperty("tone", tone)
            restyle(self.ratio_line)
        self.ratio_line.setText(text)


class WaterRatioScreen(Screen):
    """Supervisor sets the day's water ÷ flour ratio.

    Reached only through the PIN. The water each batch needs depends on the
    flour in use that day, so this cannot live in the recipe — and until it is
    set, no batch can start.
    """

    def build(self):
        s = self.panel.scale
        self.crumb = label("", "crumb")
        self.box.addWidget(self.crumb)

        body = QHBoxLayout()
        body.setSpacing(int(18 * s))
        col = QVBoxLayout()
        self.headline = label("Water ÷ flour ratio for today", "ingName")
        col.addWidget(self.headline)
        self.previous = label("", "target", wrap=True)
        col.addWidget(self.previous)
        col.addStretch(1)
        self.entry = label("—", "bigAdd", Qt.AlignCenter)
        col.addWidget(self.entry)
        self.guide = label("", "guide", Qt.AlignCenter, wrap=True)
        col.addWidget(self.guide)
        col.addStretch(1)
        self.preview = label("", "target", Qt.AlignCenter, wrap=True)
        col.addWidget(self.preview)
        body.addLayout(col, 1)

        self.pad = NumericPad(s, self.on_typed)
        self.pad.setMaximumWidth(int(300 * s))
        body.addWidget(self.pad, 0)
        self.box.addLayout(body, 1)

        self.save_btn = button("SAVE FOR TODAY ▶", "good", self.panel.save_water_ratio)
        self.save_btn.setEnabled(False)
        self.action_row((button("◀ CANCEL", "ghost", self.panel.go_home), 1),
                        (self.save_btn, 0))

    def enter(self):
        cfg, daily = self.cfg, self.panel.daily
        self.pad.clear()
        self.crumb.setText(
            f"Supervisor — <b>water ratio for {daily.production_day()}</b>")
        prev = daily.previous()
        self.previous.setText(
            f"Last set: {prev['ratio']:.3f} on {prev['date']}."
            if prev else "No previous ratio on file.")
        self.on_typed()

    def set_tone(self, tone, text):
        if self.guide.property("tone") != tone:
            self.guide.setProperty("tone", tone)
            restyle(self.guide)
        self.guide.setText(text)

    def on_typed(self):
        cfg, daily = self.cfg, self.panel.daily
        self.entry.setText(self.pad.text or "—")
        v = self.pad.value()
        if v is None or not self.pad.text:
            self.set_tone("dead", f"Enter a ratio between "
                                  f"{cfg.water_ratio_min:g} and {cfg.water_ratio_max:g}.")
            self.preview.setText("")
            self.save_btn.setEnabled(False)
            return

        problem = daily.validate(v)
        if problem:
            self.set_tone("over", f"{v:g} is {problem}.")
            self.preview.setText("")
            self.save_btn.setEnabled(False)
            return

        # Show what this actually means in grams, so a wrong number is visible
        # before it is committed rather than after.
        lines = []
        worst = 0.0
        for p in cfg.products:
            if not cfg.water_gated(p["id"]):
                continue
            flour = 3200 * cfg.pct_of(p["id"], cfg.flour_of(p["id"])) / 100
            off = daily.off_nominal(v, p["id"])
            worst = max(worst, abs(off or 0))
            lines.append(f"{p['name']}: {flour * v:.0f} g water on a 3.2 kg batch")
        self.preview.setText("   ·   ".join(lines))

        if worst > cfg.water_off_nominal_warn:
            self.set_tone("over", f"{worst:.0%} away from the usual ratio — "
                                  f"check before saving.")
        else:
            self.set_tone("ok", "In the usual range — press SAVE.")
        self.save_btn.setEnabled(True)


class CaptureScreen(Screen):
    def build(self):
        self.crumb = label("", "crumb")
        self.box.addWidget(self.crumb)
        self.live = LiveWeight(self.panel.scale)
        self.box.addStretch(1)
        self.box.addWidget(self.live)
        self.hint = label("", "prompt", Qt.AlignCenter, wrap=True)
        self.box.addWidget(self.hint)
        self.box.addStretch(1)
        self.cap_btn = button("CAPTURE WEIGHT", "good", self.panel.capture_base)
        self.cap_btn.setEnabled(False)
        self.action_row((button("◀ BACK", "ghost",
                                lambda: self.panel.show_screen("PRODUCT")), 0),
                        (self.cap_btn, 1))

    def enter(self):
        meat = self.st.base or "the meat"
        self.crumb.setText(
            f"Step 2 of 3 — <b>Weigh {meat}</b> · "
            f"{self.cfg.product_name(self.st.product)}")

    def tick(self, snap, live):
        self.live.update_from(snap["grams"], live, snap["stable"])
        floor = self.cfg.min_base_for(self.st.product)
        ok = live and snap["stable"] and snap["grams"] >= floor
        self.cap_btn.setEnabled(ok)
        if not live:
            self.hint.setText("Waiting for the scale…")
        elif ok:
            self.hint.setText(f"Stable: {snap['grams']:.0f} g — press CAPTURE")
        elif snap["grams"] < floor:
            need = f" — this product needs at least {floor:.0f} g" \
                   if floor > self.cfg.min_base_g else ""
            self.hint.setText(f"Put the {self.st.base or 'meat'} "
                              f"in the container…{need}")
        else:
            self.hint.setText("Stabilising…")


class ProductScreen(Screen):
    """Step 1 — the operator picks the finished product, nothing else.

    The meat follows from the product rather than being asked separately, so
    there is one decision at the start of a batch instead of two.
    """

    def build(self):
        self.box.addWidget(label("Step 1 of 3 — <b>Select product</b>", "crumb"))
        self.grid = QGridLayout()
        self.grid.setSpacing(int(10 * self.panel.scale))
        self.box.addLayout(self.grid)
        self.box.addStretch(1)
        self.note = label("", "target", Qt.AlignLeft, wrap=True)
        self.box.addWidget(self.note)
        self.action_row((button("◀ BACK", "ghost", self.panel.go_home), 0))

    def enter(self):
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.deleteLater()

        drafts = []
        for i, p in enumerate(self.cfg.products):
            draft = self.cfg.is_draft(p["id"])
            meat = self.cfg.meat_of(p["id"])
            caption = p["name"] if not meat else f"{p['name']}\n{meat}"
            btn = button(caption, "ghost" if draft else "primary",
                         lambda _, pid=p["id"]: self.panel.choose_product(pid))
            btn.setMinimumHeight(int(62 * self.panel.scale))
            if draft:
                btn.setEnabled(False)
                btn.setToolTip("No ingredients entered for this recipe yet")
                drafts.append(p["name"])
            self.grid.addWidget(btn, i // 2, i % 2)

        if drafts:
            self.note.setText(
                f"Greyed out — no ingredients entered yet: {', '.join(drafts)}. "
                f"Fill them in DOKI-Recipes.xlsx and run xlsx_to_recipes.py.")
        else:
            self.note.setText("")


class ReviewScreen(Screen):
    def build(self):
        self.crumb = label("", "crumb")
        self.box.addWidget(self.crumb)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["#", "Ingredient", "Weigh on", "Target", "Tol."])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(QHeaderView.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.Stretch)   # ingredient takes the slack
        # Column 0 carries the spanned group headers, so left to size itself it
        # would widen to fit "BENCH SCALE — 15 ingredients, 816 g".
        head.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, int(42 * self.panel.scale))
        self.box.addWidget(self.table, 1)
        self.warning = label("", "notice", Qt.AlignLeft, wrap=True)
        self.warning.setProperty("tone", "over")
        self.warning.setVisible(False)
        self.box.addWidget(self.warning)
        self.start_btn = button("START ADDING ▶", "good", self.panel.start_adding)
        self.action_row((button("◀ BACK", "ghost", lambda: self.panel.show_screen("PRODUCT")), 0),
                        (self.start_btn, 1))

    def enter(self):
        rb = " · <span style='color:#e8b339'>REBALANCED</span>" if self.st.rebalanced else ""
        water = ""
        if self.cfg.water_gated(self.st.product) and self.st.water_ratio:
            water = (f" · water/flour <b style='color:{BLUE}'>"
                     f"{self.st.water_ratio:.3f}</b>")
        self.crumb.setText(f"Step 3 of 3 — <b>Recipe review</b> · "
                           f"{self.cfg.product_name(self.st.product)}{water}{rb}")
        steps = self.st.steps
        # A step no scale can resolve must stop the batch here, not be
        # discovered by an operator who adds nothing and is waved through.
        problems = self.cfg.unweighable_steps(steps)
        bad_names = {name for name, _, _ in problems}
        degraded = []

        # Grouped in the order the operator will work: floor scale, then bench.
        groups = [(k, [s for s in steps if s.scale == k]) for k in (MAIN, SMALL)]
        groups = [(k, g) for k, g in groups if g]
        self.table.setRowCount(len(steps) + len(groups) + 1)

        row = 0
        n = 0
        for key, group in groups:
            spec = self.cfg.spec(key)
            subtotal = sum(s.target for s in group)
            head = QTableWidgetItem(
                f"{spec.name.upper()} — {len(group)} ingredient"
                f"{'s' if len(group) != 1 else ''}, {fmt(subtotal)}")
            f = head.font(); f.setBold(True); head.setFont(f)
            head.setForeground(QColor(AMBER if key == SMALL else BLUE))
            self.table.setItem(row, 0, head)
            self.table.setSpan(row, 0, 1, 5)
            row += 1

            for s in group:
                n += 1
                tick = " ✓" if s.actual is not None else ""
                if s.from_ratio:
                    tick += "  (from today's ratio)"
                tol = self.cfg.tol_of(s.target)
                is_deg = self.cfg.tolerance_degraded(s.target)
                if is_deg and s.name not in bad_names:
                    degraded.append(s.name)
                for c, text in enumerate([str(n), s.name + tick, spec.name,
                                          grams(s.target), "± " + grams(tol)]):
                    item = QTableWidgetItem(text)
                    if c >= 3:
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    if s.name in bad_names:
                        item.setForeground(QColor(RED))
                    elif is_deg and c >= 2:
                        item.setForeground(QColor(AMBER))
                    elif s.from_ratio and c == 1:
                        item.setForeground(QColor(BLUE))
                    self.table.setItem(row, c, item)
                row += 1

        # Ingredients listed at zero: shown so nobody wonders whether they were
        # forgotten, but not weighed.
        reference = self.cfg.reference_ingredients(self.st.product) \
            if self.st.product else []
        if reference:
            self.table.setRowCount(self.table.rowCount() + len(reference) + 1)
            head = QTableWidgetItem("LISTED BUT NOT WEIGHED — no quantity set")
            f = head.font(); f.setBold(True); head.setFont(f)
            head.setForeground(QColor(MUTED))
            self.table.setItem(row, 0, head)
            self.table.setSpan(row, 0, 1, 5)
            row += 1
            for n, _ in reference:
                for c, text in enumerate(["", n, "—", "—", ""]):
                    item = QTableWidgetItem(text)
                    item.setForeground(QColor(MUTED))
                    if c >= 3:
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    self.table.setItem(row, c, item)
                row += 1

        total = self.st.base_wt + sum(s.target for s in steps)
        for c, text in enumerate(["", "Total batch with meat", "", fmt(total), ""]):
            item = QTableWidgetItem(text)
            f = item.font(); f.setBold(True); item.setFont(f)
            if c >= 3:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row, c, item)

        self.start_btn.setEnabled(not problems)
        self.warning.setVisible(bool(problems) or bool(degraded))
        if problems:
            self.warning.setProperty("tone", "over"); restyle(self.warning)
            lines = "; ".join(f"{n} — {why}" for n, _, why in problems)
            self.warning.setText(
                f"⚠ Neither scale can weigh {len(problems)} of these "
                f"{len(steps)} ingredients: {lines}.")
        elif degraded:
            # Allowed, but the operator should not assume the recipe's
            # percentage is being enforced when the scale's step is coarser.
            self.warning.setProperty("tone", "ok"); restyle(self.warning)
            self.warning.setText(
                f"Held to the bench scale's resolution rather than the recipe's "
                f"{self.cfg._tol_pct:.0%} on: {', '.join(degraded)}.")


class AddScreen(Screen):
    def build(self):
        s = self.panel.scale
        head = QHBoxLayout()
        left = QVBoxLayout()
        self.step_no = label("", "stepNo")
        self.ing_name = label("", "ingName")
        self.target_line = label("", "target")
        for w in (self.step_no, self.ing_name, self.target_line):
            left.addWidget(w)
        right = QVBoxLayout()
        self.prod_line = label("", "stepNo", Qt.AlignRight)
        self.base_line = label("", "stepNo", Qt.AlignRight)
        right.addWidget(self.prod_line); right.addWidget(self.base_line)
        head.addLayout(left, 1); head.addLayout(right, 0)
        self.box.addLayout(head)

        self.box.addStretch(1)
        # Sized from the stylesheet, not setFont — QSS wins over QFont.
        self.big = label("0", "bigAdd", Qt.AlignCenter)
        self.box.addWidget(self.big)
        self.bar = ToleranceBar(s)
        self.box.addWidget(self.bar)
        self.guide = label("", "guide", Qt.AlignCenter)
        self.box.addWidget(self.guide)
        self.box.addStretch(1)

        self.next_btn = button("NEXT ▶", "good", self.panel.accept_step)
        self.next_btn.setEnabled(False)
        self.action_row((button("ABORT", "danger", self.panel.confirm_abort), 0),
                        (button("SKIP (PIN)", "ghost", self.panel.skip_step), 1),
                        (self.next_btn, 0))

        self.ok_since = None
        self.over_prompted = False

    def enter(self):
        st, cfg = self.st, self.cfg
        s = st.steps[st.idx]
        self.ok_since = None
        self.over_prompted = False
        self.step_no.setText(f"Ingredient {st.idx + 1} of {len(st.steps)}")
        self.ing_name.setText(s.name)
        self.target_line.setText(
            f"Target <b style='color:{INK}'>{fmt_g(s.target)} g</b> "
            f"(± {fmt_g(cfg.tol_of(s.target))} g)")
        self.prod_line.setText(cfg.product_name(st.product))
        self.base_line.setText(f"{st.base or 'Meat'} {fmt(st.base_wt)}")
        self.big.setText(f"0 / {fmt_g(s.target)} g")

    def set_tone(self, tone, text):
        if self.guide.property("tone") != tone:
            self.guide.setProperty("tone", tone)
            restyle(self.guide)
        self.guide.setText(text)

    def tick(self, snap, live):
        st, cfg = self.st, self.cfg
        s = st.steps[st.idx]
        tol = cfg.tol_of(s.target)

        if not live:
            self.set_tone("dead", "Scale not reporting — hold.")
            self.next_btn.setEnabled(False)
            self.ok_since = None
            self.big.setText(f"— / {fmt_g(s.target)} g")
            return

        # Container lifted off mid-step.
        if snap["grams"] < st.step_zero - cfg.drop_alarm_g:
            self.panel.container_removed()
            return

        added = max(0.0, snap["grams"] - st.step_zero)
        self.big.setText(f"{added:.0f} / {fmt_g(s.target)} g")

        if added < s.target - tol:
            self.bar.set_values(added, s.target, tol, "under")
            self.set_tone("under", f"Add {s.target - added:.0f} g more…")
            self.next_btn.setEnabled(False)
            self.ok_since = None
        elif added <= s.target + tol:
            self.bar.set_values(added, s.target, tol, "ok")
            self.next_btn.setEnabled(True)
            if snap["stable"]:
                if self.ok_since is None:
                    self.ok_since = time.monotonic()
                left = HOLD_TO_ACCEPT_S - (time.monotonic() - self.ok_since)
                self.set_tone("ok", f"OK — hold steady… {left:.1f} s" if left > 0 else "OK")
                if left <= 0:
                    self.panel.accept_step()
            else:
                self.ok_since = None
                self.set_tone("ok", "OK — stop adding to confirm")
        else:
            self.bar.set_values(added, s.target, tol, "over")
            self.set_tone("over", f"Over by {added - s.target:.0f} g — scoop some out")
            self.next_btn.setEnabled(False)
            self.ok_since = None
            if added > s.target + tol * 3 and snap["stable"] and not self.over_prompted:
                self.over_prompted = True
                self.panel.over_target(added)


class NumericPad(QWidget):
    """Touch keypad for entering what the bench scale read."""

    def __init__(self, scale, on_change):
        super().__init__()
        self.text = ""
        self.on_change = on_change
        grid = QGridLayout(self)
        grid.setSpacing(int(8 * scale))
        grid.setContentsMargins(0, 0, 0, 0)
        for i, k in enumerate(["1", "2", "3", "4", "5", "6", "7", "8", "9",
                               ".", "0", "←"]):
            b = button(k, None, lambda _, key=k: self.press(key))
            b.setMinimumHeight(int(48 * scale))
            grid.addWidget(b, i // 3, i % 3)

    def press(self, k):
        if k == "←":
            self.text = self.text[:-1]
        elif k == ".":
            if "." not in self.text:
                self.text = (self.text or "0") + "."
        elif len(self.text.replace(".", "")) < 6:
            self.text += k
        self.on_change()

    def clear(self):
        self.text = ""
        self.on_change()

    def value(self):
        try:
            return float(self.text)
        except ValueError:
            return None


class ManualAddScreen(Screen):
    """An ingredient too small for the floor scale.

    The operator weighs it on the bench scale — which the Pi cannot see — and
    tips it into the tub. The floor scale watches it arrive, so the bar moves
    and a missing addition is caught, but it resolves only to 1 g and cannot
    confirm the bench scale's figure. So the recorded actual is the *target*,
    flagged as assumed rather than measured, and the batch reconciliation at
    the end is what catches a dosing that went badly wrong.
    """

    def build(self):
        s = self.panel.scale
        head = QHBoxLayout()
        left = QVBoxLayout()
        self.step_no = label("", "stepNo")
        self.ing_name = label("", "ingName")
        self.target_line = label("", "target")
        for w in (self.step_no, self.ing_name, self.target_line):
            left.addWidget(w)
        right = QVBoxLayout()
        self.scale_badge = label("", "stepNo", Qt.AlignRight)
        self.prod_line = label("", "stepNo", Qt.AlignRight)
        right.addWidget(self.scale_badge); right.addWidget(self.prod_line)
        head.addLayout(left, 1); head.addLayout(right, 0)
        self.box.addLayout(head)

        self.instruction = label("", "prompt", Qt.AlignCenter, wrap=True)
        self.box.addWidget(self.instruction)
        self.box.addStretch(1)

        self.big = label("0", "bigAdd", Qt.AlignCenter)
        self.box.addWidget(self.big)
        self.bar = ToleranceBar(s)
        self.box.addWidget(self.bar)
        self.guide = label("", "guide", Qt.AlignCenter, wrap=True)
        self.box.addWidget(self.guide)
        self.box.addStretch(1)
        self.witness = label("", "target", Qt.AlignCenter, wrap=True)
        self.box.addWidget(self.witness)

        self.confirm_btn = button("CONFIRM ▶", "good", self.panel.confirm_manual)
        self.action_row((button("ABORT", "danger", self.panel.confirm_abort), 0),
                        (button("SKIP (PIN)", "ghost", self.panel.skip_step), 1),
                        (self.confirm_btn, 0))

    def enter(self):
        st, cfg = self.st, self.cfg
        s = st.steps[st.idx]
        spec = cfg.spec(s.scale)
        self.step_no.setText(f"Ingredient {st.idx + 1} of {len(st.steps)}")
        self.ing_name.setText(s.name)
        self.target_line.setText(
            f"Target <b style='color:{INK}'>{grams(s.target)}</b> "
            f"(± {grams(cfg.tol_of(s.target))})")
        self.scale_badge.setText(f"<b style='color:{AMBER}'>{spec.name}</b>")
        self.prod_line.setText(cfg.product_name(st.product))
        self.instruction.setText(
            f"Weigh <b>{grams(s.target)}</b> of <b>{s.name}</b> on the "
            f"{spec.name}, then tip it into the tub.")

    def set_tone(self, tone, text):
        if self.guide.property("tone") != tone:
            self.guide.setProperty("tone", tone)
            restyle(self.guide)
        self.guide.setText(text)

    def tick(self, snap, live):
        """The floor scale watching the ingredient arrive."""
        st, cfg = self.st, self.cfg
        s = st.steps[st.idx]
        tol = cfg.tol_of(s.target)

        if not live:
            self.big.setText("—")
            self.bar.set_values(0, s.target, tol, "under")
            self.set_tone("dead", f"{cfg.main.name} not reporting — "
                                  f"weigh it on the bench and press CONFIRM.")
            self.witness.setText("")
            return

        seen = max(0.0, snap["grams"] - st.step_zero)
        self.big.setText(f"{seen:.0f} / {grams(s.target)}")
        # The floor scale is coarse, so judge arrival against what it can
        # actually resolve rather than the bench scale's tighter band.
        slack = cfg.witness_tolerance(s.target)
        if seen < s.target - slack:
            tone, msg = "under", f"{s.target - seen:.0f} g still to go in"
        elif seen > s.target + slack:
            tone, msg = "over", f"{seen - s.target:.0f} g more than expected"
        else:
            tone, msg = "ok", "That looks right — press CONFIRM"
        self.bar.set_values(seen, s.target, tol, tone)
        self.set_tone(tone, msg)

        if not cfg.can_witness(s.target):
            self.witness.setText(
                f"At {grams(s.target)} this is below what the {cfg.main.name} "
                f"can see, so nothing here can be cross-checked.")
        else:
            self.witness.setText(
                f"{cfg.main.name} has seen {seen:+.0f} g arrive. The recorded "
                f"weight will be the {grams(s.target)} target, not this.")


class DoneScreen(Screen):
    def build(self):
        self.head = label("", "doneHead")
        self.box.addWidget(self.head)
        self.stars = label("", "stars", Qt.AlignCenter)
        self.box.addWidget(self.stars)
        self.msg = label("", "prompt", Qt.AlignCenter)
        self.box.addWidget(self.msg)
        self.recon = label("", "target", Qt.AlignCenter, wrap=True)
        self.box.addWidget(self.recon)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["#", "Ingredient", "Weighed on", "Target", "Actual", "Dev."])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(QHeaderView.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.Stretch)   # ingredient takes the slack
        # Column 0 carries the spanned group headers, so left to size itself it
        # would widen to fit "BENCH SCALE — 15 ingredients, 816 g".
        head.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, int(42 * self.panel.scale))
        self.box.addWidget(self.table, 1)
        self.action_row((button("DONE — NEW BATCH", "primary", self.panel.new_batch), 1))

    def enter(self):
        st, cfg = self.st, self.cfg
        scored = [s for s in st.steps if s.actual is not None]
        within = sum(1 for s in scored if abs(s.actual - s.target) <= cfg.tol_of(s.target))
        within2 = sum(1 for s in scored if abs(s.actual - s.target) <= 2 * cfg.tol_of(s.target))
        stars = 3 if scored and within == len(scored) else (2 if within2 == len(scored) else 1)
        self.head.setText(f"✓ BATCH COMPLETE — #MC-{str(st.batch_no).zfill(4)} · logged ✓")
        self.stars.setText("".join(
            f"<span style='color:{AMBER if i < stars else '#2a3440'}'>★</span>"
            for i in range(3)))
        self.msg.setText({3: "Perfect batch!", 2: "Good — a little off on some."}
                         .get(stars, "Logged — needs more care."))

        self.table.setRowCount(len(st.steps))
        for i, s in enumerate(st.steps):
            if s.actual is None:
                actual, dev_text, colour = "—", "skipped", RED
            else:
                dev = s.actual - s.target
                pct = dev / s.target * 100 if s.target else 0
                actual = f"{fmt_g(s.actual)} g"
                dev_text = f"{'+' if dev >= 0 else ''}{dev:.0f} g ({pct:.1f}%)"
                colour = GREEN if abs(dev) <= cfg.tol_of(s.target) else RED
            spec = cfg.spec(s.scale)
            where = spec.name if spec else "—"
            if s.assumed:
                where += " (assumed)"
            if s.verified is False:
                where += " ⚠"
            for c, text in enumerate([str(i + 1), s.name, where,
                                      f"{fmt_g(s.target)} g", actual, dev_text]):
                item = QTableWidgetItem(text)
                if c >= 3:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 5:
                    item.setForeground(QColor(colour))
                if c == 2 and s.verified is False:
                    item.setForeground(QColor(RED))
                elif c == 2 and s.verified is None and s.scale == SMALL:
                    item.setForeground(QColor(MUTED))
                self.table.setItem(i, c, item)

        r = self.panel.reconcile()
        if not r.get("available"):
            self.recon.setText("Batch total could not be reconciled — "
                               "the floor scale was not reporting at the end.")
        elif r["ok"]:
            self.recon.setText(
                f"Batch total reconciles: floor scale {r['observed_g']:.0f} g "
                f"vs {r['expected_g']:.0f} g recorded "
                f"({r['difference_g']:+.0f} g, within ±{r['allowance_g']:.0f} g).")
        else:
            self.recon.setText(
                f"⚠ Batch total does not reconcile: floor scale reads "
                f"{r['observed_g']:.0f} g but {r['expected_g']:.0f} g was recorded "
                f"({r['difference_g']:+.0f} g, outside ±{r['allowance_g']:.0f} g). "
                f"One of the bench-scale entries is likely wrong.")


class MenuScreen(Screen):
    def build(self):
        self.box.addWidget(label("<b>Menu</b> (supervisor)", "crumb"))
        grid = QGridLayout()
        grid.setSpacing(int(10 * self.panel.scale))
        entries = [("Today's water ratio", lambda: self.panel.show_screen("WATER")),
                   ("Scale calibration", None),
                   ("Recipe editor", None), ("Batch log", self.panel.show_batch_log)]
        for i, (text, fn) in enumerate(entries):
            btn = button(text, None, fn)
            btn.setMinimumHeight(int(60 * self.panel.scale))
            btn.setEnabled(fn is not None)
            grid.addWidget(btn, i // 2, i % 2)
        self.box.addLayout(grid)
        self.box.addStretch(1)
        self.action_row((button("◀ EXIT MENU", "ghost", self.panel.go_home), 1),
                        (button("EXIT TO DESKTOP", "danger", self.panel.quit_app), 0))


# -------------------------------------------------------------------- panel

class Panel(QMainWindow):
    SCREENS = {"HOME": HomeScreen, "CAPTURE": CaptureScreen,
               "PRODUCT": ProductScreen, "REVIEW": ReviewScreen, "ADD": AddScreen,
               "MANUAL": ManualAddScreen, "DONE": DoneScreen, "MENU": MenuScreen,
               "WATER": WaterRatioScreen}

    def __init__(self, state, cfg, batches, daily, sim=None):
        super().__init__()
        self.state = state
        self.cfg = cfg
        self.batches = batches
        self.daily = daily
        self.sim = sim
        self.st = BatchState()
        self.dialog_open = False
        self.scale = 1.0
        self.qss = ""

        self.setWindowTitle("MINCECRAFT — Weighing Station")
        self.setMinimumSize(DESIGN_W, DESIGN_H)
        self._build()
        self.set_display_scale(1.0)
        self.show_screen("HOME")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(100)

    # -- construction ------------------------------------------------------

    def _build(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        bar = QFrame(); bar.setObjectName("statusbar")
        hb = QHBoxLayout(bar)
        hb.setContentsMargins(14, 8, 14, 8)
        self.name_lbl = label("MINCECRAFT", "stationName")
        self.info_lbl = label("", align=Qt.AlignCenter)
        self.link_lbl = label("CONNECTING…", "link", Qt.AlignRight)
        self.link_lbl.setProperty("state", "down")
        hb.addWidget(self.name_lbl, 0)
        hb.addWidget(self.info_lbl, 1)
        hb.addWidget(self.link_lbl, 0)
        outer.addWidget(bar)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self.screens = {}
        for key, cls in self.SCREENS.items():
            w = cls(self)
            self.screens[key] = w
            self.stack.addWidget(w)
        self.current = "HOME"

        if self.sim is not None:
            outer.addWidget(self._sim_bar())

        self.setCentralWidget(root)

    def _sim_bar(self):
        """The bench rig: stands in for a scale, a tub and a pair of hands.

        POUR is press-and-hold with a rate that ramps the longer it is held,
        because that is what makes the tolerance guidance worth testing — a
        constant trickle would never overshoot, and overshooting is the case
        the panel exists to handle.
        """
        bar = QFrame(); bar.setObjectName("simbar")
        hb = QHBoxLayout(bar)
        hb.setContentsMargins(10, 6, 10, 6)
        hb.setSpacing(int(6 * self.scale))

        self.sim_readout = label("", "simReadout")
        self.sim_readout.setMinimumWidth(int(150 * self.scale))
        hb.addWidget(self.sim_readout)
        hb.addSpacing(int(10 * self.scale))

        self.pour_btn = QPushButton("▼ POUR")
        self.pour_btn.setToolTip("Press and hold — the rate ramps up")
        self.pour_btn.setObjectName("pourBtn")
        self.pour_btn.setCursor(Qt.BlankCursor)
        self.pour_btn.pressed.connect(self.sim_pour_start)
        self.pour_btn.released.connect(self.sim_pour_stop)
        hb.addWidget(self.pour_btn)

        self.scoop_btn = QPushButton("▲ SCOOP")
        self.scoop_btn.setToolTip("Press and hold to take weight back off")
        self.scoop_btn.setCursor(Qt.BlankCursor)
        self.scoop_btn.pressed.connect(lambda: self.sim_pour_start(-1))
        self.scoop_btn.released.connect(self.sim_pour_stop)
        hb.addWidget(self.scoop_btn)

        for text, grams in [("+1", 1), ("+5", 5), ("+50", 50), ("+500", 500),
                            ("−5", -5), ("−50", -50)]:
            hb.addWidget(button(text, None, lambda _, g=grams: self.sim.add(g)))

        hb.addStretch(1)
        self.fill_btn = button("→ TARGET", None, self.sim_fill_target)
        self.fill_btn.setToolTip("Jump to exactly what this screen is waiting for")
        hb.addWidget(self.fill_btn)
        self.lift_btn = button("LIFT TUB", None, self.sim_lift)
        self.lift_btn.setToolTip("Take the container off, to rehearse the drop alarm")
        hb.addWidget(self.lift_btn)
        hb.addWidget(button("EMPTY", None, self.sim.zero))

        self.pour_dir = 1
        self.pour_ticks = 0
        self.pour_timer = QTimer(self)
        self.pour_timer.timeout.connect(self.sim_pour_tick)
        return bar

    # -- simulation controls ------------------------------------------------

    def sim_pour_start(self, direction=1):
        self.pour_dir = direction
        self.pour_ticks = 0
        self.pour_timer.start(100)

    def sim_pour_stop(self):
        self.pour_timer.stop()

    def sim_pour_tick(self):
        # 20 g/s building to 280 g/s, the same ramp the browser simulator used.
        self.pour_ticks += 1
        rate = min(2 + self.pour_ticks * 1.6, 28)
        self.sim.add(rate * self.pour_dir)

    def sim_fill_target(self):
        """Jump straight to whatever the current screen is waiting for.

        Walking a seven-ingredient recipe by holding POUR each time is fine
        once; this is for the fifth run-through.
        """
        if self.current == "CAPTURE":
            self.sim.set(3200)
        elif self.current in ("ADD", "MANUAL") and self.st.steps:
            self.sim.set(self.st.step_zero + self.st.steps[self.st.idx].target)
        else:
            self.sim.set(3200)

    def sim_lift(self):
        lifted = self.sim.lift()
        self.lift_btn.setText("TUB BACK" if lifted else "LIFT TUB")

    def _paint_sim(self):
        if self.sim is None:
            return
        if self.sim.is_lifted:
            self.sim_readout.setText("RIG · tub off")
            return
        true_g = self.sim.reading()
        wanted = ""
        if self.current in ("ADD", "MANUAL") and self.st.steps:
            step = self.st.steps[self.st.idx]
            need = self.st.step_zero + step.target - true_g
            wanted = f" · {need:+.0f} to go"
        self.sim_readout.setText(f"RIG · {true_g:,.0f} g{wanted}")

    def set_display_scale(self, factor):
        """Multiply every {N}px in the stylesheet, so a 1920×1080 monitor gets
        proportionally larger type rather than a small panel in the corner."""
        self.scale = factor
        with open(os.path.join(HERE, "style.qss"), "r", encoding="utf-8") as fh:
            raw = fh.read()
        self.qss = re.sub(r"\{(\d+)\}", lambda m: str(max(1, round(int(m.group(1)) * factor))), raw)
        self.setStyleSheet(self.qss)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        factor = min(self.width() / DESIGN_W, self.height() / DESIGN_H)
        factor = max(1.0, min(factor, 2.0))
        if abs(factor - self.scale) > 0.05:
            self.set_display_scale(factor)

    # -- navigation --------------------------------------------------------

    def show_screen(self, key):
        self.current = key
        w = self.screens[key]
        w.enter()
        self.stack.setCurrentWidget(w)
        self._paint_info()

    def _paint_info(self):
        st = self.st
        if st.product:
            bits = [self.cfg.product_name(st.product)]
            if st.base:
                bits.append(st.base)
            if st.base_wt:
                bits.append(f"meat {fmt(st.base_wt)}")
            self.info_lbl.setText(" · ".join(bits))
        else:
            self.info_lbl.setText("")

    # -- the 100 ms poll ---------------------------------------------------

    def tick(self):
        snap = self.state.snapshot()
        live = snap["fresh"] and snap["grams"] is not None

        state = "live" if live else ("stale" if snap["connected"] else "down")
        text = {"live": "● LIVE", "stale": "STALE", "down": "NO SCALE"}[state]
        if self.link_lbl.property("state") != state:
            self.link_lbl.setProperty("state", state)
            restyle(self.link_lbl)
        self.link_lbl.setText(text)

        self._paint_sim()
        if not self.dialog_open:
            self.screens[self.current].tick(snap, live)

    def _dialog(self, dlg):
        """Run a modal dialog with the screen tick suspended."""
        self.dialog_open = True
        try:
            return dlg.exec_()
        finally:
            self.dialog_open = False

    def ask_pin(self, why):
        return self._dialog(PinDialog(self, why)) == QDialog.Accepted

    # -- flow --------------------------------------------------------------

    def start_batch(self):
        if self.cfg.any_water_gated and not self.daily.is_set():
            return                      # the home screen already says why
        self.st.started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        # Captured now, so a batch that starts at 23:55 finishes on the ratio
        # it began with even though the day rolls over mid-batch.
        self.st.water_ratio = self.daily.ratio()
        self.show_screen("PRODUCT")

    def open_water_ratio(self):
        if self.ask_pin("Enter supervisor PIN to set today's water ratio."):
            self.show_screen("WATER")

    def save_water_ratio(self):
        value = self.screens["WATER"].pad.value()
        if value is None:
            return
        try:
            self.daily.set(value)
        except ValueError:
            return                      # the screen already showed the reason
        self.go_home()

    def go_home(self):
        self.st.reset()
        self.show_screen("HOME")

    def open_menu(self):
        if self.ask_pin("Enter supervisor PIN to open the menu."):
            self.show_screen("MENU")

    def capture_base(self):
        snap = self.state.snapshot()
        if not snap["fresh"] or snap["grams"] is None:
            return
        self.st.base_wt = snap["grams"]
        self.pick_product(self.st.product)

    def choose_product(self, product_id):
        """Product is chosen first; the meat it implies is what gets weighed."""
        if self.cfg.is_draft(product_id):
            return                      # the button is disabled anyway
        self.st.product = product_id
        self.st.base = self.cfg.meat_of(product_id)
        self.show_screen("CAPTURE")

    def pick_product(self, product_id):
        """Every target computed at once, the moment the meat is on the scale."""
        self.st.product = product_id
        self.st.rebalanced = False
        cfg = self.cfg
        p = cfg.product(product_id)

        gated = cfg.water_gated(product_id)
        flour_name = cfg.flour_of(product_id) if gated else None
        water_name = cfg.water_of(product_id) if gated else None
        flour_target = (self.st.base_wt * cfg.pct_of(product_id, flour_name) / 100
                        if gated else None)

        self.st.steps = []
        for n, pct in cfg.active_ingredients(product_id):
            if gated and n == water_name:
                # Water is not a recipe percentage — it follows the day's flour.
                target = flour_target * self.st.water_ratio
                pct = target / self.st.base_wt * 100 if self.st.base_wt else 0
            else:
                target = self.st.base_wt * pct / 100
            self.st.steps.append(Step(name=n, pct=pct, target=target,
                                      scale=cfg.scale_for(target) or MAIN,
                                      from_ratio=bool(gated and n == water_name)))
        self.order_steps()
        self.show_screen("REVIEW")

    def order_steps(self):
        """Floor scale first, then bench, keeping recipe order inside each.

        One trip to the tub and one stint at the bench, instead of walking
        between the two for every ingredient.
        """
        self.st.steps.sort(key=lambda s: 0 if s.scale == MAIN else 1)

    def start_adding(self):
        idx = next((i for i, s in enumerate(self.st.steps)
                    if s.actual is None and not s.skipped), 0)
        self.open_step(idx)

    def open_step(self, idx):
        """Software tare: the step measures from the reading at the moment it
        opened, so the operator adds cumulatively into one container."""
        snap = self.state.snapshot()
        self.st.idx = idx
        self.st.step_zero = snap["grams"] if snap["grams"] is not None else 0.0
        step = self.st.steps[idx]
        self.show_screen("MANUAL" if step.scale == SMALL else "ADD")

    def current_added(self):
        snap = self.state.snapshot()
        if snap["grams"] is None:
            return 0.0
        return max(0.0, snap["grams"] - self.st.step_zero)

    def accept_step(self):
        step = self.st.steps[self.st.idx]
        step.actual = self.current_added()
        step.witness_g = step.actual      # weighed on the main scale directly
        step.verified = True
        step.assumed = False
        self.next_step()

    def confirm_manual(self):
        """Accept a bench-weighed ingredient.

        There is no keypad, so the bench scale's reading never reaches the Pi.
        The target is recorded as the actual and flagged `assumed`, and the
        floor scale is still asked whether something of roughly that size went
        in — which is the one thing it can genuinely answer.
        """
        step = self.st.steps[self.st.idx]
        snap = self.state.snapshot()
        observed = (snap["grams"] - self.st.step_zero) if snap["fresh"] else None

        step.actual = step.target
        step.assumed = True
        step.witness_g = observed

        if not self.cfg.can_witness(step.target) or observed is None:
            step.verified = None       # nothing could be checked; say so
            self.next_step()
            return

        if abs(observed - step.target) > self.cfg.witness_tolerance(step.target):
            dlg = WitnessDialog(self, step, step.target, observed,
                                self.cfg.main.name)
            if self._dialog(dlg) != WitnessDialog.ACCEPT:
                return
            if not self.ask_pin("Accept an addition the floor scale disagrees "
                                "with (will be logged)."):
                return
            step.verified = False
        else:
            step.verified = True
        self.next_step()

    def next_step(self):
        nxt = next((i for i, s in enumerate(self.st.steps)
                    if i > self.st.idx and s.actual is None and not s.skipped), None)
        if nxt is None:
            self.record_batch()
            self.show_screen("DONE")
        else:
            self.open_step(nxt)

    def skip_step(self):
        if self.ask_pin("Skip this ingredient (logged as skipped)."):
            self.st.steps[self.st.idx].skipped = True
            self.next_step()

    def new_batch(self):
        self.st.batch_no += 1
        self.go_home()

    # -- interruptions -----------------------------------------------------

    def confirm_abort(self):
        dlg = AbortDialog(self, self.st.idx + 1, len(self.st.steps))
        if self._dialog(dlg) == QDialog.Accepted:
            self.st.batch_no += 1
            self.go_home()

    def container_removed(self):
        dlg = PauseDialog(self, self.st.step_zero)
        result = self._dialog(dlg)
        if result == PauseDialog.ABORT:
            self.st.batch_no += 1
            self.go_home()
            return
        # Resumed: re-tare so the amount already added is preserved.
        snap = self.state.snapshot()
        if snap["grams"] is not None:
            self.st.step_zero = snap["grams"] - min(
                self.screens["ADD"].bar.added, self.st.steps[self.st.idx].target * 2)
        self.screens["ADD"].over_prompted = False

    def over_target(self, added):
        st, cfg = self.st, self.cfg
        s = st.steps[st.idx]
        over = added - s.target
        can_rb = over <= s.target * cfg.rebalance_limit and st.idx < len(st.steps) - 1
        result = self._dialog(OverTargetDialog(self, s, over, added, can_rb))

        if result == OverTargetDialog.REBALANCE:
            k = self.current_added() / s.target
            for later in st.steps[st.idx + 1:]:
                later.target *= k
            s.actual = self.current_added()
            s.target = s.actual
            st.rebalanced = True
            # Later targets moved, so the pick list must be redrawn, not left
            # showing the numbers the operator memorised a minute ago.
            self.order_steps()
            self.show_screen("REVIEW")
        elif result == OverTargetDialog.ACCEPT:
            if self.ask_pin("Accept over-target amount (will be logged)."):
                self.accept_step()
            else:
                self.screens["ADD"].over_prompted = False
        else:
            self.screens["ADD"].over_prompted = False

    # -- output ------------------------------------------------------------

    def reconcile(self):
        """Compare what the floor scale gained across the whole batch with the
        sum of everything recorded.

        Individually a 2 g spice is invisible to a 5 g scale. Collectively the
        bench-weighed ingredients are not, so the batch total is the one check
        that covers them — it will not tell you which entry was wrong, but it
        will tell you that one was.
        """
        snap = self.state.snapshot()
        if not snap["fresh"] or snap["grams"] is None or not self.st.steps:
            return {"available": False}
        expected = self.st.base_wt + sum(s.actual or 0 for s in self.st.steps)
        observed = snap["grams"]
        # One division of slack per weighing, since each is quantised.
        allowance = self.cfg.main.division_g * (len(self.st.steps) + 1)
        return {"available": True,
                "expected_g": round(expected, 1),
                "observed_g": round(observed, 1),
                "difference_g": round(observed - expected, 1),
                "allowance_g": round(allowance, 1),
                "ok": abs(observed - expected) <= allowance}

    def record_batch(self):
        st = self.st
        self.batches.append({
            "batch_no": st.batch_no,
            "base": st.base,
            "product": st.product,
            "base_weight_g": round(st.base_wt),
            "rebalanced": st.rebalanced,
            "started_at": st.started_at,
            "water_ratio": st.water_ratio,
            "production_day": self.daily.production_day(),
            "steps": [{"name": s.name,
                       "target_g": round(s.target, 2),
                       "actual_g": None if s.actual is None else round(s.actual, 2),
                       "skipped": s.skipped,
                       "weighed_on": s.scale,
                       "assumed": s.assumed,
                       "witness_g": None if s.witness_g is None else round(s.witness_g, 1),
                       "verified": s.verified} for s in st.steps],
            "reconciliation": self.reconcile(),
        })
        self.refresh_consumption()

    def refresh_consumption(self):
        """Rebuild the Excel consumption view from the batch log.

        Best effort and never on the critical path — the JSONL is the record,
        this is only the view of it, so a failure here must not lose a batch.
        """
        try:
            import consumption
            consumption.build(self.batches.path,
                              os.path.join(os.path.dirname(self.batches.path),
                                           "consumption.xlsx"))
        except Exception:
            pass

    def show_batch_log(self):
        self._dialog(BatchLogDialog(self, self.batches.recent()))

    def quit_app(self):
        if self.ask_pin("Enter supervisor PIN to leave the station app."):
            self.close()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            e.ignore()          # no accidental exit from a fullscreen station
        else:
            super().keyPressEvent(e)
