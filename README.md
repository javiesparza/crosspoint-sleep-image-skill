# CrossPoint Sleep Image Skill

A reusable agent skill and small Python CLI for preparing, uploading, and
activating a custom CrossPoint e-reader sleep image. Image processing stays
local. **Device changes require `--apply`.** No cloud image services are used.

The default result preserves the full artwork, corrects EXIF orientation, and
centers it without cropping or stretching, adding white padding where needed.
Pixels are grayscale, stored as **RGB in an uncompressed 24-bit BMP**.

| Profile | Screen dimensions |
| --- | --- |
| X3 | 528x792 |
| X4 | 480x800 |

## Install as an agent skill

Requires Python 3.10+ with `venv`/`pip`, Git, and Pillow. Installation downloads
Pillow from the configured package index; image processing itself is local.

```sh
git clone https://github.com/javiesparza/crosspoint-sleep-image-skill.git \
  ~/.copilot/skills/crosspoint-sleep-image
cd ~/.copilot/skills/crosspoint-sleep-image
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

The root [SKILL.md](SKILL.md) supplies standard `name`/`description` YAML front
matter and procedural guidance. In a compatible agent supporting personal
skills at `~/.copilot/skills`, reload/start a session if necessary, then ask:

> Use crosspoint-sleep-image to prepare my local image for an X3 sleep screen.
> Keep the entire design and show me the result before uploading.

After reviewing it, explicitly authorize upload/activation and replacement of
any existing image. The agent should not treat installation or a preview request
as permission to modify a device. Other compatible agents can use their own
skill-discovery directory; clone the complete repo so bundled scripts stay
beside `SKILL.md`.

## Direct CLI use

Run these commands from the cloned repository with its isolated environment.
Relative outputs and backups go in the current working directory. Quote image
paths containing spaces. An input image is required; no personal artwork is
bundled.

```sh
.venv/bin/python scripts/crosspoint_sleep_image.py --help

# Entirely offline: choose the correct profile and inspect this preview locally.
.venv/bin/python scripts/crosspoint_sleep_image.py artwork.jpg \
  --profile X3 --output output/preview.bmp

# Read-only device discovery, then local preparation. No uploads/settings writes.
.venv/bin/python scripts/crosspoint_sleep_image.py artwork.jpg \
  --output output/detected.bmp

# Explicitly upload and activate; profile is detected from /api/status.
.venv/bin/python scripts/crosspoint_sleep_image.py artwork.jpg \
  --apply --output output/applied.bmp

# After approving replacement, back up and overwrite an existing /sleep.bmp.
.venv/bin/python scripts/crosspoint_sleep_image.py artwork.jpg \
  --apply --overwrite --output output/replacement.bmp
```

Keep the reader awake in **Wi-Fi File Transfer** on the same network for
connected commands. `--host http://crosspoint.local` is the default.
`--host reader-hostname.local` or a user-supplied device address/HTTP(S) URL
overrides it; an optional port is supported. Use only a trusted reader address,
not a third-party service. Credentials, base paths, query strings, and fragments
are rejected. TLS certificates are verified when HTTPS is selected.

Without `--apply`, `--profile X3`, `--profile X4`, or `--size WIDTHxHEIGHT` makes
the command completely offline. With no explicit dimensions, it makes just a
read-only `/api/status` request. Applying always reads status even with an
explicit profile. Unknown devices require an explicit profile/dimension choice;
a known device conflicting with that choice is rejected. There is no X4 fallback.
Custom dimensions must be 1..4096 per axis and at most 4,000,000 pixels.

The default output is `output/sleep.bmp`. **Local output files are never
overwritten**, including when `--overwrite` is supplied (that flag concerns the
reader only). For each retry or preview-to-apply step, select a new `--output`
path. The CLI always prepares from the input rather than uploading an arbitrary,
unchecked BMP verbatim. `--backup-dir` selects the recovery root (default
`backups`), and `--timeout` sets a finite positive socket timeout (default 15
seconds). Requests are not automatically retried.

### Edge-to-edge artwork

`--fit contain` is the default: full design, original proportions, possible white
sidebars or top/bottom padding. For an explicit **no-crop, no-added-padding** fill:

```sh
.venv/bin/python scripts/crosspoint_sleep_image.py artwork.jpg \
  --profile X3 --fit stretch --output output/stretched-preview.bmp
```

`--fit stretch` scales the entire image to the exact screen dimensions. It keeps
the full design but **changes its aspect ratio** (people, circles, and text may
look wider or taller). Existing borders inside the source image remain. Ask the
user before distorting or cropping an image; cropping is deliberately not
implemented. After approval, use the same `--fit stretch` on the `--apply`
command with a new output path.

## What an authorized apply does

1. Read `/api/status` and determine screen dimensions.
2. Read `/api/settings`, validate the `sleepScreen` enum, and find its `Custom`
   option by name instead of assuming a numeric index.
