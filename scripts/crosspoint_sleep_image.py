#!/usr/bin/env python3
"""Prepare a CrossPoint BMP, optionally back up, upload, verify, and activate it."""

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
from http.client import HTTPException
import io
import json
import math
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings
import zlib

from PIL import Image, ImageOps, UnidentifiedImageError


PROFILES = {"X3": (528, 792), "X4": (480, 800)}
DEFAULT_HOST = "http://crosspoint.local"
MAX_RESPONSE = 32 * 1024 * 1024
MAX_JSON = 2 * 1024 * 1024
CONNECTION_HELP = (
    "Wake the reader, enable Wi-Fi File Transfer, and keep it on the same network. "
    "Wait for its server to start, then retry; if mDNS fails, use --host with the "
    "address shown on the reader. Check the /files and /settings pages."
)


class SkillError(Exception):
    """An actionable workflow failure safe to show without response dumps."""


class UploadCollision(SkillError):
    """The reader explicitly rejected uploading over an existing sleep.bmp."""


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def normalize_host(value):
    if "://" not in value:
        value = "http://" + value
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
            or (port is not None and not 1 <= port <= 65535)
            or any(char.isspace() or ord(char) < 32 for char in value)
        ):
            raise ValueError
    except ValueError as exc:
        raise SkillError(
            "--host must be an HTTP(S) host/URL, optionally with a port, "
            "but without credentials, a path, a query, or a fragment."
        ) from exc
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))


class DeviceClient:
    def __init__(self, host=DEFAULT_HOST, timeout=15):
        self.host = normalize_host(host)
        if not math.isfinite(timeout) or timeout <= 0:
            raise SkillError("--timeout must be a finite positive number.")
        self.timeout = timeout
        # LAN traffic must not leak through configured system/environment proxies.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirects()
        )

    @staticmethod
    def read_body(response, limit, context):
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise SkillError(f"{context}: response exceeds the safety limit.")
        length = response.headers.get("Content-Length")
        if length is not None:
            if not length.isdecimal() or len(raw) != int(length):
                raise SkillError(f"{context}: truncated/invalid Content-Length.")
        encoding = response.headers.get("Content-Encoding", "").strip().lower()
        if encoding == "gzip":
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
                raw = compressed.read(limit + 1)
            if len(raw) > limit:
                raise SkillError(f"{context}: decoded response is too large.")
        elif encoding not in ("", "identity"):
            raise SkillError(f"{context}: unsupported Content-Encoding.")
        return raw

    def request(self, path, data=None, content_type=None, limit=MAX_RESPONSE):
        method = "GET" if data is None else "POST"
        headers = {"Accept-Encoding": "gzip"}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            self.host + path, data=data, headers=headers, method=method
        )
        # urllib sends a Content-Length for bytes and does not use Expect: 100-continue.
        endpoint = path.split("?")[0]
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise SkillError(f"{method} {endpoint}: HTTP {response.status}; expected 200.")
                return self.read_body(response, limit, f"{method} {endpoint}")
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = b""
            try:
                if not 300 <= status < 400:
                    body = self.read_body(exc, 64 * 1024, f"{method} {endpoint}").strip()
            except (SkillError, OSError, HTTPException, EOFError, zlib.error) as read_error:
                raise SkillError(
                    f"{method} {endpoint}: HTTP {status}; error response unreadable "
                    f"({type(read_error).__name__})."
                ) from read_error
            finally:
                exc.close()
            if method == "POST" and endpoint == "/upload":
                if status == 400 and body == b"File already exists: sleep.bmp":
                    raise UploadCollision("POST /upload: HTTP 400. File already exists: sleep.bmp.") from exc
                safe_errors = {
                    b"Invalid file name", b"Failed to create file on SD card",
                    b"Failed to write to SD card - disk may be full",
                    b"Failed to write final data to SD card", b"Upload aborted",
                    b"Unknown error during upload",
                }
                if body in safe_errors:
                    raise SkillError(f"POST /upload: HTTP {status}. {body.decode('ascii')}.") from exc
            detail = ("Redirect refused; no redirected request was sent." if 300 <= status < 400
                      else "Check firmware API compatibility and Wi-Fi File Transfer.")
            raise SkillError(f"{method} {endpoint}: HTTP {status}. {detail}") from exc
        except (urllib.error.URLError, OSError, HTTPException, EOFError, zlib.error) as exc:
            raise SkillError(
                f"{method} {endpoint}: connection or response failure "
                f"({type(exc).__name__}). {CONNECTION_HELP}"
            ) from exc

    def get_json(self, path):
        raw = self.request(path, limit=MAX_JSON)
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise SkillError(
                f"GET {path.split('?')[0]}: invalid JSON; check firmware API compatibility."
            ) from exc

    def upload(self, data):
        boundary = "crosspoint-" + uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="sleep.bmp"\r\n'
            "Content-Type: image/bmp\r\n\r\n"
        ).encode("ascii") + data + f"\r\n--{boundary}--\r\n".encode("ascii")
        result = self.request(
            "/upload?path=%2F", body, f"multipart/form-data; boundary={boundary}"
        )
        if result.strip() != b"File uploaded successfully: sleep.bmp":
            raise SkillError(
                "Upload returned HTTP 200 without the expected success acknowledgement; "
                "the device may have rejected the file."
            )

    def rename_sleep(self, old_name, backup_name):
        body = urllib.parse.urlencode({"path": "/" + old_name, "name": backup_name}).encode("ascii")
        result = self.request("/rename", body, "application/x-www-form-urlencoded")
        if result.strip() != b"Renamed successfully":
            raise SkillError("Rename returned HTTP 200 without the expected success acknowledgement.")


