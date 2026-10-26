"""Read UART logs without asserting reset lines or sending data to the board."""
import argparse
import re
import time
from pathlib import Path
import serial

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--port", default="COM9")
parser.add_argument("--seconds", type=float, default=20)
parser.add_argument("--output", type=Path)
args = parser.parse_args()
uart = serial.Serial(port=None, baudrate=115200, timeout=0.25)
uart.dtr = False
uart.rts = False
uart.port = args.port
uart.open()
deadline = time.monotonic() + args.seconds
parts = []
try:
    while time.monotonic() < deadline:
        data = uart.read(uart.in_waiting or 1)
        if data:
            text = data.decode("utf-8", errors="replace")
            parts.append(text)
            print(text, end="", flush=True)
finally:
    uart.close()
    if args.output:
        args.output.write_text("".join(parts), encoding="utf-8")
