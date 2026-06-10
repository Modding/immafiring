# ImmaFiring 🔥

An open-source laser-engraving tool for cheap Chinese lasers (GRBL_ESP32 /
ESP3D firmware) controlled over WiFi – a free replacement for the proprietary
vendor apps. Runs on **Windows, Linux and macOS**.

Load images and engrave them directly as raster/photo engravings or as
vector/outline cuts – ImmaFiring generates the G-code itself and sends it to the
laser over WiFi.

The WiFi protocol was reconstructed by analyzing a vendor app (decompilation
permitted for interoperability purposes, § 69e UrhG). ImmaFiring contains **no**
third-party code and no vendor assets – only a standalone, freshly written
implementation of the open HTTP protocol.

## Features

- **Image engraving:** load PNG/JPG/BMP → generate G-code
  - **Raster / photo engraving:** grayscale → variable laser power (with dithering)
  - **Vector / outline cutting:** trace/cut contours (marching squares)
  - **Live preview** of the engraving including engraving-area dimensions
  - **Frame trace:** drive the bounding box of the engraving area to check
    placement before starting (laser off, or low constant power for a visible dot)
- WiFi connection, live status and position display
- Jog control (X/Y/Z), homing, unlock, pause/resume, soft reset, laser off
- Upload and start G-code files
- Console for arbitrary GRBL/ESP3D commands
- CLI **and** graphical interface

## Contents

| File               | Purpose                                                     |
|--------------------|-------------------------------------------------------------|
| `immafiring.py`     | Protocol library + command-line tool (CLI)                  |
| `image_engrave.py`  | Image → G-code engine (raster + vector) + image CLI         |
| `immafiring_gui.py` | Graphical interface (Tkinter) with image tab + preview      |
| `LICENSE`          | GNU General Public License v3                               |

**Dependencies:**
- CLI machine control (`immafiring.py`): none (Python standard library only)
- Image engraving + GUI: [Pillow](https://pypi.org/project/Pillow/) (`pip install Pillow`)
  and Tkinter
  - Tkinter: usually bundled with the python.org installer on Windows/macOS;
    on Linux install via `sudo apt install python3-tk`

## Quick start

Connect your PC to the laser's WiFi (default AP-mode IP: `192.168.0.1`), then:

```bash
python3 immafiring.py --ip 192.168.0.1 test
python3 immafiring.py status
python3 immafiring_gui.py        # GUI with image tab
```

On Windows, use `python` instead of `python3`.

## Image engraving

**In the GUI:** "Engrave image" tab → *Load image* → choose method
(raster or vector), set size/power/feed → *Refresh preview* → *Send to laser*
(or *Save G-code* first).

**Via CLI** (`image_engrave.py`, generates only the `.gcode` file, no laser):

```bash
# Photo engraving, 80 mm wide, 10 lines/mm, max power 1000
python3 image_engrave.py photo.jpg --mode raster --width 80 --lpmm 10 --power 1000

# Cut outline, 2 passes
python3 image_engrave.py logo.png --mode vector --width 60 --passes 2 -o logo.gcode
```

Then send the generated file to the laser with `immafiring.py upload` + `run`.

## CLI examples

```bash
python3 immafiring.py status                 # state + position
python3 immafiring.py home                   # homing cycle ($H)
python3 immafiring.py jog --x 10 --feed 3000 # 10 mm in +X
python3 immafiring.py frame --width 80 --height 60   # trace bounding box (laser off)
python3 immafiring.py upload motiv.gcode     # upload file
python3 immafiring.py run motiv.gcode        # start uploaded file
python3 immafiring.py laseroff               # laser off immediately
```

`python3 immafiring.py --help` lists all commands.

## Protocol (summary)

Everything runs over HTTP to the laser:

| Action            | HTTP call                                               |
|-------------------|---------------------------------------------------------|
| Send command      | `GET /command?commandText=<cmd>&PAGEID=0`               |
| Status/position   | `commandText=?` → parse `<Idle\|MPos:x,y,z\|…>`          |
| Upload file       | `POST /upload?path=/&PAGEID=0` (multipart, field `file`) |
| Start file        | `commandText=[ESP220]/<filename>`                       |

GRBL commands used: `?` `$H` `$X` `$$` `$I` `$J=…` `M5 S0` and `0x18` (reset).

## ⚠️ Safety

Lasers can damage eyes and start fires. Wear safety goggles, ensure fume
extraction and fire protection, and **never** leave the machine running
unattended. Test new commands first without a workpiece / at minimal power.

## License

ImmaFiring is licensed under the **GNU General Public License v3.0 or later**
(GPLv3+). You may use, modify and redistribute it; derivative works must also be
licensed under the GPLv3 and disclose their source code. See the [`LICENSE`](LICENSE)
file for the full text.

```
ImmaFiring - Open-source client for cheap GRBL_ESP32/ESP3D lasers over WiFi.
Copyright (C) 2026  Valentin Seipt

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
```

## Disclaimer

Independent interoperability project, created by analyzing the public WiFi
protocol. Not affiliated with, endorsed by, or approved by any manufacturer.
All product and brand names belong to their respective owners and are used here
only to describe compatibility. Use at your own risk.