def screen_size(status, profile=None, size=None):
    if not isinstance(status, dict) or not isinstance(status.get("device"), str):
        raise SkillError("GET /api/status: missing/invalid device field; incompatible API.")
    detected = PROFILES.get(status["device"].strip().upper())
    selected = PROFILES[profile] if profile else size
    if selected and detected and selected != detected:
        raise SkillError("Explicit screen size conflicts with the detected device profile.")
    if selected:
        return selected
    if detected:
        return detected
    raise SkillError("Unknown device: specify --profile X3/X4 or --size WIDTHxHEIGHT.")


def prepare_image(source, size, fit="contain"):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as original:
                if getattr(original, "n_frames", 1) != 1:
                    raise SkillError("Use a single, static image, not an animation/multipage file.")
                oriented = ImageOps.exif_transpose(original)
                rgba = oriented.convert("RGBA")
                white = Image.new("RGBA", rgba.size, "white")
                gray = Image.alpha_composite(white, rgba).convert("L")
                if fit == "stretch":
                    fitted = gray.resize(size, resample=Image.Resampling.LANCZOS)
                elif fit == "contain":
                    fitted = ImageOps.contain(gray, size, method=Image.Resampling.LANCZOS)
                else:
                    raise SkillError("Unknown fit mode; use contain or stretch.")
                canvas = Image.new("RGB", size, "white")
                canvas.paste(
                    fitted.convert("RGB"),
                    ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2),
                )
                encoded = io.BytesIO()
                # RGB BMP output from Pillow is 24-bit BI_RGB (uncompressed).
                canvas.save(encoded, format="BMP")
                return encoded.getvalue()
    except (
        OSError, UnidentifiedImageError, ValueError, SyntaxError,
        Image.DecompressionBombError, Image.DecompressionBombWarning,
    ) as exc:
        raise SkillError(
            f"Cannot decode the input image ({type(exc).__name__}); use a readable, "
            "reasonably sized single-frame image supported by Pillow."
        ) from exc


def sleep_setting(settings):
    if not isinstance(settings, list) or not all(isinstance(item, dict) for item in settings):
        raise SkillError("GET /api/settings: expected a settings array; incompatible API.")
    matches = [item for item in settings if item.get("key") == "sleepScreen"]
    if len(matches) != 1:
        raise SkillError("GET /api/settings: missing/duplicate sleepScreen setting.")
    setting = matches[0]
    options, value = setting.get("options"), setting.get("value")
    if (
        setting.get("type") != "enum"
        or not isinstance(options, list)
        or not options
        or not all(isinstance(option, str) for option in options)
        or type(value) is not int
        or not 0 <= value < len(options)
    ):
        raise SkillError("GET /api/settings: invalid sleepScreen enum schema.")
    custom = [index for index, option in enumerate(options) if option.strip().casefold() == "custom"]
    if len(custom) != 1:
        raise SkillError("GET /api/settings: expected exactly one Custom option.")
    return value, options[value], custom[0]


