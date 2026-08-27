# DOKI weighing station — Raspberry Pi app

Native PyQt5 application for the Pi. The real hardware behind the MINCECRAFT
panel simulator, which describes itself as a "UX reference for firmware" —
this is that firmware.

```
scale --RS232--> USB/FTDI --> /dev/ttyUSB0 --> reader thread --> Qt panel
```

One process. No server, no browser, no network.

## Run

```bash
sudo apt install python3-pyqt5 python3-serial
sudo usermod -aG dialout pi        # so the app can open /dev/ttyUSB0, then log out and back in

python3 station.py                 # real scale, fullscreen
python3 station.py --demo          # no hardware at all — start here
python3 station.py --sim           # simulated scale, fullscreen
python3 station.py --windowed      # windowed, for development
python3 station.py --port /dev/ttyUSB1 --baud 9600
```

Start it automatically on boot:

```bash
mkdir -p ~/.config/autostart
cp mincecraft.desktop ~/.config/autostart/
```

An autostart entry is used rather than a systemd unit deliberately.
Raspberry Pi OS Bookworm runs Wayland on the Pi 4 and 5, so a systemd unit has
to guess at `DISPLAY`/`WAYLAND_DISPLAY` and usually ends up fighting the
compositor; a desktop autostart entry inherits the right session environment
for free. For a console-only boot with no desktop, run it under
`QT_QPA_PLATFORM=eglfs` from a systemd unit instead.

## Files

| file | purpose |
|---|---|
| `scale.py` | the scale: parsing, de-dup, stability, reader, batch log, config. No Qt. |
| `panel.py` | the whole UI: screens, dialogs, custom widgets |
| `station.py` | entry point — arg parsing, wiring, fullscreen launch |
| `style.qss` | MINCECRAFT palette; `{N}px` sizes scale with the display |
| `recipes.json` | products, percentages, tolerances, scales, PIN |
| `recipe_data.py` | the recipes as supplied, in g per 10 kg — the source both `recipes.json` and the workbook are built from |
| `batches.jsonl` | one JSON line per completed batch (created on first batch) |
| `consumption.py` | rebuilds `consumption.xlsx` from the batch log |
| `check_scale.py` | measures a real scale: frame format, division, dither |
| `fake_scale.py` | virtual serial port streaming real frames, for testing with no scale |
| `run-demo.bat` / `run-demo.sh` | one-click demo launcher (Windows / Linux) |
| `daily.json` | the day's water ratio (created when a supervisor sets it) |
| `tests/test_scale.py` | 75 tests, incl. a virtual serial port and the entry point |
| `tests/test_panel.py` | 91-check headless walkthrough of a whole batch |
| `tests/test_fake_scale.py` | 10 tests driving the real reader over a virtual port |

`scale.py` imports nothing from `panel.py` and knows nothing about Qt. That
boundary is the point: the logic that decides what the weight *is* stays
testable without a display, the same way the cook state machine in
`doki_probe.ino` is plain C++ so the harness can lift it out verbatim.

## Running it with no scale

**`python3 station.py --demo`** is the one to reach for — or double-click
`run-demo.bat` on Windows, `run-demo.sh` on the Pi. It implies `--sim` and
`--windowed`, pre-sets today's water ratio so the supervisor gate does not stop
you before you have seen anything, and points the batch log and daily file at a
scratch directory so a demo never lands in the real records.

That gives you a **rig strip** along the bottom standing in for a scale, a tub
and a pair of hands:

| control | what it is for |
|---|---|
| **▼ POUR** (hold) | pours at a rate that *ramps* the longer you hold — which is what makes the tolerance guidance worth testing. A constant trickle would never overshoot, and overshooting is the case the panel exists to handle. |
| **▲ SCOOP** (hold) | takes weight back off, for the over-target path |
| +1 +5 +50 +500 −5 −50 | nudges, for landing on a number deliberately |
| **→ TARGET** | jumps to exactly what the current screen is waiting for. Walking a seven-ingredient recipe by hand is fine once; this is for the fifth run-through. |
| **LIFT TUB** | takes the container off, to rehearse the container-removed alarm |
| **EMPTY** | back to zero |

