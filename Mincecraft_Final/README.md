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
python3 station.py --sim           # no hardware; on-screen +/- buttons
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
| `recipes.json` | bases, products, percentages, tolerances, PIN — edit this |
| `batches.jsonl` | one JSON line per completed batch (created on first batch) |
| `tests/test_scale.py` | 26 tests, incl. a virtual serial port |
| `tests/test_panel.py` | 33-check headless walkthrough of a whole batch |

`scale.py` imports nothing from `panel.py` and knows nothing about Qt. That
boundary is the point: the logic that decides what the weight *is* stays
testable without a display, the same way the cook state machine in
`doki_probe.ino` is plain C++ so the harness can lift it out verbatim.

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

1. **START BATCH** → pick base meat.
2. **Capture** the base weight — armed only when the reading is live, stable
   and above `min_base_g`.
3. Pick the **product**; ingredient targets are computed as percentages of the
   captured base.
4. **Review** the recipe, then add ingredients one at a time.

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
python3 tests/test_scale.py                              # 26 tests
QT_QPA_PLATFORM=offscreen python3 tests/test_panel.py    # 33 checks + screenshots
```

The panel test drives the real app in simulation: a complete batch from base
through capture, product, review and all five ingredients auto-advancing in
tolerance, the batch record verified in the JSONL, then the container-removal
alarm, the over-target prompt, the PIN pad, feed loss, and the layout at both
1024×600 and 1920×1080. Screenshots land in `screenshots/`.

## Open — needs a human decision

**Tolerances have to respect the scale's resolution.** The simulator used a
`max(2 g, 2 %)` tolerance, which no 5 g-division scale can ever satisfy for a
small ingredient: 1.8 % salt on a 3.2 kg base is a 58 g target with a ±2 g
window, narrower than one division. The floor here is **10 g (two
divisions)** so the flow works, but the correct per-ingredient tolerance is a
recipe and QA question, not a firmware one — the same shape as the cook hold
time. Confirm the scale's actual division (`scale_division_g` in
`recipes.json`) on the bench, then set tolerances against it.

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
