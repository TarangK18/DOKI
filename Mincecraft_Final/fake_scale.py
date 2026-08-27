#!/usr/bin/env python3
"""A scale that isn't there — streams real RS232 frames over a virtual port.

  python3 fake_scale.py
      it prints the virtual port it created, e.g. /dev/pts/3
  python3 station.py --port <that port> --windowed
      in a second terminal

Why bother, when `station.py --sim` already exists: `--sim` bypasses the serial
code entirely, so it proves the panel works but proves nothing about the
reader, the parser, the de-duplication or the reconnect logic. This streams
genuine `+NNN.NNN kg\\r\\n` frames — doubled, at 10 Hz, with dither — down a
real serial device, so everything from `parse_frame` upward runs exactly as it
will on the bench.

Linux and macOS only: it needs a pty. On Windows use `station.py --demo`.

Type commands while it runs:

  3200        set the weight to 3200 g
  +50  -50    add or remove
  pour        ramp up continuously until you press enter again
  lift        take the tub off (tests the drop alarm)
  zero        empty the scale
  noise 0     make it perfectly steady; noise 4 is the default dither
  drop        stop sending frames, to watch the panel go STALE
  resume      start sending again
  quit
"""

import os
import pty
import random
import sys
import threading
import time

DIVISION_G = 5.0
FRAME_HZ = 10


class FakeScale:
    def __init__(self):
        self.true_g = 0.0
        self.lifted = None
        self.noise = 4.0
        self.sending = True
        self.pouring = False
        self.stop = threading.Event()
        self.lock = threading.Lock()

    def frame(self):
        """The scale's actual output format, quantised and dithered."""
        with self.lock:
            true_g = self.true_g
            noise = self.noise
        jitter = (random.random() - 0.5) * noise if true_g > 0 else 0.0
        grams = max(0.0, round((true_g + jitter) / DIVISION_G) * DIVISION_G)
        kg = grams / 1000.0
        sign = "+" if kg >= 0 else "-"
        return f"{sign}{abs(kg):07.3f} kg\r\n".encode()

    def run(self, fd):
        ramp = 0
        while not self.stop.is_set():
            with self.lock:
                pouring = self.pouring
            if pouring:
                ramp += 1
                self.add(min(2 + ramp * 1.6, 28))
            else:
                ramp = 0
            if self.sending:
                frame = self.frame()
                try:
                    os.write(fd, frame)      # the real scale sends each twice
                    os.write(fd, frame)
                except OSError:
                    return
            time.sleep(1.0 / FRAME_HZ)

    # -- commands ----------------------------------------------------------

    def add(self, grams):
        with self.lock:
            if self.lifted is None:
                self.true_g = max(0.0, self.true_g + grams)

    def set(self, grams):
        with self.lock:
            if self.lifted is None:
                self.true_g = max(0.0, grams)

    def lift(self):
        with self.lock:
            if self.lifted is None:
                self.lifted, self.true_g = self.true_g, 0.0
                return "tub lifted off"
            self.true_g, self.lifted = self.lifted, None
            return "tub back on"

    def reading(self):
        with self.lock:
            return self.true_g


def main():
    scale = FakeScale()
    master, slave = pty.openpty()
    name = os.ttyname(slave)

    print(__doc__.split("Type commands")[0].rstrip())
    print(f"\nvirtual port: {name}")
    print(f"run:          python3 station.py --port {name} --windowed\n")
    print("commands: <grams> | +N | -N | pour | lift | zero | noise N | "
          "drop | resume | quit\n")

    threading.Thread(target=scale.run, args=(master,), daemon=True).start()

    try:
        for line in sys.stdin:
            cmd = line.strip().lower()
            if not cmd:
                if scale.pouring:
                    scale.pouring = False
                    print(f"    stopped at {scale.reading():.0f} g")
                continue
            if cmd in ("quit", "exit", "q"):
                break
            elif cmd == "pour":
                scale.pouring = True
                print("    pouring — press enter to stop")
            elif cmd == "lift":
                print("   ", scale.lift())
            elif cmd == "zero":
                scale.set(0)
                print("    zeroed")
            elif cmd == "drop":
                scale.sending = False
                print("    frames stopped — the panel should go STALE, then "
                      "dim the reading")
            elif cmd == "resume":
                scale.sending = True
                print("    frames resumed")
            elif cmd.startswith("noise"):
                try:
                    scale.noise = float(cmd.split()[1])
                    print(f"    dither {scale.noise} g")
                except (IndexError, ValueError):
                    print("    usage: noise 4")
            elif cmd[0] in "+-":
                try:
                    scale.add(float(cmd))
                    print(f"    {scale.reading():.0f} g")
                except ValueError:
                    print("    ?")
            else:
                try:
                    scale.set(float(cmd))
                    print(f"    {scale.reading():.0f} g")
                except ValueError:
                    print("    ?")
    except KeyboardInterrupt:
        pass
    finally:
        scale.stop.set()
        os.close(master)
        os.close(slave)
    return 0


if __name__ == "__main__":
    sys.exit(main())