3. Read `/api/files?path=%2F`. If root `sleep.bmp` exists, require `--overwrite`,
   download it, and verify its length against the listing before any write.
4. Create a unique local recovery directory with the original `sleep.bmp` (when
   present) and a small `recovery.json` containing only the previous sleep mode
   and image hashes. Flush the files to disk before uploading.
5. POST multipart field `file`, filename `sleep.bmp`, to `/upload?path=%2F`.
   No `Expect: 100-continue` is sent.
6. GET `/download?path=%2Fsleep.bmp` and require exact byte equality to the
   prepared file before touching settings.
7. POST **only** `{"sleepScreen": <discovered Custom index>}` to `/api/settings`
   and read the settings back to confirm Custom mode.

Only `/sleep.bmp` and the sleep-screen mode are changed. Root `/sleep.bmp` takes
priority over `.sleep`/`sleep` image folders; nothing in those folders is
modified or deleted. System/environment proxies are bypassed, redirects are
refused (including upload redirects), gzip responses are supported, and actual
HTTP status plus endpoint-specific acknowledgement bodies are checked. Raw
responses/status/settings/file listings are never printed.

**Success means byte-identical read-back and confirmed Custom mode, not an
observed physical display.** Exit File Transfer when appropriate and perform a
device sleep cycle yourself to see the result. The script does not restart,
sleep, or otherwise control the reader.

## Failure and recovery

Before upload, a backup/schema/download failure prevents all live changes.
After upload begins, `/sleep.bmp` may have changed even if an error is returned.
If read-back fails, the CLI does **not** send a settings update. An already-Custom
reader may still use the changed file. If activation or its read-back fails,
the image is verified but the active mode is uncertain. Errors exit nonzero and
include the recovery directory once live writes have begun.

No automatic rollback is attempted: a second write can also fail or overwrite a
concurrent change. Keep the recovery directory private. To recover, with the
owner's permission, open the reader's `/files` page and upload the original
backup file named `sleep.bmp` to the root. Use `/settings` to restore the previous
mode recorded in `recovery.json`; check the current option labels rather than
blindly reusing an index after a firmware update. Download the restored file to
compare it with the backup. If there was no old root image, the record says
`backup_file: null`; restoring the previous mode may suffice, while returning to
the previous file layout requires explicitly authorized removal of the new root
file through the UI. The CLI never deletes files.

## Troubleshooting and limitations

| Symptom | Action |
| --- | --- |
| DNS/connection timeout | Wake the reader, enable Wi-Fi File Transfer, join the same network, wait for the server, then retry. A failed first `.local` request does not prove the reader is absent. Use the address shown on the reader with `--host` if mDNS fails. |
| HTTP errors or invalid JSON/schema | Check `/`, `/files`, and `/settings` in a browser. Firmware APIs may differ; this tool refuses ambiguous responses rather than guessing. No need to inspect browser history. |
| Existing root image | Review replacement intent, then use `--overwrite`; backup remains mandatory. |
| Backup/read-back mismatch or truncated response | Stop and inspect connectivity/storage. Do not activate unverified artwork; retain the local recovery directory. |
| Output already exists | Choose another `--output` path. No local overwrite flag is provided. |
| Invalid input | Use a readable single-frame format supported by the installed Pillow build, such as JPEG or PNG. Multi-frame/animated files and oversized inputs triggering Pillow's safety checks are rejected. |
| Correct upload but unexpected screen | Confirm Custom mode and cycle sleep on the reader. Physical rendering, grayscale quantization, and firmware-specific behavior are outside the CLI's verification. |

This targets the HTTP API behavior observed on firmware 1.6.0 and the official
development documentation below; it is not an official CrossPoint project.
Future firmware may change schemas or acknowledgement text and require updates.
It handles one static image, not randomized folders, transparent overlays,
cover modes, or remote image URLs. Network responses have a 32 MiB safety limit
(2 MiB for JSON), including after gzip decompression. The timeout is per socket
operation, not a total workflow deadline. Device operations are not atomic;
avoid concurrent file/settings changes while applying. Plain HTTP is appropriate
only on a trusted local network; proxies are not supported.

`.gitignore` excludes common image formats, generated output, recovery
directories, environments, caches, and `.env` files. This is a convenience, not a
privacy guarantee: never force-add personal files, and review staged content
before publishing. User images, device identifiers, account data, settings
dumps, and secrets do not belong in this repository.

## Development

All fixtures are generated synthetic images and a loopback HTTP fake reader.
The tests never contact a real CrossPoint or use personal artwork.

```sh
.venv/bin/python -m unittest discover -s tests -v
```

## References

- [Official CrossPoint user guide: custom images](https://github.com/crosspoint-reader/crosspoint-reader/blob/develop/USER_GUIDE.md#custom-images)
- [Official webserver endpoints](https://github.com/crosspoint-reader/crosspoint-reader/blob/develop/docs/webserver-endpoints.md)

These are moving `develop` references, not a presumed firmware release tag.

## License

[MIT](LICENSE).