def existing_sleep_file(files):
    if not isinstance(files, list):
        raise SkillError("GET /api/files: expected a file array; incompatible API.")
    for item in files:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or type(item.get("isDirectory")) is not bool
            or type(item.get("size")) is not int
            or item["size"] < 0
        ):
            raise SkillError("GET /api/files: invalid file entry; cannot safely check overwrites.")
    matches = [item for item in files if item["name"].casefold() == "sleep.bmp"]
    if len(matches) > 1 or (matches and matches[0]["isDirectory"]):
        raise SkillError("Root sleep.bmp is ambiguous or a directory; resolve it in /files first.")
    return matches[0] if matches else None


def private_write(path, data):
    with path.open("xb") as file:
        os.chmod(path, 0o600)
        file.write(data)
        file.flush()
        os.fsync(file.fileno())


def save_recovery(directory, previous, old_data, new_data, device_backup_name):
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    run = directory / name
    run.mkdir(parents=True, mode=0o700)
    if old_data is not None:
        private_write(run / "sleep.bmp", old_data)
    record = {
        "previous_sleepScreen": previous[0],
        "previous_mode": previous[1],
        "backup_file": "sleep.bmp" if old_data is not None else None,
        "backup_sha256": hashlib.sha256(old_data).hexdigest() if old_data is not None else None,
        "prepared_sha256": hashlib.sha256(new_data).hexdigest(),
        "device_backup_candidate": device_backup_name,
    }
    private_write(run / "recovery.json", (json.dumps(record, indent=2) + "\n").encode("utf-8"))
    return run


def apply_image(client, data, overwrite, backup_dir):
    previous = sleep_setting(client.get_json("/api/settings"))
    existing = existing_sleep_file(client.get_json("/api/files?path=%2F"))
    old_data = None
    if existing:
        if not overwrite:
            raise SkillError(
                "Root sleep.bmp already exists. No device changes made. "
                "Obtain overwrite approval, then rerun with --overwrite to back it up and replace it."
            )
        path = urllib.parse.quote("/" + existing["name"], safe="")
        old_data = client.request("/download?path=" + path)
        if len(old_data) != existing["size"]:
            raise SkillError("Existing sleep.bmp download size mismatch; refusing to overwrite it.")
    backup_name = "sleep-backup-" + uuid.uuid4().hex + ".bmp" if existing else None
    recovery = save_recovery(backup_dir, previous, old_data, data, backup_name)
    print(f"Recovery saved: {recovery} (previous sleepScreen={previous[0]})", flush=True)
    backup_state = "No device-side backup rename attempted."
    try:
        try:
            client.upload(data)
        except UploadCollision:
            if old_data is None:
                raise SkillError("Upload target appeared after preflight; no backed-up original to replace.")
            if client.request("/download?path=" + path) != old_data:
                raise SkillError("Existing sleep.bmp changed since backup; refusing to rename it.")
            print(
                "Reader rejected overwrite (HTTP 400: File already exists: sleep.bmp). "
                f"Preserving the original as /{backup_name} before replacement.", flush=True
            )
            backup_state = (
                f"A rename to /{backup_name} was attempted; inspect /files for its actual state."
            )
            client.rename_sleep(existing["name"], backup_name)
            backup_path = urllib.parse.quote("/" + backup_name, safe="")
            if client.request("/download?path=" + backup_path) != old_data:
                raise SkillError("Renamed device backup does not match the original; stopping.")
            backup_state = (
                f"Original image verified at /{backup_name}; root /sleep.bmp may be missing or incomplete."
            )
            print(f"Device backup byte-verified: /{backup_name}", flush=True)
            # Only a confirmed name collision triggers this one compatibility retry.
            client.upload(data)
        readback = client.request("/download?path=%2Fsleep.bmp")
        if readback != data:
            raise SkillError("Uploaded sleep.bmp read-back differs from the prepared BMP.")
    except SkillError as exc:
        raise SkillError(
            f"{exc} Partial state: /sleep.bmp may have changed; no settings update was sent. "
            f"An already-Custom mode may use the changed file. {backup_state} Recovery: {recovery}. "
            "Restore the local backup sleep.bmp through /files if needed; do not delete backups."
        ) from exc
    try:
        response = client.request(
            "/api/settings",
            json.dumps({"sleepScreen": previous[2]}).encode("ascii"),
            "application/json",
        )
        if response.strip() != b"Applied 1 setting(s)":
            raise SkillError("Settings returned HTTP 200 without the expected one-setting acknowledgement.")
        saved_value, _, saved_custom = sleep_setting(client.get_json("/api/settings"))
        if saved_value != saved_custom:
            raise SkillError("Settings read-back did not confirm Custom mode.")
    except SkillError as exc:
        raise SkillError(
            f"{exc} Partial state: /sleep.bmp is byte-verified, but the active mode is uncertain. "
            f"Inspect /settings before retrying. Recovery: {recovery}."
        ) from exc
    print(
        "Uploaded /sleep.bmp with byte-identical read-back; Custom mode confirmed. "
        "A device sleep cycle is required to see it; the physical display has not been verified."
    )