The strip's left-hand readout shows the rig's *true* weight and how far it is
from target — next to which the panel shows what the scale actually reads, so
you can watch quantisation and dither at work.

Runs on Windows as well as the Pi; nothing in `--demo` needs a serial port.

## With the scale on the bench

Before trusting anything else, measure it:

```bash
python3 check_scale.py                 # /dev/ttyUSB0, 20 s
python3 check_scale.py --seconds 30 --port /dev/ttyUSB1
```

Run it twice — once with nothing on the scale, once with a steady weight —
and leave it alone while it runs. It reports the frame format, the smallest
step the scale actually reads, how much a settled reading dithers, the frame
rate and the duplicate rate, then says what to put in `recipes.json`.

Three numbers in this app were taken on trust and this is what confirms them:
`scales.main.division_g`, the stability band, and whether the scale ever emits
anything other than the `+NNN.NNN kg` frame. It measures dither from quiet
1.5 s windows rather than the whole run, so changing the weight partway
through does not spoil the figure.

Then run the station against the real port:

```bash
python3 station.py --port /dev/ttyUSB0 --windowed
```

### Exercising the real serial path

`--sim` bypasses the serial code, so it proves the panel works and nothing
about the reader. For that, `fake_scale.py` creates a **virtual serial port**
and streams genuine `+NNN.NNN kg` frames down it — doubled, at 10 Hz, with
dither — so the parser, de-duplication, staleness and reconnect logic all run
exactly as they will on the bench:

```bash
python3 fake_scale.py                       # prints the port it created
python3 station.py --port /dev/pts/3 --windowed    # in another terminal
```

Type at it while it runs: `3200`, `+50`, `pour`, `lift`, `zero`, `noise 0`,
`drop` (stop sending, to watch the panel go STALE), `resume`, `quit`.

Linux and macOS only — it needs a pty. On Windows use `--demo`.

## How the reading is handled

The scale streams continuously at 9600 8N1 and **sends every reading twice**.
Three concerns come out of that one stream, and they deliberately use
different inputs:

- **De-duplication** applies to what is *recorded*. Consecutive identical
  values are dropped, so the log is a list of changes, not a 10 Hz tape.
- **Stability** is judged on the *raw* stream, before de-duplication. A run of
  identical frames is exactly what a settled scale looks like — dropping those
  frames would destroy the evidence that it settled. Stable means at least
  8 samples in the last 1.5 s spanning ≤ 5 g.
- **Liveness** comes from the last frame of *any* kind, duplicates included.
  A held weight sends nothing new for minutes, and that must read as alive.

The reader owns the serial port on its own thread; the panel polls
`ScaleState.snapshot()` every 100 ms and never touches the port. Staleness is
computed when the snapshot is taken, so a scale that has gone silent is
noticed by the reader of the state rather than having to announce itself.

No reading older than 2 s is shown as current. The panel dims the number to a
dash and disables the actions instead — the same discipline as the cook alarm
board: a confident stale number is worse than admitting the reading is gone.
Unplugging the USB converter is handled the same way, and the reader
reconnects on its own every 2 s until the port comes back.

## What the panel does

1. **START BATCH** → pick the finished product. Eight of them, the vinegar
   bath among them. The meat is not asked: each product carries its own in
   `recipes.json`, so there is one decision at the start of a batch, not two.
2. **Weigh the meat** — CAPTURE arms only when the reading is live, stable and
   above that recipe's own minimum batch (see below).
3. **Review** the recipe, then add ingredients one at a time.

A recipe with no ingredients yet shows on the product screen but cannot be
selected, and the screen says which ones and where to fill them in. An empty
recipe reaching the floor is worse than a greyed-out button.

**An ingredient listed at zero is shown but not weighed.** Liquid smoke sits
at 0 in Teriyaki and Pepper. It stays on the review list under *"listed but not
weighed"* so nobody wonders whether it was forgotten — but it is not a step,
because a zero-weight step has a tolerance wider than its target and would
refuse to start the batch. A reminder should not stop the line.

