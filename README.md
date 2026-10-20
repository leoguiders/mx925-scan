# mx925-scan

Push-button **scan-to-folder** for the **Canon PIXMA MX925** (MX920 series) over the network, packaged as a Docker container. Start a scan on the printer's operation panel, and the result lands in a folder, for example the [paperless-ngx](https://docs.paperless-ngx.com/) consume directory.

All scan settings come from the printer's panel: colour or black, resolution, flatbed or ADF, simplex or duplex, binding edge, ADF orientation, scan size, file format, and the image corrections (unsharp mask, show-through reduction, moiré reduction).

## How it works

The daemon talks to the printer via SANE's `pixma` backend over Canon's BJNP protocol (port 8612).

1. **Polling.** A SANE connection stays open, and the button state is polled about once per second, much like scanbd does but without dbus or saned.
2. **Event packet.** When *Scan → PC* is started on the panel, the printer sends an event packet. The `pixma` backend decodes only a few fields of it (button, resolution index). The daemon also captures the **raw packet**: it enables pixma's debug hex dump and parses it from a redirected stderr. That makes the rest of the panel settings available.
3. **Rules.** `config.yaml` maps sensor values and raw packet bytes to scan settings.
4. **Scan.** `scanimage` scans with those settings. The daemon then drops blank pages, rotates duplex back sides, applies image filters, writes PDF, TIFF, JPEG or PNG, and moves the result atomically into the output folder.

OCR is left to paperless.

## Quick start

```bash
git clone <this repo> mx925-scan && cd mx925-scan
# edit docker-compose.yml: SCANNER_IP, PUID/PGID, path of the consume directory
docker compose up -d --build
docker compose logs -f
```

Once the log shows `Ready - waiting for a scan to be started on the printer`, select *Scan → PC* on the printer and press the colour or black start button.

To check connectivity:

```bash
docker compose exec mx925-scan scanimage -L
```

## Configuration

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `SCANNER_IP` | – | IP address of the printer |
| `SCANNER_DEVICE` | auto | SANE device name, e.g. `pixma:MX920_192.168.1.50` |
| `BJNP_TIMEOUT` | `30000` | BJNP timeout in ms. The MX925 sometimes needs more than the SANE default of 10 s on scan start. |
| `OUTPUT_DIR` | `/output` | Output directory inside the container |
| `PUID` / `PGID` | – | Owner of the created files (e.g. the paperless user) |
| `LOG_LEVEL` | `INFO` | `DEBUG` shows sensor changes and scanimage output |
| `POLL_INTERVAL` | `1` | Seconds between button polls |
| `COOLDOWN` | `5` | Seconds after a scan before polling resumes |
| `SETTLE_DELAY` | `2` | Seconds between closing the polling connection and starting scanimage |
| `RETRIES` / `RETRY_DELAY` | `2` / `10` | Retries on BJNP connection errors |
| `RAW_EVENTS` | `1` | Capture the raw event packet (`0` = SANE values only) |

### config.yaml

On first start, `config.yaml` is copied to `./config/config.yaml`. That copy is **re-read on every scan**, so edits take effect without a restart.

- `defaults`: base settings
- `buttons`: overrides per start button (`button-1` = colour, `button-2` = black)
- `rules`: `when` → `set` mappings, applied in order, with later rules overriding earlier ones. Conditions can match SANE sensor values (e.g. `scan-resolution`) or raw packet bytes (`byte-XX`, where XX is the hex offset).

Main settings:

| Setting | Values |
|---|---|
| `mode` | `Color`, `Gray`, `Lineart` |
| `resolution` | dpi |
| `source` | `auto`, `flatbed`, `adf`, `duplex` |
| `duplex` | `true` / `false` (used with `source: adf` or `auto`) |
| `duplex_rotate_back` | rotation of back sides in degrees |
| `page_rotate` | rotation of all pages (counter-clockwise) |
| `page_width` / `page_height` | scan area in mm; `null` = full area |
| `auto_crop` | crop the flatbed scan to its content |
| `format` | `pdf`, `tiff` (multi-page), `jpeg`, `png` (one file per page) |
| `jpeg_quality` | JPEG quality for PDF and JPEG output |
| `skip_blank`, `blank_threshold` | drop blank pages |
| `unsharp_mask`, `unsharp_*` | sharpening |
| `reduce_bleed`, `bleed_threshold` | show-through reduction (light tones → white) |
| `descreen`, `descreen_radius` | moiré / halftone reduction |
| `filename` | strftime pattern for the output file name |
| `subdir` | subfolder of the output dir (with `PAPERLESS_CONSUMER_SUBDIRS_AS_TAGS=true` → tag) |
| `extra_args` | additional `scanimage` arguments |

## MX925 event packet

The bytes below were determined empirically on an MX925 (sending the packet from the panel with one setting changed at a time). All offsets are hex.

| Panel setting | Source | Values |
|---|---|---|
| Colour / black | `button-1` / `button-2` | 1 = pressed |
| Resolution | `scan-resolution` | 1 = 75, 2 = 150, 3 = 300, 4 = 600 dpi |
| Flatbed / ADF | `byte-08` | 01 = flatbed, 02 = ADF |
| Simplex / duplex | `byte-09` | 00 = flatbed, 01 = simplex, 02 = duplex |
| Scan size | `byte-0a` | 01 = A4, 02 = Letter, 03 = Legal, 08 = 10×15 cm, 09 = 13×18 cm, 0b = auto |
| File format | `byte-0b` | 01 = JPEG, 02 = TIFF, 03 = PDF, 04 = compact PDF |
| Reduce show-through | `byte-0d` | 01 = off, 02 = on |
| Reduce moiré | `byte-0e` | 01 = off, 02 = on |
| Unsharp mask | `byte-0f` | 01 = off, 02 = on |
| ADF orientation | `byte-10` | 00 = flatbed, 01 = portrait, 02 = landscape |
| Binding edge | `byte-11` | 00 = simplex, 01 = long edge, 02 = short edge |

Unknown so far: `byte-0c` (usually 02 or 01, 00 for compact PDF) and `byte-12` (02 for duplex).

Example packet (ADF, duplex, long edge, A4, PDF, unsharp mask on):

```
00 00 00 00 01 01 01 02 02 02 01 03 02 01 01 02 01 01 02 00 ...
```

On another PIXMA model, set `LOG_LEVEL: DEBUG`, change one panel setting at a time, compare the `Event packet:` log lines, and adjust the rules.

## Notes and limitations

- **Duplex back sides.** The MX925 always delivers back sides rotated by 180°. With *long edge* binding the daemon rotates them back. With *short edge* binding they are left as scanned, because on such documents the back is printed upside down.
- **Image corrections.** `scanimage` cannot apply them, so the daemon reproduces them with Pillow. Results will look similar to Canon's driver, not identical.
- **Multi-page flatbed scans.** Each press of the start button produces one file. The Windows driver keeps the job open and lets the panel ask "scan next page / finish". SANE does not implement that part of the protocol.
- **Exclusive access.** While the container is connected, other computers cannot scan via the network. Printing is not affected.
- **"PCScanning" / "Previous scan attempt was not completed".** The printer is stuck in an unfinished job. Press *Stop* or power-cycle the printer. Starting a scan on an empty ADF can cause this, which is why the source is taken from the panel instead of being probed.
- **Auto scan size.** The daemon scans the full flatbed area and crops to the content. Several photos at once end up in one image, and skewed photos are not straightened.
