from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
import argparse
import copy
from email import policy
from email.parser import BytesParser
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.parse
import uuid

from PIL import Image, ImageChops, ImageDraw

from scripts import crosspoint_sleep_image as skill


PRIVATE_SENTINEL = "synthetic-private-field-do-not-print"


class FakeReader:
    def __init__(self):
        self.status = {"device": "X3", "version": "1.6.0", "private": PRIVATE_SENTINEL}
        self.settings = [
            {"key": "sleepScreen", "type": "enum", "value": 0,
             "options": ["Dark", "Light", "Custom", "Cover"]},
            {"key": "fontSize", "type": "enum", "value": 1, "options": ["small", "large"]},
            {"key": "password", "type": "string", "value": PRIVATE_SENTINEL},
        ]
        self.files = [{"name": ".sleep", "isDirectory": True, "size": 0}]
        self.image = None
        self.requests = []
        self.failures = {}
        self.gzip = False
        self.corrupt_readback = False
        self.ignore_settings = False
        self.upload_ack = b"File uploaded successfully: sleep.bmp"
        self.settings_ack = b"Applied 1 setting(s)"
        self.after_settings = None
        self.reject_existing_uploads = False
        self.device_backups = {}
        self.change_after_collision = False
        self.corrupt_device_backup = False
        self.fail_replacement = False
        self.rename_ack = b"Renamed successfully"
        self.uploaded = False

    def handle(self, method, path, body, headers):
        self.requests.append((method, path, body, headers))
        if (method, path) in self.failures:
            return self.failures[(method, path)]
        if method == "GET":
            if path == "/api/status":
                return 200, json.dumps(self.status).encode(), {}
            if path == "/api/settings":
                return 200, json.dumps(self.settings).encode(), {}
            if path == "/api/files?path=%2F":
                return 200, json.dumps(self.files).encode(), {}
            if path.startswith("/download?"):
                name = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["path"][0].lstrip("/")
                if name in self.device_backups:
                    data = self.device_backups[name]
                    return 200, data[:-1] if self.corrupt_device_backup else data, {}
            if path.lower() == "/download?path=%2fsleep.bmp":
                if self.image is None:
                    return 404, b"not found", {}
                data = self.image[:-1] if self.corrupt_readback and self.uploaded else self.image
                return 200, data, {}
        if method == "POST" and path == "/upload?path=%2F":
            message = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {headers['Content-Type']}\r\n\r\n".encode() + body
            )
            parts = list(message.iter_parts())
            if (
                len(parts) != 1
                or parts[0].get_param("name", header="content-disposition") != "file"
                or parts[0].get_filename() != "sleep.bmp"
                or parts[0].get_content_type() != "image/bmp"
                or "Expect" in headers
            ):
                return 400, b"invalid multipart", {}
            if self.reject_existing_uploads and self.image is not None:
                if self.change_after_collision:
                    self.image = b"concurrent change"
                return 400, b"File already exists: sleep.bmp", {}
            if self.fail_replacement and self.device_backups:
                return 400, b"Failed to create file on SD card", {}
            self.image = parts[0].get_payload(decode=True)
            self.uploaded = True
            return 200, self.upload_ack, {}
        if method == "POST" and path == "/rename":
            form = urllib.parse.parse_qs(body.decode())
            if (form["path"][0].lower() != "/sleep.bmp"
                    or headers["Content-Type"] != "application/x-www-form-urlencoded"):
                return 400, b"invalid rename", {}
            target = form["name"][0]
            if target in self.device_backups:
                return 409, b"Target already exists", {}
            if self.image is None:
                return 404, b"Item not found", {}
            self.device_backups[target] = self.image
            self.image = None
            return 200, self.rename_ack, {}
        if method == "POST" and path == "/api/settings":
            changes = json.loads(body)
            if list(changes) != ["sleepScreen"] or headers["Content-Type"] != "application/json":
                return 400, b"wrong settings payload", {}
            if not self.ignore_settings:
                self.settings[0]["value"] = changes["sleepScreen"]
            if self.after_settings is not None:
                self.settings = self.after_settings
            return 200, self.settings_ack, {}
        return 404, b"unsupported route", {}