**Each recipe has its own minimum batch size.** The smallest ingredient sets
it: bhut jholokia at 0.025 % of the meat does not reach two divisions of the
bench scale until the batch is 800 g, so a 500 g Teriyaki batch is not a
tolerance problem — it is unmakeable. The capture screen says so at the scale
rather than letting it fail at recipe review.

Each ingredient step takes a **software tare** at the moment it opens, so the
operator adds cumulatively into one container and the panel shows only what
the current ingredient has contributed. In tolerance and stable for 2 s
auto-advances. Over target offers scoop-out, rebalancing the remaining
ingredients, or a PIN'd supervisor accept. A drop of more than
`drop_alarm_g` below the step's tare raises the container-removed alarm, and
RESUME only arms once the weight is actually back — the panel does not take
the operator's word for it.

Completed batches append to `batches.jsonl` with target, actual and deviation
per ingredient. Supervisor menu → *Batch log* reads them back.

The station is a kiosk: fullscreen, no cursor, Escape ignored. **Menu → Exit
to desktop, behind the supervisor PIN**, is the only way out.

## Screens scale with the monitor

The design target is 1024×600. Every size in `style.qss` is written as
`{N}px` and multiplied by `min(width/1024, height/600)` at startup and on
resize, so a 1920×1080 monitor gets proportionally larger type instead of a
small panel in the corner.

## Verify

```bash
python3 tests/test_scale.py                              # 75 tests
QT_QPA_PLATFORM=offscreen python3 tests/test_panel.py    # 91 checks + screenshots
python3 tests/test_fake_scale.py                         # 10 over a virtual port
```

The panel test drives the real app in simulation: a complete batch from base
through capture, product, review and all five ingredients auto-advancing in
tolerance, the batch record verified in the JSONL, then the container-removal
alarm, the over-target prompt, the PIN pad, feed loss, and the layout at both
1024×600 and 1920×1080. Screenshots land in `screenshots/`.

## The daily water ratio

How much water a batch needs depends on the flour in use that day, so it cannot
be a recipe percentage. A supervisor sets **water ÷ flour** once per production
day, behind the PIN, and **no batch can start until they have** — the home
screen says which of the two locks (no scale, no ratio) is holding.

Water target = `ratio × flour target`. The recipe's own water figure stays in
the workbook as the *nominal*, used only for the sanity check. A recipe is
gated only if it names both a `flour_ingredient` and a `water_ingredient`;
others run unchanged.

Two guards, because a mistyped ratio is the obvious failure mode:

- **Hard bounds** (`water.ratio_min` / `max`, default 0.2–1.5) — `5.5` typed
  for `0.55` is refused outright.
- **Off-nominal warning** — a ratio more than 25 % from what the recipe implies
  is flagged before saving. That catches `0.75` for `0.55`, which no bound can.

The entry screen shows the previous day's value and, live as they type, the
grams of water it implies for each recipe — so a wrong number is visible before
it is committed rather than after.

The lock only bites when a recipe actually derives its water from flour.
Teriyaki and the vinegar bath use no flour, so today nothing is held for a
ratio nobody uses — the home screen says as much. It will lock once a flour
recipe is entered.

**Expiry is at each new production day.** A batch already running is not
interrupted: the ratio is captured into the batch when its targets are computed,
so a batch begun at 23:55 finishes on the ratio it started with and only the
*next* one is blocked. `water.day_start_hour` (default 0) moves the rollover if
a night shift runs past midnight, so the lock lands between shifts.

Every batch record carries the `water_ratio` and `production_day` it used.

## The order of work, and the pick list

Every target is computed the moment the meat is weighed — not one at a time —
and the review screen shows the whole plan before any pouring starts, grouped
into a **Floor scale** section and a **Bench scale** section with counts and
subtotals. Floor-scale ingredients are worked first, then bench, so the
operator makes one trip rather than walking between the two scales for every
ingredient. Recipe order is kept within each group.

An over-target ingredient that triggers a rebalance moves the later targets, so
the list is redrawn rather than left showing numbers that no longer apply.

## Consumption log

