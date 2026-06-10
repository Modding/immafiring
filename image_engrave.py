#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# ImmaFiring - Open-Source-Client für günstige GRBL_ESP32/ESP3D-Laser über WiFi.
# Copyright (C) 2026  Valentin Seipt
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
image_engrave.py  --  Bild → G-Code-Engine für ImmaFiring.

Wandelt ein Bild (PNG/JPG/BMP/…) in GRBL-G-Code für einen Laser-Engraver um.
Zwei Verfahren:

  * RASTER  (raster_to_gcode):  Foto-/Graustufengravur. Das Bild wird Zeile für
    Zeile abgetastet, die Helligkeit jedes Pixels steuert die Laserleistung
    (S-Wert). Geeignet für Fotos, Logos, Schattierungen auf Holz/Leder/Acryl.

  * VEKTOR  (vector_to_gcode):  Outline/Kontur. Das Bild wird an einer Schwelle
    binarisiert; die Konturlinien werden per Marching-Squares extrahiert und
    nachgefahren. Geeignet zum Schneiden oder Ritzen von Silhouetten.

Benötigt: Pillow  (pip install Pillow).

S-Wert / Leistung: GRBL nutzt S0..$30 (Standard 1000) für die Laserleistung.
M4 = dynamischer Lasermodus (Leistung skaliert mit der Geschwindigkeit, ideal
fürs Rastern), M3 = konstante Leistung (für Vektor/Schneiden).

