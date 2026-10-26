"""Send an application image over HTTP and verify the rebooted version.

Uses only Python's standard library. Credentials are read locally from config.h.
"""
import argparse
import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def credentials():
    text = (ROOT / "simple_ota" / "config.h").read_text(encoding="utf-8-sig")
    def value(key):
        match = re.search(r'^#define\s+' + key + r'\s+("(?:\\.|[^"\\])*")', text, re.M)
        if not match:
            raise RuntimeError(f"Missing {key} in config.h")
        return json.loads(match.group(1))
    return value("OTA_USERNAME"), value("OTA_PASSWORD")


class Device:
    def __init__(self, address):
        self.base = "http://" + address.removeprefix("http://").rstrip("/")
        user, password = credentials()
        self.authorization = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        # A local device should be reached directly, even if the PC uses a proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, data=None, headers=None, authenticated=True):
        fields = {"Origin": self.base}
        if authenticated:
            fields["Authorization"] = self.authorization
        fields.update(headers or {})
        request = urllib.request.Request(self.base + path, data=data, headers=fields)
        try:
            with self.opener.open(request, timeout=180 if data else 5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def status(self):
        code, body = self.request("/api/status")
        if code != 200:
            raise RuntimeError(f"Status HTTP {code}: {body.decode(errors='replace')}")
        return json.loads(body)

    def upload(self, image, token, declared_size=None, authenticated=True):
        boundary = "----ESP32S3OTADemoBoundary"
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="firmware"; filename="firmware.bin"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n').encode()
        body += image + f"\r\n--{boundary}--\r\n".encode()
        query = urllib.parse.urlencode({"token": token, "size": len(image) if declared_size is None else declared_size})
        return self.request("/update?" + query, body,
                            {"Content-Type": "multipart/form-data; boundary=" + boundary}, authenticated)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address", help="ESP32 IP address or esp32s3-ota.local")
    parser.add_argument("firmware", type=Path)
    parser.add_argument("--expect", help="Expected running version after reboot, e.g. 2.0.0")
    args = parser.parse_args()
    image = args.firmware.read_bytes()
    if not image or image[0] != 0xE9:
        parser.error("Not an ESP application image")
    device = Device(args.address)
    before = device.status()
    print("Before:", json.dumps(before))
    code, body = device.upload(image, before["boot_token"])
    print(f"Upload HTTP {code}: {body.decode(errors='replace')}")
    if code != 200:
        raise SystemExit(1)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            after = device.status()
        except (OSError, ValueError, RuntimeError):
            continue
        if after["boot_token"] == before["boot_token"]:
            continue
        if after["partition"] == before["partition"]:
            raise SystemExit("Device rebooted but did not switch OTA partitions")
        if args.expect and after["version"] != args.expect:
            raise SystemExit("Unexpected running version: " + after["version"])
        print("After:", json.dumps(after))
        print("PASS: Wi-Fi upload, reboot, version and partition switch verified.")
        return
    raise SystemExit("Timed out waiting for the rebooted device")


if __name__ == "__main__":
    main()
