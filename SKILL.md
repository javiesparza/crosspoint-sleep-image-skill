---
name: crosspoint-sleep-image
description: Prepare, upload, and activate a custom CrossPoint e-reader sleep screen from a local image. Use when asked to set CrossPoint sleep artwork, convert a photo to sleep.bmp, fix X3/X4 sleep-image sizing, or upload a custom sleep image over Wi-Fi File Transfer. Supports offline preparation, no-crop grayscale BMPs, opt-in stretch, verified uploads, and recoverable replacement.
---

# CrossPoint sleep image

Use the bundled `scripts/crosspoint_sleep_image.py` and `requirements.txt`.
Resolve those paths relative to this skill's installation directory, not the
user's project. Run in a local, writable working directory for outputs/backups.
See [README.md](README.md) for installation, arguments, and recovery.

## Consent and privacy

- Use only the image the user provided or explicitly selected. Process it locally
  with Pillow; never send it to an image service.
- Default to **contain**: preserve the entire artwork and its proportions, with
  centered white padding. If asked for edge-to-edge artwork, explain that
  `--fit stretch` keeps everything but changes proportions. Obtain explicit
  permission before distorting or cropping; cropping is not implemented.
- Preparation must not change the reader. Only use `--apply` when the user has
  authorized live upload/activation. Replacing an existing root image also
  requires approval and `--overwrite`; the script always backs it up first.
- Do not publish user images, backups, recovery records, host addresses, status
  dumps, file listings, account data, or settings dumps. Settings can contain
  passwords. Report only the image dimensions, verification result, and relevant
  sleep mode. Never inspect browser history to find the reader.

## Procedure

1. Check prerequisites: local Python 3.10+, an isolated environment with the
   bundled Pillow requirement, and a readable single-frame input image. Install
   dependencies using the commands in the README if needed.
2. For connected preparation/application, have the user wake the reader, enable
   **Wi-Fi File Transfer**, and join the same network. Default host is
   `http://crosspoint.local`; accept the user's explicit `--host` override.
   An initial connection failure is not proof the device is absent: wait for File
   Transfer to start, then retry. Do not restart or put the reader to sleep.
3. Prepare a preview without `--apply`, using an unused output path:

   ```sh
   <skill-dir>/.venv/bin/python <skill-dir>/scripts/crosspoint_sleep_image.py \
     "<input-image>" --profile X3 --output output/preview.bmp
   ```

   `--profile X3` selects 528x792; `--profile X4` selects 480x800. Explicit
   `--profile` or `--size WIDTHxHEIGHT` makes preparation fully offline.
   Without either, the script reads `/api/status`; unknown devices require an
   explicit choice, never an assumed X4 profile. It honors EXIF orientation,
   composites transparency onto white, and produces grayscale stored as RGB in
   an uncompressed 24-bit BMP. Preview the file if local image viewing is available.
4. After live-change authorization, run with an unused output path and `--apply`:

   ```sh
   <skill-dir>/.venv/bin/python <skill-dir>/scripts/crosspoint_sleep_image.py \
     "<input-image>" --output output/applied.bmp --apply
   ```

   Carry over the chosen `--profile`/`--size`, `--host`, and `--fit` when needed.
   If an existing root `sleep.bmp` is reported, get replacement approval and
   rerun with `--overwrite` and another unused output path. Never delete the old
   image or bypass a failed backup.
5. Let the script complete its safety sequence: discover the device; discover
   the `Custom` enum option; inspect root files; back up any existing `sleep.bmp`
   and record only the previous sleep mode; upload `/sleep.bmp`; download and
   compare bytes; update **only** `sleepScreen`; read back and confirm `Custom`.
   Root `/sleep.bmp` takes priority over `.sleep`/`sleep` folders; leave them and
   all unrelated files/settings alone.
6. On failure, report the actual error and any partial state/recovery location.
   Never claim activation if upload verification or settings read-back failed.
   Do not automatically retry writes or roll back. Recovery actions require
   permission; use the saved original image and previous mode via `/files` and
   `/settings`, as described in the README.
7. On success, report byte-identical upload and confirmed Custom mode. A **device
   sleep cycle is required** to see it; the script cannot verify the physical
   e-ink display. Ask the user to inspect it rather than claiming it was observed.

## References

- [Official custom sleep-image guide](https://github.com/crosspoint-reader/crosspoint-reader/blob/develop/USER_GUIDE.md#custom-images)
- [Official webserver endpoints](https://github.com/crosspoint-reader/crosspoint-reader/blob/develop/docs/webserver-endpoints.md)