`batches.jsonl` is the record — append-only, one line per batch, which is what
survives a Pi losing power mid-write. `consumption.xlsx` is a *view* of it,
rebuilt from scratch after every batch:

- **Consumption** — one row per ingredient per batch, which is the grain an
  inventory system needs.
- **Batches** — one row per batch with totals and the reconciliation result.
- **By ingredient** — total consumed per ingredient per month.

Nothing ever writes to the workbook directly, so it cannot drift from the log,
and deleting it costs nothing: `python3 consumption.py` brings it back correct.

## Two scales

The floor scale reads and errs to **1 g**. Two of those have to fit inside the
recipe's 2 % before it can be said to be enforcing anything, so it can only
hold that tolerance above **100 g** — derived as `2 × division ÷ percent`, not
picked. Everything below that goes on the bench scale. Confirm the division
with `check_scale.py` before trusting the split.

That crossover is the fix for a real defect. With one scale, 58 g of salt got
a ±10 g window, and anything under 10 g had a tolerance *wider than its own
target* — meaning zero was inside tolerance and an operator who added nothing
would pass the step. Routed to the bench scale, the same salt is held to
±1.15 g, which is what the recipe asked for all along.

`scales.crossover_g` overrides the derived value. Lowering it keeps more
ingredients on the floor scale — easier to pour into, looser tolerance — and
recipe review marks in amber exactly which ingredients are then held to the
scale's resolution rather than the recipe's percentage. Nothing is degraded
silently.

If neither scale can weigh an ingredient, review says so and refuses to start
the batch. A `dead_zone` appears if the bench scale's usable capacity stops
below the floor scale's crossover; it is reported rather than discovered
mid-batch.

### The bench scale is not wired to the Pi

There is **no keypad**. A typed number is only ever a claim about what the bench
scale showed, and the Pi has no way to check it, so the station does not ask for
one. The operator weighs the ingredient on the bench, tips it into the tub, and
**the floor scale sees it arrive** — that is the only figure the Pi can vouch
for, and it drives the same tolerance bar as a floor-scale step, so the addition
is visible as it happens rather than confirmed blind.

What gets recorded is the **recipe target**, flagged `assumed` rather than
`measured`. The screen says so while the operator is standing there: *"The
recorded weight will be the 148.5 g target, not this."* The consumption workbook
carries a **Measured?** column so inventory can tell a weighed figure from an
assumed one and never treats the two as the same evidence.

The floor scale still acts as a witness:

- Above 2 g (two floor-scale divisions) it compares what it saw arrive against
  the target and challenges a mismatch behind the PIN. It cannot be fooled by an
  ingredient that was never added.
- Below that it says so plainly — "below what the Floor scale can see, so
  nothing here can be cross-checked" — and logs the step unverified rather than
  implying a check that never happened.
- At the end of the batch the **total** reconciles: individually a 2 g spice is
  invisible, collectively the bench-weighed ingredients are not. It will not
  say which entry was wrong, but it will say that one was.

Every batch record carries `weighed_on`, `assumed`, `witness_g` and `verified`
per ingredient, plus the batch reconciliation.

## Open — needs a human decision

**The bench scale's spec is a guess.** `recipes.json` assumes 3 kg × 0.1 g.
Check the label and correct `division_g`, `capacity_g` and `usable_g`
(capacity minus the tare of whatever container sits on it) before this goes on
the floor — the crossover and every small-ingredient tolerance depend on them.

Also still open:

- Supervisor PIN is `1234` in plain text in `recipes.json`. Going fully local
  removed the network exposure, not the file.
- Whether completed batches should also reach Supabase or MQTT. The batch
  record is already a clean JSON document, so this is one POST away — but it
  would reintroduce the network this rewrite removed.
- Handover to the Schedule 1 mixing station. On the Pi that is a real station,
  not a sibling page, so the simulator's SEND TO MIXING STATION button was
  dropped rather than faked.

## Nothing has run on real hardware yet

Everything above was verified against a virtual serial port and a simulated
scale. The frame format, baud and parser come from your sniffer output, so the
first bench run is mainly about confirming the scale's division and whether it
ever sends anything other than the `+NNN.NNN kg` frame.
