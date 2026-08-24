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

from scale import fmt, fmt_g

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

    def reset(self):
        self.base = None
        self.product = None
        self.base_wt = 0.0
        self.steps = []
        self.idx = 0
        self.step_zero = 0.0
        self.rebalanced = False
        self.started_at = None


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
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
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
        self.prompt = label("Ready. Put the empty container on the scale "
                            "and press START BATCH.", "prompt", Qt.AlignCenter, wrap=True)
        self.box.addWidget(self.prompt)
        self.box.addStretch(1)
        self.start_btn = button("START BATCH", "primary", self.panel.start_batch)
        self.action_row((button("MENU", "ghost", self.panel.open_menu), 0),
                        (self.start_btn, 1))

    def tick(self, snap, live):
        self.live.update_from(snap["grams"], live, snap["stable"])
        self.start_btn.setEnabled(live)
        if not live:
            self.prompt.setText("Waiting for the scale…")
        else:
            self.prompt.setText("Ready. Put the empty container on the scale "
                                "and press START BATCH.")


class BaseScreen(Screen):
    def build(self):
        self.box.addWidget(label("Step 1 of 4 — <b>Select base ingredient</b>", "crumb"))
        grid = QGridLayout()
        grid.setSpacing(int(10 * self.panel.scale))
        for i, b in enumerate(self.cfg.bases):
            btn = button(f"{b['icon']}  {b['name']}", "primary",
                         lambda _, bid=b["id"]: self.panel.pick_base(bid))
            btn.setMinimumHeight(int(64 * self.panel.scale))
            grid.addWidget(btn, i // 2, i % 2)
        self.box.addLayout(grid)
        self.box.addStretch(1)
        self.action_row((button("◀ BACK", "ghost", self.panel.go_home), 0))


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
        self.action_row((button("◀ BACK", "ghost", lambda: self.panel.show_screen("BASE")), 0),
                        (self.cap_btn, 1))

    def enter(self):
        self.crumb.setText("Step 2 of 4 — <b>Weigh base: "
                           f"{self.cfg.base_name(self.st.base)}</b>")

    def tick(self, snap, live):
        self.live.update_from(snap["grams"], live, snap["stable"])
        ok = live and snap["stable"] and snap["grams"] >= self.cfg.min_base_g
        self.cap_btn.setEnabled(ok)
        if not live:
            self.hint.setText("Waiting for the scale…")
        elif ok:
            self.hint.setText(f"Stable: {snap['grams']:.0f} g — press CAPTURE")
        elif snap["grams"] < self.cfg.min_base_g:
            self.hint.setText(f"Put the {self.cfg.base_name(self.st.base)} "
                              "tub in the container…")
        else:
            self.hint.setText("Stabilising…")


class ProductScreen(Screen):
    def build(self):
        self.crumb = label("", "crumb")
        self.box.addWidget(self.crumb)
        self.grid = QGridLayout()
        self.grid.setSpacing(int(10 * self.panel.scale))
        self.box.addLayout(self.grid)
        self.box.addStretch(1)
        self.action_row((button("◀ BACK", "ghost", lambda: self.panel.show_screen("CAPTURE")), 0))

    def enter(self):
        self.crumb.setText(
            f"Step 3 of 4 — <b>Select product / flavour</b> · "
            f"{self.cfg.base_name(self.st.base)} {fmt(self.st.base_wt)}")
        while self.grid.count():
            self.grid.takeAt(0).widget().deleteLater()
        for i, p in enumerate(self.cfg.products_for(self.st.base)):
            btn = button(p["name"], "primary",
                         lambda _, pid=p["id"]: self.panel.pick_product(pid))
            btn.setMinimumHeight(int(64 * self.panel.scale))
            self.grid.addWidget(btn, i // 2, i % 2)


class ReviewScreen(Screen):
    def build(self):
        self.crumb = label("", "crumb")
        self.box.addWidget(self.crumb)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "Ingredient", "Target", "Tol."])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.box.addWidget(self.table, 1)
        self.action_row((button("◀ BACK", "ghost", lambda: self.panel.show_screen("PRODUCT")), 0),
                        (button("START ADDING ▶", "good", self.panel.start_adding), 1))

    def enter(self):
        rb = " · <span style='color:#e8b339'>REBALANCED</span>" if self.st.rebalanced else ""
        self.crumb.setText(f"Step 4 of 4 — <b>Recipe review</b> · "
                           f"{self.cfg.product_name(self.st.product)}{rb}")
        steps = self.st.steps
        self.table.setRowCount(len(steps) + 1)
        for i, s in enumerate(steps):
            tick = " ✓" if s.actual is not None else ""
            for c, text in enumerate([str(i + 1), s.name + tick,
                                      f"{fmt_g(s.target)} g",
                                      f"± {fmt_g(self.cfg.tol_of(s.target))} g"]):
                item = QTableWidgetItem(text)
                if c >= 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(i, c, item)
        total = self.st.base_wt + sum(s.target for s in steps)
        for c, text in enumerate(["", "Total batch", fmt(total), ""]):
            item = QTableWidgetItem(text)
            f = item.font(); f.setBold(True); item.setFont(f)
            if c >= 2:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(len(steps), c, item)


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
        self.base_line.setText(f"{cfg.base_icon(st.base)} "
                               f"{cfg.base_name(st.base)} {fmt(st.base_wt)}")
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


