# Bed check and auto-start (Queue)

OS_3DPMS can start the next queued job on a printer **by itself** once the camera shows that the
finished part has been taken off the bed. It is **off by default** for every printer. Without it,
the Queue works as before: a person presses **Bed is clear, start**.

No AI and no cloud service: the check compares the camera picture with a picture of the empty bed.

## How to turn it on

1. Make sure the printer's camera works in OS_3DPMS and shows the bed.
2. Empty the bed, park the toolhead where it stops after a print, and switch the light on as usual.
3. **Queue → Bed check and auto-start → Save empty-bed picture** for that printer.
4. Put a part (or any object) on the bed and press **Test now**: it must say *NOT clear*. Remove it
   and press **Test now** again: it must say *looks clear*. If not, adjust the settings below.
5. **Devices → Edit** the printer and tick **Start the next queued job by itself when the bed
   check passes**. Save.

Saving or deleting the empty-bed picture needs an account that may control printers (not a
viewer or read-only account). Changing the auto-start setting needs an account that may edit
printers (the PC itself, or *Allowed Tailscale accounts*).

## When does a job start by itself?

All of these must be true at the same moment. If any is false, nothing is started and the
manual button stays:

- **Auto-start** is on for that printer (`"auto_start": true`).
- The job is **ready** by the normal queue rules: it is first in line for that printer, its file
  is still in Files, the printer reports *standby*, *complete* or *cancelled*, and no job was
  started there in the last 30 seconds.
- An **empty-bed picture** is saved for the printer.
- The camera gives a **fresh** picture (at most 3 seconds old) that is **different** from the
  previous one (a frozen camera never counts).
- With **require removal** on (default and recommended), a part was seen on the bed since the
  last print (the check failed at least once) and then disappeared. This prevents a start when the
  check simply cannot see the part. When the bed was already empty (for example a job queued long
  after the last print), start the first job by hand.
- The last `bed_check_frames` pictures in a row (default 3, one about every 5 seconds) all match
  the empty bed.

Then the job is sent and started exactly like the manual button. It is written to `audit.log` as
`queue-auto-start` by user `auto-start` (with the score), recorded in the job history, and an
**Auto-start** notification is sent. If the printer refuses the start, the job stays queued, a
warning notification is sent, and that job waits for a person (no retry loop).

When a print ends and the part is still on the bed, one **Bed not clear** notification is sent per
wait ("remove the part and it starts by itself").

Any error (camera offline, unreadable picture, light off, camera resolution changed, decoder
problem) counts as **not clear**.

## How the comparison works

1. Both pictures are reduced to a small picture: one value per 8×8 pixels, for brightness and the
   two color channels. Without extra packages, OS_3DPMS reads the block averages directly from
   the JPEG (baseline JPEG, which webcams, crowsnest/mjpg-streamer and Bambu cameras send). If
   [Pillow](https://pypi.org/project/pillow/) is installed (`py -m pip install pillow`) it is used
   instead; it is faster and also reads progressive JPEGs. Pillow is optional.
2. The **bed area** (`bed_check_roi`) is cut out and averaged into at most 32 × 24 cells.
3. Overall brightness is matched (lights and camera exposure drift), and so is the overall color
   (white balance). A brightness change of more than ×2 is refused (lights switched on/off).
4. A cell has **changed** when its brightness or color differs by more than
   `bed_check_tolerance` gray levels. The **score** is the percentage of changed cells. The bed
   counts as clear when the score is at most `bed_check_threshold`.

The Queue page shows the score and the reason for every printer with auto-start on.

## Settings (per printer, Devices → Edit or config.json)

| Key | Default | Meaning |
|-----|---------|---------|
| `auto_start` | `false` | Start the next queued job by itself when the bed check passes |
| `bed_check_threshold` | `0.5` | Percent of changed cells that still counts as clear (0.1-50). Lower = stricter |
| `bed_check_tolerance` | `24` | Gray levels a cell may differ before it counts as changed (4-128); config.json only |
| `bed_check_frames` | `3` | Clear pictures in a row before a start (1-10) |
| `bed_check_roi` | `[0, 0, 1, 1]` | Bed area as left, top, right, bottom fractions of the camera picture |
| `bed_check_require_removal` | `true` | Only start after a part was seen and then removed |

`bed_check_roi` is in the camera's own orientation (before the dashboard's rotation and mirroring).
Setting it to just the bed makes the check more sensitive and ignores the toolhead and cables at
the edges. Example: the bed fills the lower two thirds of the picture: `[0.05, 0.33, 0.95, 1]`.

The empty-bed pictures are kept in `bed_reference/<printer>.jpg` next to `config.json`.

## Limits: read before turning it on

The check is a simple picture comparison. It **can miss** a part, and it **can refuse** a clear
bed. A missed part means the printer prints on top of it, which can damage the nozzle, the bed or
the printer. Use auto-start only where that risk is acceptable, and keep **require removal** on.

It can miss:

- **Small or flat parts**: with the whole picture as bed area a cell is 1/32 of the picture width; a part
  smaller than about two cells (for example a thin 10 mm washer on a 250 mm bed seen from far)
  may not change enough cells. Narrow the ROI to the bed and lower the threshold.
- **Transparent, translucent, or bed-colored parts** (clear PETG on glass, black PLA on a black
  PEI sheet, white on white): little brightness or color difference.
- **Parts hidden from the camera** (behind the toolhead, outside the ROI, at a steep camera angle).
- **Parts that moved but were not removed** in a way that still matches (rare).

It can refuse a clear bed (safe: nothing starts):

- Toolhead, cables or the bed itself in a different position than in the empty-bed picture
  (bed slingers park the bed differently; take the picture where the printer parks after a print).
- Strong light changes, sunlight patches, shadows of people, a door opening.
- Camera moved, refocused or changed resolution (save a new empty-bed picture).
- Night: the light off (too dark is always refused).

Other notes:

- Only the **next** job of a printer is considered, and only one start per check run. Each job is
  started at most once (the start re-checks readiness while holding the printer's upload lock).
- The decoder is limited to pictures up to about 2560 × 2048; bigger camera pictures are refused.
- This was tested with synthetic camera pictures and mock printers, **not yet with real printers
  and cameras**. Try it with **Test now** on your setup before relying on it.