def parse_size(value):
    try:
        width, height = (int(part) for part in value.lower().split("x"))
        if not (1 <= width <= 4096 and 1 <= height <= 4096 and width * height <= 4_000_000):
            raise ValueError
        return width, height
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Use WIDTHxHEIGHT: each dimension 1..4096, at most 4,000,000 pixels."
        ) from exc


def positive_timeout(value):
    try:
        timeout = float(value)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
        return timeout
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Timeout must be a finite positive number.") from exc


def parser():
    cli = argparse.ArgumentParser(
        description="Create a no-crop grayscale RGB BMP. Device writes require --apply.",
        epilog="With --profile or --size and without --apply, preparation is fully offline.",
    )
    cli.add_argument("image", type=Path, help="local single-frame input image")
    cli.add_argument("--output", type=Path, default=Path("output/sleep.bmp"),
                     help="new local BMP path (default: output/sleep.bmp; never overwritten)")
    dimensions = cli.add_mutually_exclusive_group()
    dimensions.add_argument("--profile", type=str.upper, choices=PROFILES, help="explicit X3/X4 profile")
    dimensions.add_argument("--size", type=parse_size, help="explicit WIDTHxHEIGHT for other devices")
    cli.add_argument("--fit", choices=("contain", "stretch"), default="contain",
                     help="contain: preserve proportions with white padding (default); "
                          "stretch: fill without cropping, but distort proportions")
    cli.add_argument("--host", default=DEFAULT_HOST, help="reader host or HTTP(S) base URL")
    cli.add_argument("--timeout", type=positive_timeout, default=15, help="socket timeout in seconds (default: 15)")
    cli.add_argument("--apply", action="store_true", help="explicitly allow upload and Custom activation")
    cli.add_argument("--overwrite", action="store_true", help="approve replacing existing sleep.bmp after backup")
    cli.add_argument("--backup-dir", type=Path, default=Path("backups"),
                     help="private recovery directory (default: backups)")
    return cli


def run(args):
    if args.overwrite and not args.apply:
        raise SkillError("--overwrite requires --apply; no device changes made.")
    if args.output.resolve() == args.image.resolve():
        raise SkillError("Output must not replace the input image.")
    if args.output.exists():
        raise SkillError("Output already exists; choose a new --output path (local files are never overwritten).")
    client = DeviceClient(args.host, args.timeout)
    if args.apply or not (args.profile or args.size):
        size = screen_size(client.get_json("/api/status"), args.profile, args.size)
    else:
        size = PROFILES[args.profile] if args.profile else args.size
    data = prepare_image(args.image, size, args.fit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    private_write(args.output, data)
    print(f"Prepared {size[0]}x{size[1]} uncompressed 24-bit grayscale RGB BMP: {args.output}")
    print(f"SHA-256: {hashlib.sha256(data).hexdigest()}")
    if args.fit == "stretch":
        print("Stretch selected: no added padding/crop; artwork proportions may be distorted.")
    if args.apply:
        apply_image(client, data, args.overwrite, args.backup_dir)
    else:
        print("Preparation only; no device files or settings changed. Use --apply to authorize device writes.")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        run(args)
    except (SkillError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
