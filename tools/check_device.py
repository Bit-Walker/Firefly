"""Exercise real-device OTA rejection paths without changing the running app."""
import argparse
import json
from pathlib import Path
from ota_upload import Device


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address")
    parser.add_argument("firmware", type=Path)
    args = parser.parse_args()
    device = Device(args.address)
    before = device.status()
    token = before["boot_token"]
    image = args.firmware.read_bytes()
    checks = []

    def check(name, actual, expected):
        code, body = actual
        assert code == expected, (name, code, body)
        checks.append({"test": name, "http_status": code, "result": "PASS"})
        print(f"PASS: {name} -> HTTP {code}")

    check("status requires login", device.request("/api/status", authenticated=False), 401)
    check("upload requires login", device.upload(b"invalid", token, authenticated=False), 401)
    check("bad token rejected", device.upload(b"invalid", "wrong-token"), 403)
    check("non-firmware rejected", device.upload(b"not an ESP firmware" * 64, token), 400)
    check("oversized firmware rejected", device.upload(b"invalid", token, before["ota_capacity"] + 1), 400)
    check("incomplete upload rejected", device.upload(image[:512], token, len(image)), 400)
    check("truncated image rejected", device.upload(image[:512], token), 400)
    wrong_chip = bytearray(image)
    wrong_chip[12:14] = b"\x00\x00"  # ESP32 instead of ESP32-S3
    check("wrong chip image rejected", device.upload(bytes(wrong_chip), token), 400)
    after = device.status()
    for field in ("version", "partition", "boot_token"):
        assert before[field] == after[field], (field, before, after)
    print("PASS: original app still running; no reboot or partition switch")
    record = {"before": before, "after": after, "checks": checks}
    output = Path(__file__).resolve().parents[1] / "artifacts" / "rejection-checks.json"
    output.write_text(json.dumps(record, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
