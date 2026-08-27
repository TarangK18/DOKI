#!/usr/bin/env bash
# DOKI weighing station — demo with no hardware. Pi, Linux or macOS.
set -e
cd "$(dirname "$0")"

if ! python3 -c "import PyQt5" 2>/dev/null; then
    echo "PyQt5 is missing. On the Pi:  sudo apt install python3-pyqt5"
    echo "Elsewhere:                    python3 -m pip install PyQt5"
    exit 1
fi

echo "Starting the station in demo mode — simulated scale, scratch log files."
exec python3 station.py --demo