ACHTUNG: Erst mit geringer Leistung / ohne Werkstück testen. Laser sind gefährlich.
"""

from dataclasses import dataclass, field

try:
    from PIL import Image, ImageOps, ImageEnhance
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Pillow wird für die Bildgravur benötigt. Installieren mit:  pip install Pillow"
    ) from exc

# Pillow >= 9.1 nutzt Image.Resampling; Fallback für ältere Versionen.
_LANCZOS = getattr(Image, "Resampling", Image).LANCZOS


# --------------------------------------------------------------------------- #
# Parameter
# --------------------------------------------------------------------------- #
@dataclass
class EngraveParams:
    """Alle Einstellungen für eine Gravur. Maße in mm, Leistung als S-Wert."""

    # Zielgröße auf dem Werkstück (mm). Höhe wird aus dem Seitenverhältnis
    # berechnet, wenn height_mm None ist.
    width_mm: float = 80.0
    height_mm: float | None = None

    # Auflösung
    lines_per_mm: float = 10.0      # Zeilen pro mm (Y) -> 10 = 0,1 mm Zeilenabstand
    px_per_mm: float | None = None  # Pixel pro mm entlang der Scanrichtung (X);
    #                                 None -> gleich lines_per_mm (quadratische Pixel)

    # Laser / Bewegung
    max_power: int = 1000           # S-Wert bei „ganz schwarz“ (siehe $30)
    min_power: int = 0              # S-Wert bei „gerade noch brennend“
    feed: float = 3000.0            # Gravur-Vorschub mm/min
    travel_feed: float = 6000.0     # Eil-Vorschub für Leerwege mm/min
    laser_mode: str = "M4"          # "M4" (dynamisch) oder "M3" (konstant)

    # Bildaufbereitung
    invert: bool = False            # Hell/Dunkel tauschen
    brightness: float = 1.0         # 1.0 = neutral
    contrast: float = 1.0           # 1.0 = neutral
    gamma: float = 1.0              # >1 hellt Mitteltöne auf
    dither: bool = True             # Floyd-Steinberg-Dithering (gut für Fotos)
    white_cutoff: int = 250         # Pixel heller als dies werden NICHT gebrannt

    # Raster-Verhalten
    bidirectional: bool = True      # Zickzack (schneller) statt immer links->rechts

    # Vektor-Verhalten
    threshold: int = 128            # Binarisierungsschwelle (0..255)
    vector_power: int = 1000        # S-Wert beim Konturfahren
    vector_feed: float = 1200.0     # Vorschub beim Konturfahren mm/min
    passes: int = 1                 # Anzahl Durchgänge (Schneiden)

    # Nullpunkt / Offset (Werkstück-Koordinaten)
    origin_x: float = 0.0
    origin_y: float = 0.0


# --------------------------------------------------------------------------- #
# Bildaufbereitung
# --------------------------------------------------------------------------- #
def process_image(path_or_image, params: EngraveParams):
    """
    Lädt/übernimmt ein Bild und bereitet es als 8-Bit-Graustufenbild in der
    Zielauflösung (Pixelmaße) auf. Gibt (PIL.Image 'L', width_mm, height_mm)
    zurück.
    """
    if isinstance(path_or_image, Image.Image):
        img = path_or_image
    else:
        img = Image.open(path_or_image)

    # Transparenz auf Weiß legen, dann Graustufen.
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA"))
        img = bg.convert("L")
    else:
        img = img.convert("L")

    # Zielmaße in mm bestimmen.
    src_w, src_h = img.size
    aspect = src_h / src_w if src_w else 1.0
    width_mm = float(params.width_mm)
    height_mm = float(params.height_mm) if params.height_mm else width_mm * aspect

    # Zielpixelmaße aus der Auflösung.
    px_per_mm_x = params.px_per_mm if params.px_per_mm else params.lines_per_mm
    px_w = max(1, round(width_mm * px_per_mm_x))
    px_h = max(1, round(height_mm * params.lines_per_mm))
    img = img.resize((px_w, px_h), _LANCZOS)

    # Helligkeit / Kontrast / Gamma / Invertierung.
    if params.brightness != 1.0:
        img = ImageEnhance.Brightness(img).enhance(params.brightness)
    if params.contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(params.contrast)
    if params.gamma and params.gamma != 1.0:
        inv = 1.0 / params.gamma
        lut = [min(255, max(0, round(255 * ((i / 255.0) ** inv)))) for i in range(256)]
        img = img.point(lut)
    if params.invert:
        img = ImageOps.invert(img)

    return img, width_mm, height_mm


def _prepare_levels(img, params: EngraveParams):
    """Wendet optional Dithering an und liefert Pixelzugriff + Maße zurück."""
    if params.dither:
        # 1-Bit-Dithering, dann zurück nach 'L' (0/255), gut für Fotos.
        work = img.convert("1")  # Floyd-Steinberg
        work = work.convert("L")
    else:
        work = img
    return work


# --------------------------------------------------------------------------- #
# Raster-Gravur
# --------------------------------------------------------------------------- #
def raster_to_gcode(img, params: EngraveParams, width_mm, height_mm, progress=None):
    """
    Erzeugt G-Code (Liste von Zeilen) für eine Raster-/Fotogravur.

    Das Bild wird zeilenweise abgetastet. Zusammenhängende Pixel gleicher
    Leistung werden zu einer einzigen G1-Bewegung zusammengefasst. Weiße
    Bereiche (heller als white_cutoff) werden übersprungen.
    """
    work = _prepare_levels(img, params)
    px_w, px_h = work.size
    px = work.load()

    mm_per_px_x = width_mm / px_w
    mm_per_px_y = height_mm / px_h

    span = params.max_power - params.min_power
    cutoff = params.white_cutoff

    def power_for(value):
        """Pixelhelligkeit (0=schwarz..255=weiß) -> S-Wert (0 = aus)."""
        if value >= cutoff:
            return 0
        norm = (255 - value) / 255.0          # 0..1, dunkel = groß
        return int(round(params.min_power + norm * span))

    out = []
    out.append("; ImmaFiring Raster-Gravur")
    out.append("; Bild %dx%d px  ->  %.1f x %.1f mm  @ %.1f L/mm"
               % (px_w, px_h, width_mm, height_mm, params.lines_per_mm))
    out.append("G21")          # mm
    out.append("G90")          # absolut
    out.append("M5 S0")        # Laser aus
    out.append("G92 X0 Y0")    # aktuelle Position = Nullpunkt (optional)

    ox, oy = params.origin_x, params.origin_y
    left_to_right = True

    for row in range(px_h):
        # Y so, dass die oberste Bildzeile bei der größten Y-Koordinate liegt.
        y = oy + (px_h - 1 - row) * mm_per_px_y

        # Leistungswerte der Zeile bestimmen.
        powers = [power_for(px[c, row]) for c in range(px_w)]

        # Erste/letzte zu brennende Spalte finden -> Leerränder überspringen.
        first = next((i for i, p in enumerate(powers) if p > 0), None)
        if first is None:
            if progress:
                progress(row + 1, px_h)
            continue
        last = max(i for i, p in enumerate(powers) if p > 0)

        cols = range(first, last + 1) if left_to_right else range(last, first - 1, -1)
        cols = list(cols)

        # An den Zeilenanfang fahren (Laser aus). Startkante richtungsabhängig:
        # links->rechts an der linken Kante des ersten Pixels, rechts->links an
        # der rechten Kante -- sonst ist jede Rückzeile um 1 Pixel versetzt.
        start_edge = cols[0] + (0 if left_to_right else 1)
        start_x = ox + start_edge * mm_per_px_x
        out.append("M5 S0")
        out.append("G0 X%.3f Y%.3f F%g" % (start_x, y, params.travel_feed))
        out.append("%s S0" % params.laser_mode)   # Lasermodus aktivieren, Leistung 0

        # Zeile in Läufen gleicher Leistung abfahren.
        i = 0
        n = len(cols)
        while i < n:
            p = powers[cols[i]]
            j = i + 1
            while j < n and powers[cols[j]] == p:
                j += 1
            # Endpunkt dieses Laufs = rechte Kante des letzten Pixels im Lauf.
            end_col = cols[j - 1]
            edge = end_col + (1 if left_to_right else 0)
            end_x = ox + edge * mm_per_px_x
            out.append("G1 X%.3f S%d F%g" % (end_x, p, params.feed))
            i = j

        out.append("M5 S0")
        if params.bidirectional:
            left_to_right = not left_to_right
        if progress:
            progress(row + 1, px_h)

    out.append("M5 S0")
    out.append("G0 X%.3f Y%.3f F%g" % (ox, oy, params.travel_feed))
    return out


# --------------------------------------------------------------------------- #
# Vektor-/Outline-Gravur (Marching Squares)
# --------------------------------------------------------------------------- #
def _marching_squares(mask, px_w, px_h):
    """
    Extrahiert Konturliniensegmente aus einer Binärmaske (1 = innen).
    Liefert eine Liste von Segmenten [(x0,y0,x1,y1), …] in Pixel-Koordinaten
    (Gitterpunkte zwischen den Pixeln). Klassisches Marching-Squares.
    """
    def inside(x, y):
        if x < 0 or y < 0 or x >= px_w or y >= px_h:
            return 0
        return 1 if mask[y * px_w + x] else 0

    segs = []
    # Über die Zellen zwischen den Pixelzentren laufen.
    for y in range(-1, px_h):
        for x in range(-1, px_w):
            tl = inside(x, y)
            tr = inside(x + 1, y)
            br = inside(x + 1, y + 1)
            bl = inside(x, y + 1)
            case = (tl << 3) | (tr << 2) | (br << 1) | bl
            if case == 0 or case == 15:
                continue
            # Kantenmittelpunkte der Zelle (Koordinaten in „Pixelmitten-Gitter“).
            top = (x + 0.5, y)
            right = (x + 1, y + 0.5)
            bottom = (x + 0.5, y + 1)
            left = (x, y + 0.5)
            # Welche Kanten verbinden? (Standard-Lookup, Sattelfälle einfach gelöst)
            edges = {
                1:  [(left, bottom)],
                2:  [(bottom, right)],
                3:  [(left, right)],
                4:  [(top, right)],
                5:  [(left, top), (bottom, right)],
                6:  [(top, bottom)],
                7:  [(left, top)],
                8:  [(left, top)],
                9:  [(top, bottom)],
                10: [(left, bottom), (top, right)],
                11: [(top, right)],
                12: [(left, right)],
                13: [(bottom, right)],
                14: [(left, bottom)],
            }.get(case, [])
            for (a, b) in edges:
                segs.append((a[0], a[1], b[0], b[1]))
    return segs


def _chain_segments(segs):
    """Fügt Liniensegmente zu möglichst langen Polylinien zusammen."""
    from collections import defaultdict

    def key(p):
        return (round(p[0], 4), round(p[1], 4))

    adj = defaultdict(list)
    for idx, (x0, y0, x1, y1) in enumerate(segs):
        adj[key((x0, y0))].append((idx, (x1, y1)))
        adj[key((x1, y1))].append((idx, (x0, y0)))

    used = [False] * len(segs)
    polylines = []
    for start_idx in range(len(segs)):
        if used[start_idx]:
            continue
        x0, y0, x1, y1 = segs[start_idx]
        used[start_idx] = True
        chain = [(x0, y0), (x1, y1)]

        # Nach vorne verlängern.
        changed = True
        while changed:
            changed = False
            tail = chain[-1]
            for idx, other in adj[key(tail)]:
                if not used[idx]:
                    used[idx] = True
                    chain.append(other)
                    changed = True
                    break
        # Nach hinten verlängern.
        changed = True
        while changed:
            changed = False
            head = chain[0]
            for idx, other in adj[key(head)]:
                if not used[idx]:
                    used[idx] = True
                    chain.insert(0, other)
                    changed = True
                    break
        polylines.append(chain)
    return polylines


def vector_to_gcode(img, params: EngraveParams, width_mm, height_mm, progress=None):
    """
    Erzeugt G-Code für eine Vektor-/Outline-Gravur (Konturen schneiden/ritzen).
    Das Bild wird an params.threshold binarisiert und die Kanten nachgefahren.
    """
    px_w, px_h = img.size
    px = img.load()
    thr = params.threshold
    # 1 = dunkler als Schwelle (= Material/Motiv).
    mask = [1 if px[c, r] < thr else 0 for r in range(px_h) for c in range(px_w)]

    segs = _marching_squares(mask, px_w, px_h)
    polylines = _chain_segments(segs)

    mm_per_px_x = width_mm / px_w
    mm_per_px_y = height_mm / px_h
    ox, oy = params.origin_x, params.origin_y

    def to_mm(p):
        gx, gy = p
        x = ox + gx * mm_per_px_x
        # Bild oben = große Y.
        y = oy + (px_h - gy) * mm_per_px_y
        return x, y

    out = []
    out.append("; ImmaFiring Vektor-/Outline-Gravur")
    out.append("; %d Konturzüge, Schwelle %d, %d Durchgang/Durchgänge"
               % (len(polylines), thr, params.passes))
    out.append("G21")
    out.append("G90")
    out.append("M5 S0")
    out.append("G92 X0 Y0")

    total = max(1, len(polylines) * params.passes)
    done = 0
    for _pass in range(params.passes):
        for chain in polylines:
            if len(chain) < 2:
                done += 1
                continue
            x, y = to_mm(chain[0])
            out.append("M5 S0")
            out.append("G0 X%.3f Y%.3f F%g" % (x, y, params.travel_feed))
            out.append("%s S%d" % (params.laser_mode, params.vector_power))
            for p in chain[1:]:
                x, y = to_mm(p)
                out.append("G1 X%.3f Y%.3f F%g" % (x, y, params.vector_feed))
            out.append("M5 S0")
            done += 1
            if progress:
                progress(done, total)

    out.append("M5 S0")
    out.append("G0 X%.3f Y%.3f F%g" % (ox, oy, params.travel_feed))
    return out


# --------------------------------------------------------------------------- #
# Vorschau
# --------------------------------------------------------------------------- #
def make_preview(img, params: EngraveParams, mode="raster", max_px=480):
    """
    Erzeugt ein PIL-Bild für die GUI-Vorschau (so, wie der Laser es „sehen“ wird).
    raster: gedithertes/aufbereitetes Bild. vector: Konturen auf weißem Grund.
    """
    if mode == "vector":
        px_w, px_h = img.size
        px = img.load()
        thr = params.threshold
        mask = [1 if px[c, r] < thr else 0 for r in range(px_h) for c in range(px_w)]
        segs = _marching_squares(mask, px_w, px_h)
        prev = Image.new("L", (px_w, px_h), 255)
        draw = prev.load()
        # Segmente grob einzeichnen (auf Pixelraster gerundet).
        for (x0, y0, x1, y1) in segs:
            for t in range(0, 11):
                xx = int(round(x0 + (x1 - x0) * t / 10.0))
                yy = int(round(y0 + (y1 - y0) * t / 10.0))
                if 0 <= xx < px_w and 0 <= yy < px_h:
                    draw[xx, yy] = 0
        base = prev
    else:
        base = _prepare_levels(img, params)

    # Auf Anzeigegröße skalieren (Seitenverhältnis erhalten).
    w, h = base.size
    scale = min(max_px / w, max_px / h, 1.0) if max(w, h) > max_px else 1.0
    if scale < 1.0:
        base = base.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.NEAREST)
    return base


# --------------------------------------------------------------------------- #
# CLI (Bild -> G-Code-Datei, ohne Laser)
# --------------------------------------------------------------------------- #
def _main(argv=None):
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Bild -> G-Code (ImmaFiring).")
    ap.add_argument("image", help="Eingabebild (PNG/JPG/BMP/…)")
    ap.add_argument("-o", "--out", help="Ausgabe-G-Code (Standard: <bild>.gcode)")
    ap.add_argument("--mode", choices=["raster", "vector"], default="raster")
    ap.add_argument("--width", type=float, default=80.0, help="Breite in mm")
    ap.add_argument("--height", type=float, default=None, help="Höhe in mm (sonst aus Seitenverhältnis)")
    ap.add_argument("--lpmm", type=float, default=10.0, help="Zeilen pro mm")
    ap.add_argument("--power", type=int, default=1000, help="max. S-Wert")
    ap.add_argument("--feed", type=float, default=3000.0)
    ap.add_argument("--no-dither", action="store_true")
    ap.add_argument("--invert", action="store_true")
    ap.add_argument("--threshold", type=int, default=128, help="Vektor-Schwelle")
    ap.add_argument("--passes", type=int, default=1, help="Vektor-Durchgänge")
    args = ap.parse_args(argv)

    p = EngraveParams(
        width_mm=args.width, height_mm=args.height, lines_per_mm=args.lpmm,
        max_power=args.power, vector_power=args.power, feed=args.feed,
        dither=not args.no_dither, invert=args.invert,
        threshold=args.threshold, passes=args.passes,
    )
    img, wmm, hmm = process_image(args.image, p)

    def prog(i, total):
        print("\r%d/%d Zeilen" % (i, total), end="", flush=True)

    if args.mode == "vector":
        lines = vector_to_gcode(img, p, wmm, hmm, progress=prog)
    else:
        lines = raster_to_gcode(img, p, wmm, hmm, progress=prog)
    print()

    out = args.out
    if not out:
        base = args.image.rsplit(".", 1)[0]
        out = base + ".gcode"
    with open(out, "w", encoding="ascii", errors="replace") as fh:
        fh.write("\n".join(lines) + "\n")
    print("Geschrieben: %s  (%d Zeilen, %.1f x %.1f mm)" % (out, len(lines), wmm, hmm))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
