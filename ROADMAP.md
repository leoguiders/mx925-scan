# Roadmap

## Multi-page flatbed scans

Today every press of the start button on the flatbed produces a separate file.
The Canon Windows driver keeps the scan job open and makes the operation panel
ask *"Scan next page / Finish"*. SANE's `pixma` backend does not implement that
part of the protocol: it ends the job after each page.

Options, from quickest to best:

1. **Time window.** Collect flatbed scans that arrive within N seconds (e.g.
   60 s) of each other into one file, and write it once the window expires.
   - Pro: simple, no change to the panel workflow.
   - Con: two different documents scanned in quick succession are merged.
     ADF scans would stay unaffected.
2. **Repurpose the start buttons (flatbed only).** Colour = add page,
   black = add last page and finish the document.
   - Pro: explicit, no guessing.
   - Con: the buttons can no longer select colour vs. grey on the flatbed.
3. **Implement the panel's "next page / finish" dialog.**
   - Capture the BJNP traffic between the Canon Windows driver and the
     printer (Wireshark) while scanning several pages from the flatbed.
   - Work out how the job is kept open and how the panel answer is reported
     back (probably the XML job commands plus an interrupt packet).
   - Patch the `pixma` backend (custom `sane-backends` build in the image), or
     drive the protocol directly from the daemon.
   - Pro: the native workflow. Con: reverse engineering, uncertain outcome.

## Still to verify on real hardware

- [ ] Duplex scans in landscape ADF orientation (`page_rotate` direction,
      combined with `duplex_rotate_back`)
- [ ] Image corrections against Canon's driver: unsharp mask, show-through
      reduction, moiré reduction, background whitening
- [ ] Auto scan size / auto-crop with real photos (threshold, margin)
- [ ] Long idle periods / printer power saving vs. `RECONNECT_INTERVAL`

## Ideas

- Split several photos on the flatbed into separate files (auto scan size)
- Deskew photos and documents
- Identify the remaining event packet bytes `0x0c` and `0x12`
- Map further panel scan sizes (if the MX925 offers more than A4, Letter,
  Legal, 10×15, 13×18 and auto)