@contextmanager
def fake_server(device):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            status, payload, headers = device.handle(self.command, self.path, body, self.headers)
            if device.gzip:
                payload = gzip.compress(payload)
                headers = {**headers, "Content-Encoding": "gzip"}
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            if "Content-Length" not in headers:
                self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "art.png"

    def save(self, image, **kwargs):
        image.save(self.source, **kwargs)

    def decode(self, data):
        image = Image.open(io.BytesIO(data))
        image.load()
        self.addCleanup(image.close)
        return image

    def test_profiles_bmp_header_grayscale_and_white_padding(self):
        self.save(Image.new("RGB", (100, 300), (230, 50, 30)))
        for profile, size in skill.PROFILES.items():
            with self.subTest(profile=profile):
                data = skill.prepare_image(self.source, size)
                result = self.decode(data)
                self.assertEqual(result.size, size)
                self.assertEqual(result.mode, "RGB")
                self.assertEqual(data[:2], b"BM")
                self.assertEqual(struct.unpack_from("<I", data, 2)[0], len(data))
                self.assertEqual(struct.unpack_from("<ii", data, 18), size)
                self.assertEqual(struct.unpack_from("<H", data, 28)[0], 24)
                self.assertEqual(struct.unpack_from("<I", data, 30)[0], 0)
                r, g, b = result.split()
                self.assertIsNone(ImageChops.difference(r, g).getbbox())
                self.assertIsNone(ImageChops.difference(g, b).getbbox())
                self.assertEqual(result.getpixel((0, size[1] // 2)), (255, 255, 255))
                self.assertEqual(result.getpixel((size[0] - 1, size[1] // 2)), (255, 255, 255))
                self.assertNotEqual(result.getpixel((size[0] // 2, size[1] // 2)), (255, 255, 255))

    def test_all_four_corners_preserved_without_crop_or_stretch(self):
        image = Image.new("L", (60, 120), 128)
        draw = ImageDraw.Draw(image)
        for box, color in [((0, 0, 19, 19), 0), ((40, 0, 59, 19), 50),
                           ((0, 100, 19, 119), 180), ((40, 100, 59, 119), 220)]:
            draw.rectangle(box, fill=color)
        self.save(image)
        result = self.decode(skill.prepare_image(self.source, (120, 120)))
        for point, color in [((35, 5), 0), ((85, 5), 50), ((35, 115), 180), ((85, 115), 220)]:
            self.assertEqual(result.getpixel(point), (color,) * 3)
        self.assertEqual(result.getpixel((29, 60)), (255,) * 3)
        self.assertEqual(result.getpixel((90, 60)), (255,) * 3)

    def test_wide_image_has_top_bottom_padding(self):
        self.save(Image.new("RGB", (120, 60), "black"))
        result = self.decode(skill.prepare_image(self.source, (120, 120)))
        self.assertEqual(result.getpixel((60, 29)), (255,) * 3)
        self.assertEqual(result.getpixel((60, 30)), (0,) * 3)
        self.assertEqual(result.getpixel((60, 89)), (0,) * 3)
        self.assertEqual(result.getpixel((60, 90)), (255,) * 3)

    def test_stretch_fills_canvas_preserving_all_corners(self):
        image = Image.new("L", (60, 120), 128)
        draw = ImageDraw.Draw(image)
        for box, color in [((0, 0, 19, 19), 0), ((40, 0, 59, 19), 50),
                           ((0, 100, 19, 119), 180), ((40, 100, 59, 119), 220)]:
            draw.rectangle(box, fill=color)
        self.save(image)
        for profile, size in skill.PROFILES.items():
            with self.subTest(profile=profile):
                result = self.decode(skill.prepare_image(self.source, size, "stretch"))
                self.assertEqual(result.size, size)
                self.assertEqual(result.getpixel((0, 0)), (0,) * 3)
                self.assertEqual(result.getpixel((size[0] - 1, 0)), (50,) * 3)
                self.assertEqual(result.getpixel((0, size[1] - 1)), (180,) * 3)
                self.assertEqual(result.getpixel((size[0] - 1, size[1] - 1)), (220,) * 3)
                self.assertLess(result.convert("L").getextrema()[1], 255)

    def test_exif_orientation_and_metadata_removed(self):
        image = Image.new("RGB", (120, 60), "black")
        ImageDraw.Draw(image).rectangle((0, 0, 59, 59), fill="white")
        exif = Image.Exif()
        exif[274] = 6
        exif[315] = PRIVATE_SENTINEL
        self.source = self.root / "oriented.jpg"
        self.save(image, exif=exif, quality=100)
        result = self.decode(skill.prepare_image(self.source, (60, 120)))
        self.assertEqual(result.getpixel((30, 15)), (255,) * 3)
        self.assertEqual(result.getpixel((30, 105)), (0,) * 3)
        self.assertNotIn(PRIVATE_SENTINEL.encode(), skill.prepare_image(self.source, (60, 120)))
        self.assertFalse(result.getexif())

    def test_transparency_composited_onto_white(self):
        self.save(Image.new("RGBA", (10, 10), (0, 0, 0, 0)))
        result = self.decode(skill.prepare_image(self.source, (20, 20)))
        self.assertEqual(result.getextrema(), ((255, 255),) * 3)

    def test_invalid_missing_animated_and_oversized_inputs(self):
        with self.assertRaisesRegex(skill.SkillError, "Cannot decode"):
            skill.prepare_image(self.source, (10, 10))
        self.source.write_bytes(b"not an image")
        with self.assertRaisesRegex(skill.SkillError, "Cannot decode"):
            skill.prepare_image(self.source, (10, 10))
        self.source = self.root / "animated.gif"
        self.save(Image.new("RGB", (10, 10), "black"), save_all=True,
                  append_images=[Image.new("RGB", (10, 10), "white")])
        with self.assertRaisesRegex(skill.SkillError, "single, static"):
            skill.prepare_image(self.source, (10, 10))
        self.source = self.root / "large.png"
        self.save(Image.new("RGB", (20, 20), "black"))
        with patch.object(Image, "MAX_IMAGE_PIXELS", 300):
            with self.assertRaisesRegex(skill.SkillError, "Cannot decode"):
                skill.prepare_image(self.source, (10, 10))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "synthetic.png"
        Image.new("RGB", (160, 300), (120, 60, 180)).save(self.source)
        self.device = FakeReader()
        self.output = self.root / "output.bmp"
        self.backups = self.root / "backups"

    def run_cli(self, host, *options):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = skill.main([
                str(self.source), "--host", host, "--output", str(self.output),
                "--backup-dir", str(self.backups), *options,
            ])
        self.assertNotIn(PRIVATE_SENTINEL, stdout.getvalue() + stderr.getvalue())
        return code, stdout.getvalue(), stderr.getvalue()

    def request_sequence(self):
        return [(method, path) for method, path, _, _ in self.device.requests]

    def assert_no_writes(self):
        self.assertFalse([request for request in self.device.requests if request[0] == "POST"])

    def assert_no_settings_write(self):
        self.assertNotIn(("POST", "/api/settings"), self.request_sequence())

    def test_offline_profile_and_size_never_connect(self):
        for options in [("--profile", "X3"), ("--profile", "X4"), ("--size", "400x600")]:
            with self.subTest(options=options), patch.object(skill.DeviceClient, "request") as request:
                code, stdout, stderr = self.run_cli("unreachable.invalid", *options)
                self.assertEqual(code, 0, stderr)
                request.assert_not_called()
                self.assertIn("Preparation only", stdout)
                self.output.unlink()

    def test_default_is_read_only_discovery(self):
        with fake_server(self.device) as host:
            code, stdout, stderr = self.run_cli(host)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.request_sequence(), [("GET", "/api/status")])
        self.assertIn("528x792", stdout)
        self.assertFalse(self.backups.exists())

    def test_success_sequence_gzip_dynamic_custom_and_private_recovery(self):
        self.device.gzip = True
        self.device.settings[0]["options"] = ["Light", "Dark", "Cover", "Custom"]
        unrelated = copy.deepcopy(self.device.settings[1:])
        with fake_server(self.device) as host:
            code, stdout, stderr = self.run_cli(host, "--apply")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.request_sequence(), [
            ("GET", "/api/status"), ("GET", "/api/settings"), ("GET", "/api/files?path=%2F"),
            ("POST", "/upload?path=%2F"), ("GET", "/download?path=%2Fsleep.bmp"),
            ("POST", "/api/settings"), ("GET", "/api/settings"),
        ])
        self.assertEqual(self.device.image, self.output.read_bytes())
        self.assertEqual(self.device.settings[0]["value"], 3)
        self.assertEqual(self.device.settings[1:], unrelated)
        self.assertIn("physical display has not been verified", stdout)
        record_file = next(self.backups.glob("*/recovery.json"))
        record = json.loads(record_file.read_text())
        self.assertEqual(record["previous_sleepScreen"], 0)
        self.assertEqual(record["previous_mode"], "Light")
        self.assertIsNone(record["backup_file"])
        self.assertEqual(record["prepared_sha256"], hashlib.sha256(self.device.image).hexdigest())
        self.assertNotIn(PRIVATE_SENTINEL, record_file.read_text())
        self.assertEqual(record_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(record_file.parent.stat().st_mode & 0o777, 0o700)

    def test_real_subprocess_end_to_end_stretch_and_help(self):
        script = Path(skill.__file__).resolve()
        help_result = subprocess.run(
            [sys.executable, str(script), "--help"], capture_output=True, text=True, timeout=15
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--apply", help_result.stdout)
        self.device.status["device"] = "X4"
        with fake_server(self.device) as host:
            result = subprocess.run(
                [sys.executable, str(script), str(self.source), "--apply", "--fit", "stretch",
                 "--host", host, "--output", str(self.output), "--backup-dir", str(self.backups)],
                capture_output=True, text=True, timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("proportions may be distorted", result.stdout)
        self.assertNotIn(PRIVATE_SENTINEL, result.stdout + result.stderr)
        with Image.open(self.output) as image:
            self.assertEqual(image.size, (480, 800))
            self.assertLess(image.convert("L").getextrema()[1], 255)
        self.assertEqual(self.device.image, self.output.read_bytes())
        self.assertEqual(self.device.settings[0]["value"], 2)

    def test_unknown_device_requires_choice_and_known_conflicts_rejected(self):
        with self.assertRaisesRegex(skill.SkillError, "Unknown device"):
            skill.screen_size({"device": "Future"})
        self.assertEqual(skill.screen_size({"device": "Future"}, profile="X3"), (528, 792))
        self.assertEqual(skill.screen_size({"device": "Future"}, size=(400, 600)), (400, 600))
        with self.assertRaisesRegex(skill.SkillError, "conflicts"):
            skill.screen_size({"device": "X3"}, profile="X4")
        for status in [None, [], {}, {"device": 4}, {"error": "unavailable"}]:
            with self.subTest(status=status), self.assertRaises(skill.SkillError):
                skill.screen_size(status, profile="X3")
        self.device.status["device"] = "Future"
        with fake_server(self.device) as host:
            code, _, stderr = self.run_cli(host, "--apply")
        self.assertEqual(code, 1)
        self.assertIn("Unknown device", stderr)
        self.assert_no_writes()
        self.assertFalse(self.output.exists())
        with fake_server(self.device) as host:
            code, _, stderr = self.run_cli(host, "--apply", "--size", "400x600")
        self.assertEqual(code, 0, stderr)

    def test_existing_image_requires_approval_then_byte_exact_backup(self):
        self.device.image = b"old image bytes, preserved even if not valid BMP"
        old = self.device.image
        self.device.files.append({"name": "SLEEP.BMP", "isDirectory": False, "size": len(old)})
        with fake_server(self.device) as host:
            code, _, stderr = self.run_cli(host, "--apply")
        self.assertEqual(code, 1)
        self.assertIn("--overwrite", stderr)
        self.assert_no_writes()
        self.assertFalse(self.backups.exists())
        self.output.unlink()
        self.device.requests.clear()
        with fake_server(self.device) as host:
            code, _, stderr = self.run_cli(host, "--apply", "--overwrite")
        self.assertEqual(code, 0, stderr)
        backup = next(self.backups.glob("*/sleep.bmp"))
        self.assertEqual(backup.read_bytes(), old)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        record = json.loads(backup.with_name("recovery.json").read_text())
        self.assertEqual(record["backup_sha256"], hashlib.sha256(old).hexdigest())
        sequence = self.request_sequence()
        self.assertLess(sequence.index(("GET", "/download?path=%2FSLEEP.BMP")),
                        sequence.index(("POST", "/upload?path=%2F")))

    def test_failed_backup_download_size_or_local_save_prevents_writes(self):
        for failure in ("http", "size", "local"):
            with self.subTest(failure=failure):
                self.device = FakeReader()
                self.device.image = b"old"
                self.device.files.append({"name": "sleep.bmp", "isDirectory": False,
                                          "size": 4 if failure == "size" else 3})
                if failure == "http":
                    self.device.failures[("GET", "/download?path=%2Fsleep.bmp")] = (500, b"error", {})
                if failure == "local":
                    self.backups.write_bytes(b"not a directory")
                with fake_server(self.device) as host:
                    code, _, _ = self.run_cli(host, "--apply", "--overwrite")
                self.assertEqual(code, 1)
                self.assert_no_writes()
                self.output.unlink()

    def prepare_collision(self):
        self.device.image = b"original image"
        self.device.files.append({"name": "sleep.bmp", "isDirectory": False,
                                  "size": len(self.device.image)})
        self.device.reject_existing_uploads = True

    def test_collision_firmware_preserves_and_verifies_device_backup(self):
        self.prepare_collision()
        self.device.gzip = True
        old = self.device.image
        unrelated = copy.deepcopy(self.device.settings[1:])
        with fake_server(self.device) as host:
            code, stdout, stderr = self.run_cli(host, "--apply", "--overwrite")
        self.assertEqual(code, 0, stderr)
        self.assertIn("HTTP 400: File already exists", stdout)
        self.assertIn("Device backup byte-verified", stdout)
        self.assertEqual(list(self.device.device_backups.values()), [old])
        backup_name = next(iter(self.device.device_backups))
        record_file = next(self.backups.glob("*/recovery.json"))
        self.assertEqual(json.loads(record_file.read_text())["device_backup_candidate"], backup_name)
        self.assertEqual(record_file.with_name("sleep.bmp").read_bytes(), old)
        self.assertEqual(self.device.image, self.output.read_bytes())
        self.assertEqual(self.device.settings[1:], unrelated)
        sequence = self.request_sequence()
        self.assertEqual(sequence.count(("POST", "/upload?path=%2F")), 2)
        rename = sequence.index(("POST", "/rename"))
        self.assertEqual(sequence[rename - 1], ("GET", "/download?path=%2Fsleep.bmp"))
        self.assertEqual(sequence[rename + 1], ("GET", "/download?path=%2F" + backup_name))
        self.assertEqual(sequence[rename + 2], ("POST", "/upload?path=%2F"))
        self.assertFalse(any("delete" in path for _, path in sequence))

    def test_collision_fallback_failures_preserve_backups_and_do_not_activate(self):
        for failure in ("changed", "rename", "rename_ack", "backup", "upload", "readback", "name_collision"):
            with self.subTest(failure=failure):
                self.device = FakeReader()
                self.prepare_collision()
                old = self.device.image
                if failure == "changed":
                    self.device.change_after_collision = True
                elif failure == "rename":
                    self.device.failures[("POST", "/rename")] = (500, b"failed", {})
                elif failure == "rename_ack":
                    self.device.rename_ack = b"unexpected acknowledgement"
                elif failure == "backup":
                    self.device.corrupt_device_backup = True
                elif failure == "upload":
                    self.device.fail_replacement = True
                elif failure == "readback":
                    self.device.corrupt_readback = True
                else:
                    self.device.device_backups["sleep-backup-" + uuid.UUID(int=1).hex + ".bmp"] = b"unrelated"
                fixed_name = (patch.object(skill.uuid, "uuid4", return_value=uuid.UUID(int=1))
                              if failure == "name_collision" else nullcontext())
                with fixed_name:
                    with fake_server(self.device) as host:
                        code, _, stderr = self.run_cli(host, "--apply", "--overwrite")
                self.assertEqual(code, 1)
                self.assertIn("Partial state", stderr)
                self.assertIn("Restore the local backup", stderr)
                self.assert_no_settings_write()
                self.assertFalse(any("delete" in path for _, path in self.request_sequence()))
                self.assertTrue(any(file.read_bytes() == old for file in self.backups.glob("*/sleep.bmp")))
                if failure == "changed":
                    self.assertNotIn(("POST", "/rename"), self.request_sequence())
                elif failure == "name_collision":
                    self.assertEqual(list(self.device.device_backups.values()), [b"unrelated"])
                    self.assertEqual(self.device.image, old)
                elif failure == "rename":
                    self.assertEqual(self.device.image, old)
                else:
                    self.assertEqual(list(self.device.device_backups.values()), [old])
                self.output.unlink()

    def test_collision_without_backed_up_original_never_renames(self):
        self.device.image = b"image not present in preflight listing"
        self.device.reject_existing_uploads = True
        with fake_server(self.device) as host:
            code, _, stderr = self.run_cli(host, "--apply", "--overwrite")
        self.assertEqual(code, 1)
        self.assertIn("appeared after preflight", stderr)
        self.assertNotIn(("POST", "/rename"), self.request_sequence())
        self.assert_no_settings_write()

    def test_failed_upload_ack_status_or_readback_never_activates(self):
        for failure in ("http", "ack", "readback", "download"):
            with self.subTest(failure=failure):
                self.device = FakeReader()
                self.device.settings[0]["value"] = 2
                if failure == "http":
                    self.device.failures[("POST", "/upload?path=%2F")] = (500, PRIVATE_SENTINEL.encode(), {})
                elif failure == "ack":
                    self.device.upload_ack = b'{"error":"disk full"}'
                elif failure == "readback":
                    self.device.corrupt_readback = True
                else:
                    self.device.failures[("GET", "/download?path=%2Fsleep.bmp")] = (404, b"missing", {})
                with fake_server(self.device) as host:
                    code, _, stderr = self.run_cli(host, "--apply")
                self.assertEqual(code, 1)
                self.assertIn("Partial state", stderr)
                self.assertIn("no settings update was sent", stderr)
                self.assertIn("already-Custom", stderr)
                self.assertIn("Recovery:", stderr)
                self.assert_no_settings_write()
                self.output.unlink()

    def test_settings_failure_or_unconfirmed_mode_is_not_success(self):
        for failure in ("http", "ack", "unchanged", "schema"):
            with self.subTest(failure=failure):
                self.device = FakeReader()
                if failure == "http":
                    self.device.failures[("POST", "/api/settings")] = (500, b"error", {})
                elif failure == "ack":
                    self.device.settings_ack = b"Applied 0 setting(s)"
                elif failure == "unchanged":
                    self.device.ignore_settings = True
                else:
                    self.device.after_settings = {"error": PRIVATE_SENTINEL}
                with fake_server(self.device) as host:
                    code, stdout, stderr = self.run_cli(host, "--apply")
                self.assertEqual(code, 1)
                self.assertIn("active mode is uncertain", stderr)
                self.assertNotIn("Custom mode confirmed", stdout)
                self.assertEqual(self.device.image, self.output.read_bytes())
                self.output.unlink()

    def test_bad_settings_schemas_prevent_upload(self):
        base = self.device.settings[0]
        cases = [
            {}, [], [base, base], [{"key": "other"}], [None],
            [{**base, "type": "string"}], [{**base, "value": True}],
            [{**base, "value": -1}], [{**base, "value": 20}],
            [{**base, "options": {"Custom": 2}}], [{**base, "options": ["Dark"]}],
            [{**base, "options": ["Custom", "custom"]}], [{**base, "options": [None]}],
        ]
        for settings in cases:
            with self.subTest(settings=settings):
                self.device = FakeReader()
                self.device.settings = settings
                with fake_server(self.device) as host:
                    code, _, _ = self.run_cli(host, "--apply")
                self.assertEqual(code, 1)
                self.assert_no_writes()
                self.output.unlink()

    def test_bad_file_schemas_or_directory_collision_prevent_upload(self):
        cases = [
            {}, [None], [{"name": "sleep.bmp"}],
            [{"name": "sleep.bmp", "isDirectory": True, "size": 0}],
            [{"name": "sleep.bmp", "isDirectory": False, "size": -1}],
            [{"name": "sleep.bmp", "isDirectory": 0, "size": 0}],
            [{"name": "sleep.bmp", "isDirectory": False, "size": 0}] * 2,
        ]
        for files in cases:
            with self.subTest(files=files):
                self.device = FakeReader()
                self.device.files = files
                with fake_server(self.device) as host:
                    code, _, _ = self.run_cli(host, "--apply", "--overwrite")
                self.assertEqual(code, 1)
                self.assert_no_writes()
                self.output.unlink()

    def test_local_input_and_output_safety(self):
        self.output.write_bytes(b"keep")
        with patch.object(skill.DeviceClient, "request") as request:
            code, _, stderr = self.run_cli("unreachable.invalid", "--apply", "--overwrite")
        self.assertEqual(code, 1)
        self.assertIn("Output already exists", stderr)
        self.assertEqual(self.output.read_bytes(), b"keep")
        request.assert_not_called()
        self.output = self.source
        code, _, stderr = self.run_cli("unreachable.invalid", "--profile", "X3")
        self.assertEqual(code, 1)
        self.assertIn("input image", stderr)
        self.output = self.root / "unused.bmp"
        code, _, stderr = self.run_cli("unreachable.invalid", "--profile", "X3", "--overwrite")
        self.assertEqual(code, 1)
        self.assertIn("requires --apply", stderr)


class TransportTests(unittest.TestCase):
    def test_redirects_refused_for_reads_and_writes(self):
        for method in ("GET", "POST"):
            for status in (301, 302, 303, 307, 308):
                with self.subTest(method=method, status=status):
                    device = FakeReader()
                    with fake_server(device) as host:
                        device.failures[(method, "/redirect")] = (
                            status, b"", {"Location": host + "/unintended-service"}
                        )
                        client = skill.DeviceClient(host)
                        with self.assertRaisesRegex(skill.SkillError, f"HTTP {status}.*Redirect refused"):
                            client.request("/redirect", b"image" if method == "POST" else None)
                    self.assertEqual(len(device.requests), 1)

    def test_proxy_bypass(self):
        device = FakeReader()
        with fake_server(device) as host, patch(
            "urllib.request.getproxies", return_value={"http": "http://127.0.0.1:1"}
        ):
            client = skill.DeviceClient(host)
            self.assertEqual(client.get_json("/api/status")["device"], "X3")

    def test_error_body_allowlist_and_invalid_gzip_keep_http_status(self):
        cases = [
            (b"Failed to write to SD card - disk may be full", {}, "disk may be full"),
            (PRIVATE_SENTINEL.encode(), {}, "HTTP 400"),
            (b"bad gzip", {"Content-Encoding": "gzip"}, "HTTP 400; error response unreadable"),
        ]
        for body, headers, message in cases:
            with self.subTest(message=message):
                device = FakeReader()
                device.failures[("POST", "/upload?path=%2F")] = (400, body, headers)
                with fake_server(device) as host:
                    with self.assertRaisesRegex(skill.SkillError, message) as caught:
                        skill.DeviceClient(host).upload(b"image")
                self.assertNotIn(PRIVATE_SENTINEL, str(caught.exception))

    def test_http_error_status_invalid_json_encoding_and_truncation(self):
        cases = [
            (403, b"private error: " + PRIVATE_SENTINEL.encode(), {}, "HTTP 403"),
            (500, b"failure", {}, "HTTP 500"),
            (204, b"", {}, "HTTP 204"),
            (200, b"<html>not JSON</html>", {}, "invalid JSON"),
            (200, b'{"device":"X3"}', {"Content-Encoding": "br"}, "unsupported Content-Encoding"),
            (200, b"not gzip", {"Content-Encoding": "gzip"}, "response failure"),
            (200, b'{"device":"X3"}', {"Content-Length": "200"}, "Content-Length"),
        ]
        for status, data, headers, message in cases:
            with self.subTest(message=message):
                device = FakeReader()
                device.failures[("GET", "/api/status")] = (status, data, headers)
                with fake_server(device) as host:
                    with self.assertRaisesRegex(skill.SkillError, message) as caught:
                        skill.DeviceClient(host).get_json("/api/status")
                self.assertNotIn(PRIVATE_SENTINEL, str(caught.exception))

    def test_response_limits_apply_before_and_after_gzip(self):
        for compressed in (False, True):
            with self.subTest(gzip=compressed):
                device = FakeReader()
                data = b"a" * 1000
                device.failures[("GET", "/large")] = (
                    200, gzip.compress(data) if compressed else data,
                    {"Content-Encoding": "gzip"} if compressed else {},
                )
                with fake_server(device) as host:
                    with self.assertRaisesRegex(skill.SkillError, "too large|safety limit"):
                        skill.DeviceClient(host).request("/large", limit=100)

    def test_finite_timeouts_and_connection_errors(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(skill.SkillError):
                skill.DeviceClient(timeout=timeout)
        client = skill.DeviceClient(timeout=2.5)
        for error in (socket.timeout(), OSError("synthetic connection failure")):
            with patch.object(client.opener, "open", side_effect=error) as opened:
                with self.assertRaisesRegex(skill.SkillError, "Wi-Fi File Transfer"):
                    client.request("/api/status")
                self.assertEqual(opened.call_args.kwargs["timeout"], 2.5)

    def test_host_validation_and_explicit_sizes(self):
        self.assertEqual(skill.normalize_host("reader.local:8080"), "http://reader.local:8080")
        self.assertEqual(skill.normalize_host("https://reader.local/"), "https://reader.local")
        for value in ("ftp://reader.local", "http://user:password@reader.local",
                      "http://reader.local/base", "http://reader.local/?token=test",
                      "http://reader.local/#fragment", "http://reader.local:0",
                      "http://reader.local:bad", "http://reader.local\n", "http://"):
            with self.subTest(host=value), self.assertRaises(skill.SkillError):
                skill.normalize_host(value)
        self.assertEqual(skill.parse_size("528x792"), (528, 792))
        for value in ("0x800", "800", "1x2x3", "9999x2", "4096x4096", "-1x10"):
            with self.subTest(size=value), self.assertRaises(argparse.ArgumentTypeError):
                skill.parse_size(value)


if __name__ == "__main__":
    unittest.main()