class DoneScreen(Screen):
    def build(self):
        self.head = label("", "doneHead")
        self.box.addWidget(self.head)
        self.stars = label("", "stars", Qt.AlignCenter)
        self.box.addWidget(self.stars)
        self.msg = label("", "prompt", Qt.AlignCenter)
        self.box.addWidget(self.msg)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["#", "Ingredient", "Target", "Actual", "Dev."])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
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
            for c, text in enumerate([str(i + 1), s.name, f"{fmt_g(s.target)} g",
                                      actual, dev_text]):
                item = QTableWidgetItem(text)
                if c >= 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 4:
                    item.setForeground(QColor(colour))
                self.table.setItem(i, c, item)


class MenuScreen(Screen):
    def build(self):
        self.box.addWidget(label("<b>Menu</b> (supervisor)", "crumb"))
        grid = QGridLayout()
        grid.setSpacing(int(10 * self.panel.scale))
        entries = [("Recipe editor", None), ("Scale calibration", None),
                   ("Manual tare", None), ("Batch log", self.panel.show_batch_log)]
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
    SCREENS = {"HOME": HomeScreen, "BASE": BaseScreen, "CAPTURE": CaptureScreen,
               "PRODUCT": ProductScreen, "REVIEW": ReviewScreen, "ADD": AddScreen,
               "DONE": DoneScreen, "MENU": MenuScreen}

    def __init__(self, state, cfg, batches, sim=None):
        super().__init__()
        self.state = state
        self.cfg = cfg
        self.batches = batches
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
        bar = QFrame(); bar.setObjectName("simbar")
        hb = QHBoxLayout(bar)
        hb.setContentsMargins(8, 6, 8, 6)
        hb.addStretch(1)
        hb.addWidget(label("SIMULATED SCALE"))
        for text, grams in [("+5 g", 5), ("+50 g", 50), ("+500 g", 500),
                            ("+3 kg", 3000), ("−50 g", -50), ("−500 g", -500)]:
            hb.addWidget(button(text, None, lambda _, g=grams: self.sim.add(g)))
        hb.addWidget(button("EMPTY", None, self.sim.zero))
        hb.addStretch(1)
        return bar

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
        if st.base:
            prod = self.cfg.product_name(st.product) if st.product else "—"
            base = fmt(st.base_wt) if st.base_wt else "—"
            self.info_lbl.setText(
                f"{self.cfg.base_name(st.base)} · {prod} · base {base}")
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
        self.st.started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.show_screen("BASE")

    def go_home(self):
        self.st.reset()
        self.show_screen("HOME")

    def open_menu(self):
        if self.ask_pin("Enter supervisor PIN to open the menu."):
            self.show_screen("MENU")

    def pick_base(self, base_id):
        self.st.base = base_id
        self.show_screen("CAPTURE")

    def capture_base(self):
        snap = self.state.snapshot()
        if not snap["fresh"] or snap["grams"] is None:
            return
        self.st.base_wt = snap["grams"]
        self.show_screen("PRODUCT")

    def pick_product(self, product_id):
        self.st.product = product_id
        self.st.rebalanced = False
        p = self.cfg.product(product_id)
        self.st.steps = [Step(name=n, pct=pct, target=self.st.base_wt * pct / 100)
                         for n, pct in p["ingredients"]]
        self.show_screen("REVIEW")

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
        self.show_screen("ADD")

    def current_added(self):
        snap = self.state.snapshot()
        if snap["grams"] is None:
            return 0.0
        return max(0.0, snap["grams"] - self.st.step_zero)

    def accept_step(self):
        self.st.steps[self.st.idx].actual = self.current_added()
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
            self.show_screen("REVIEW")
        elif result == OverTargetDialog.ACCEPT:
            if self.ask_pin("Accept over-target amount (will be logged)."):
                self.accept_step()
            else:
                self.screens["ADD"].over_prompted = False
        else:
            self.screens["ADD"].over_prompted = False

    # -- output ------------------------------------------------------------

    def record_batch(self):
        st = self.st
        self.batches.append({
            "batch_no": st.batch_no,
            "base": st.base,
            "product": st.product,
            "base_weight_g": round(st.base_wt),
            "rebalanced": st.rebalanced,
            "started_at": st.started_at,
            "steps": [{"name": s.name, "target_g": round(s.target),
                       "actual_g": None if s.actual is None else round(s.actual),
                       "skipped": s.skipped} for s in st.steps],
        })

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
