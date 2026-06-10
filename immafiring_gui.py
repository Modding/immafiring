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
immafiring_gui.py  --  Grafische Oberfläche für ImmaFiring (Windows/Linux/macOS).

Benötigt:  immafiring.py + image_engrave.py (im selben Ordner), Tkinter und Pillow.
  Tkinter:  Windows/macOS meist im Python-Installer enthalten;
            Linux:  sudo apt install python3-tk
  Pillow:   pip install Pillow

Funktionen:
  * Verbindung (IP eingeben, testen), Live-Status, Jog-Steuerung, Maschinen-Buttons
  * Bild laden -> Raster-/Foto-Gravur oder Vektor-/Outline-Schnitt mit Live-Vorschau
  * G-Code erzeugen, speichern, hochladen und starten
  * Konsole für beliebige Befehle

Die Netzwerk- und Rechen-Aufrufe laufen in Hintergrund-Threads, damit die
Oberfläche nicht einfriert. Ergebnisse kommen über eine Queue zurück.

SICHERHEIT: Laser sind gefährlich. Schutzbrille, Absaugung, Brandschutz; Gerät
während eines Jobs nie unbeaufsichtigt lassen. Erst mit geringer Leistung testen.
"""

import os
import queue
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from immafiring import LaserClient, LaserError, DEFAULT_IP

# Pillow / Engine sind optional: ohne sie läuft die Maschinensteuerung trotzdem.
try:
    from PIL import ImageTk
    import image_engrave as ie
    HAVE_IMAGE = True
except ImportError as _exc:
    HAVE_IMAGE = False
    _IMAGE_IMPORT_ERROR = str(_exc)


class LaserGUI:
    def __init__(self, root):
        self.root = root
        root.title("ImmaFiring – Laser-Engraver Steuerung")
        root.geometry("980x720")
        root.minsize(820, 640)

        self.laser = None
        self.poll_active = False
        self.msgq = queue.Queue()

        # Bild-/Gravur-Zustand
        self.src_image = None        # Original-PIL-Bild
        self.src_path = None
        self.preview_photo = None    # Referenz halten (sonst GC)
        self.gcode_lines = None      # zuletzt erzeugter G-Code
        self.gcode_path = None       # zuletzt gespeicherte Datei
        self.last_wmm = None         # Maße der letzten Vorschau (für Rahmen abfahren)
        self.last_hmm = None

        self._build_ui()
        self.root.after(100, self._drain_queue)

    # ------------------------------------------------------------------ #
    # Oberfläche aufbauen
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        pad = dict(padx=6, pady=4)

        # --- Verbindungsleiste (immer sichtbar) ---
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Engraver-IP:").pack(side="left")
        self.ip_var = tk.StringVar(value=DEFAULT_IP)
        ttk.Entry(top, textvariable=self.ip_var, width=16).pack(side="left", padx=4)
        self.connect_btn = ttk.Button(top, text="Verbinden", command=self.on_connect)
        self.connect_btn.pack(side="left", padx=4)
        self.conn_lbl = ttk.Label(top, text="● getrennt", foreground="#b00")
        self.conn_lbl.pack(side="left", padx=8)
        self.state_var = tk.StringVar(value="Zustand: —")
        self.pos_var = tk.StringVar(value="X: —   Y: —   Z: —")
        ttk.Label(top, textvariable=self.state_var,
                  font=("TkDefaultFont", 10, "bold")).pack(side="left", padx=12)
        ttk.Label(top, textvariable=self.pos_var,
                  font=("TkFixedFont", 10)).pack(side="left", padx=6)

        # --- Tabs ---
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, **pad)

        tab_ctrl = ttk.Frame(nb)
        tab_img = ttk.Frame(nb)
        nb.add(tab_ctrl, text="Steuerung")
        nb.add(tab_img, text="Bild gravieren")

        self._build_control_tab(tab_ctrl)
        self._build_image_tab(tab_img)

    # ---- Tab 1: Maschinensteuerung -------------------------------------- #
    def _build_control_tab(self, parent):
        pad = dict(padx=6, pady=4)
        mid = ttk.Frame(parent)
        mid.pack(fill="x", **pad)

        # --- Jog-Pad ---
        jog = ttk.LabelFrame(mid, text="Jog / Bewegung")
        jog.pack(side="left", fill="y", padx=4)
        grid = ttk.Frame(jog)
        grid.pack(padx=8, pady=8)
        ttk.Button(grid, text="Y+", width=5,
                   command=lambda: self.jog(y=+self.step())).grid(row=0, column=1)
        ttk.Button(grid, text="X-", width=5,
                   command=lambda: self.jog(x=-self.step())).grid(row=1, column=0)
        ttk.Button(grid, text="⌂", width=5,
                   command=self.act_home).grid(row=1, column=1)
        ttk.Button(grid, text="X+", width=5,
                   command=lambda: self.jog(x=+self.step())).grid(row=1, column=2)
        ttk.Button(grid, text="Y-", width=5,
                   command=lambda: self.jog(y=-self.step())).grid(row=2, column=1)
        ttk.Button(grid, text="Z+", width=5,
                   command=lambda: self.jog(z=+self.step())).grid(row=0, column=3, padx=(12, 0))
        ttk.Button(grid, text="Z-", width=5,
                   command=lambda: self.jog(z=-self.step())).grid(row=2, column=3, padx=(12, 0))

        opt = ttk.Frame(jog)
        opt.pack(padx=8, pady=4, fill="x")
        ttk.Label(opt, text="Schritt (mm):").grid(row=0, column=0, sticky="w")
        self.step_var = tk.StringVar(value="10")
        ttk.Combobox(opt, textvariable=self.step_var, width=7,
                     values=["0.1", "1", "5", "10", "50", "100"]).grid(row=0, column=1, padx=4)
        ttk.Label(opt, text="Vorschub:").grid(row=1, column=0, sticky="w")
        self.feed_var = tk.StringVar(value="3000")
        ttk.Entry(opt, textvariable=self.feed_var, width=9).grid(row=1, column=1, padx=4)

        # --- Maschinen-Buttons ---
        ctrl = ttk.LabelFrame(mid, text="Maschine")
        ctrl.pack(side="left", fill="both", expand=True, padx=4)
        rows = [
            ("Homing ($H)", self.act_home),
            ("Unlock ($X)", lambda: self.run_async(lambda: self.laser.unlock(), "unlock")),
            ("Pause (!)", lambda: self.run_async(lambda: self.laser.hold(), "hold")),
            ("Fortsetzen (~)", lambda: self.run_async(lambda: self.laser.resume(), "resume")),
            ("Zum Nullpunkt", lambda: self.run_async(lambda: self.laser.go_origin(), "origin")),
            ("Nullpunkt setzen", lambda: self.run_async(lambda: self.laser.set_origin(), "setorigin")),
        ]
        for i, (label, cmd) in enumerate(rows):
            ttk.Button(ctrl, text=label, command=cmd).grid(
                row=i // 2, column=i % 2, sticky="ew", padx=6, pady=4)
        ctrl.columnconfigure(0, weight=1)
        ctrl.columnconfigure(1, weight=1)

        emerg = ttk.Frame(ctrl)
        emerg.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 4), padx=6)
        self._danger_button(emerg, "LASER AUS", self.act_laser_off).pack(
            side="left", expand=True, fill="x", padx=2)
        self._danger_button(emerg, "STOP / RESET", self.act_reset).pack(
            side="left", expand=True, fill="x", padx=2)

        # --- Datei-Bereich (fertiger G-Code) ---
        filef = ttk.LabelFrame(parent, text="G-Code-Datei hochladen / starten")
        filef.pack(fill="x", **pad)
        self.file_var = tk.StringVar()
        ttk.Entry(filef, textvariable=self.file_var).pack(
            side="left", fill="x", expand=True, padx=6, pady=6)
        ttk.Button(filef, text="Durchsuchen…", command=self.on_browse).pack(side="left", padx=4)
        ttk.Button(filef, text="Hochladen", command=self.on_upload).pack(side="left", padx=4)
        ttk.Button(filef, text="Hochladen + Starten", command=self.on_upload_run).pack(
            side="left", padx=4)

        # --- Konsole ---
        consf = ttk.LabelFrame(parent, text="Konsole")
        consf.pack(fill="both", expand=True, **pad)
        self.console = tk.Text(consf, height=8, font=("TkFixedFont", 10), state="disabled",
                               background="#111", foreground="#0f0", insertbackground="#0f0")
        self.console.pack(fill="both", expand=True, padx=6, pady=(6, 2))
        cmdrow = ttk.Frame(consf)
        cmdrow.pack(fill="x", padx=6, pady=(0, 6))
        self.cmd_var = tk.StringVar()
        entry = ttk.Entry(cmdrow, textvariable=self.cmd_var)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda e: self.on_send_cmd())
        ttk.Button(cmdrow, text="Senden", command=self.on_send_cmd).pack(side="left", padx=4)

    # ---- Tab 2: Bild gravieren ------------------------------------------ #
    def _build_image_tab(self, parent):
        if not HAVE_IMAGE:
            msg = ("Bildgravur benötigt Pillow.\n\n"
                   "Installieren mit:   pip install Pillow\n\n"
                   "Detail: %s" % _IMAGE_IMPORT_ERROR)
            ttk.Label(parent, text=msg, foreground="#b00", justify="left").pack(
                padx=20, pady=20, anchor="w")
            return

        # Linke Spalte: Parameter. Rechte Spalte: Vorschau.
        left = ttk.Frame(parent)
        left.pack(side="left", fill="y", padx=8, pady=8)
        right = ttk.Frame(parent)
        right.pack(side="left", fill="both", expand=True, padx=8, pady=8)

        # Bild laden
        ttk.Button(left, text="Bild laden…", command=self.on_load_image).pack(
            fill="x", pady=(0, 6))
        self.img_info = ttk.Label(left, text="(kein Bild geladen)", foreground="#555")
        self.img_info.pack(anchor="w", pady=(0, 8))

        # Modus
        modef = ttk.LabelFrame(left, text="Verfahren")
        modef.pack(fill="x", pady=4)
        self.mode_var = tk.StringVar(value="raster")
        ttk.Radiobutton(modef, text="Raster / Foto-Gravur", value="raster",
                        variable=self.mode_var, command=self._refresh_mode).pack(anchor="w", padx=6)
        ttk.Radiobutton(modef, text="Vektor / Outline-Schnitt", value="vector",
                        variable=self.mode_var, command=self._refresh_mode).pack(anchor="w", padx=6)

        # Geometrie
        geof = ttk.LabelFrame(left, text="Größe & Auflösung")
        geof.pack(fill="x", pady=4)
        self._grid_field(geof, 0, "Breite (mm):", "width_var", "80")
        self._grid_field(geof, 1, "Höhe (mm, leer=auto):", "height_var", "")
        self._grid_field(geof, 2, "Zeilen pro mm:", "lpmm_var", "10")

        # Laser / Bewegung
        lasf = ttk.LabelFrame(left, text="Laser & Vorschub")
        lasf.pack(fill="x", pady=4)
        self._grid_field(lasf, 0, "Max. Leistung (S):", "power_var", "1000")
        self._grid_field(lasf, 1, "Gravur-Vorschub:", "engfeed_var", "3000")
        self._grid_field(lasf, 2, "Eil-Vorschub:", "travel_var", "6000")
        ttk.Label(lasf, text="Lasermodus:").grid(row=3, column=0, sticky="w", padx=6, pady=2)
        self.lasermode_var = tk.StringVar(value="M4")
        ttk.Combobox(lasf, textvariable=self.lasermode_var, width=6,
                     values=["M4", "M3"]).grid(row=3, column=1, sticky="w", padx=4)

        # Bildaufbereitung (Raster)
        self.rasterf = ttk.LabelFrame(left, text="Bildaufbereitung (Raster)")
        self.rasterf.pack(fill="x", pady=4)
        self.dither_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.rasterf, text="Dithering (für Fotos)",
                        variable=self.dither_var).grid(row=0, column=0, columnspan=2, sticky="w", padx=6)
        self.invert_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.rasterf, text="Invertieren",
                        variable=self.invert_var).grid(row=1, column=0, columnspan=2, sticky="w", padx=6)
        self._grid_field(self.rasterf, 2, "Kontrast (1=neutral):", "contrast_var", "1.0")
        self._grid_field(self.rasterf, 3, "Weiß-Grenze (0–255):", "cutoff_var", "250")

        # Vektor-Optionen
        self.vectorf = ttk.LabelFrame(left, text="Vektor / Outline")
        self.vectorf.pack(fill="x", pady=4)
        self._grid_field(self.vectorf, 0, "Schwelle (0–255):", "threshold_var", "128")
        self._grid_field(self.vectorf, 1, "Schnitt-Leistung (S):", "vpower_var", "1000")
        self._grid_field(self.vectorf, 2, "Schnitt-Vorschub:", "vfeed_var", "1200")
        self._grid_field(self.vectorf, 3, "Durchgänge:", "passes_var", "1")

        # Aktionen
        actf = ttk.Frame(left)
        actf.pack(fill="x", pady=(8, 0))
        ttk.Button(actf, text="Vorschau aktualisieren",
                   command=self.on_preview).pack(fill="x", pady=2)

        # Rahmen abfahren (Positionierhilfe): nutzt die Maße der letzten Vorschau.
        framef = ttk.Frame(actf)
        framef.pack(fill="x", pady=2)
        ttk.Button(framef, text="Rahmen abfahren",
                   command=self.on_frame).pack(side="left", fill="x", expand=True)
        ttk.Label(framef, text="S:").pack(side="left", padx=(6, 2))
        self.frame_power_var = tk.StringVar(value="0")
        ttk.Entry(framef, textvariable=self.frame_power_var, width=6).pack(side="left")

        ttk.Button(actf, text="G-Code erzeugen & speichern…",
                   command=self.on_generate_save).pack(fill="x", pady=2)
        self._danger_button(actf, "An Laser senden (Upload + Start)",
                            self.on_generate_send).pack(fill="x", pady=2)

        self.gen_status = ttk.Label(left, text="", foreground="#06c")
        self.gen_status.pack(anchor="w", pady=(6, 0))

        # Vorschau-Canvas
        prevf = ttk.LabelFrame(right, text="Vorschau")
        prevf.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(prevf, background="#fafafa", highlightthickness=1,
                                highlightbackground="#ccc")
        self.canvas.pack(fill="both", expand=True, padx=4, pady=4)
        self.canvas.create_text(240, 180, text="Bild laden und Vorschau aktualisieren",
                                fill="#999", tags="hint")

        self._refresh_mode()

    def _grid_field(self, parent, row, label, attr, default):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=2)
        var = tk.StringVar(value=default)
        setattr(self, attr, var)
        ttk.Entry(parent, textvariable=var, width=10).grid(row=row, column=1, sticky="w", padx=4, pady=2)
        return var

    def _refresh_mode(self):
        """Blendet je nach Verfahren die passenden Optionsfelder ein/aus."""
        mode = self.mode_var.get()
        if mode == "vector":
            self.rasterf.pack_forget()
            self.vectorf.pack(fill="x", pady=4)
        else:
            self.vectorf.pack_forget()
            self.rasterf.pack(fill="x", pady=4)

    def _danger_button(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, bg="#c0392b", fg="white",
                         activebackground="#e74c3c", font=("TkDefaultFont", 10, "bold"))

    # ------------------------------------------------------------------ #
    # Parameter aus den Eingabefeldern lesen
    # ------------------------------------------------------------------ #
    def _read_params(self):
        def f(var, default):
            try:
                return float(var.get())
            except (ValueError, AttributeError):
                return default

        def i(var, default):
            try:
                return int(float(var.get()))
            except (ValueError, AttributeError):
                return default

        height = None
        try:
            if self.height_var.get().strip():
                height = float(self.height_var.get())
        except ValueError:
            height = None

        return ie.EngraveParams(
            width_mm=f(self.width_var, 80.0),
            height_mm=height,
            lines_per_mm=f(self.lpmm_var, 10.0),
            max_power=i(self.power_var, 1000),
            feed=f(self.engfeed_var, 3000.0),
            travel_feed=f(self.travel_var, 6000.0),
            laser_mode=self.lasermode_var.get() or "M4",
            invert=self.invert_var.get(),
            contrast=f(self.contrast_var, 1.0),
            dither=self.dither_var.get(),
            white_cutoff=i(self.cutoff_var, 250),
            threshold=i(self.threshold_var, 128),
            vector_power=i(self.vpower_var, 1000),
            vector_feed=f(self.vfeed_var, 1200.0),
            passes=max(1, i(self.passes_var, 1)),
        )

    # ------------------------------------------------------------------ #
    # Bild-Aktionen
    # ------------------------------------------------------------------ #
    def on_load_image(self):
        path = filedialog.askopenfilename(
            title="Bild wählen",
            filetypes=[("Bilder", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"),
                       ("Alle Dateien", "*.*")])
        if not path:
            return
        try:
            from PIL import Image
            self.src_image = Image.open(path)
            self.src_image.load()
            self.src_path = path
            self.img_info.config(
                text="%s  (%dx%d px)" % (os.path.basename(path), *self.src_image.size))
            self.log("Bild geladen: %s" % path)
            self.on_preview()
        except Exception as exc:  # noqa
            messagebox.showerror("Bild laden", "Konnte Bild nicht laden:\n%s" % exc)

    def on_preview(self):
        if self.src_image is None:
            messagebox.showinfo("Kein Bild", "Bitte zuerst ein Bild laden.")
            return
        params = self._read_params()
        mode = self.mode_var.get()
        self.gen_status.config(text="Vorschau wird berechnet…")

        def worker():
            try:
                img, wmm, hmm = ie.process_image(self.src_image, params)
                prev = ie.make_preview(img, params, mode=mode, max_px=520)
                self.msgq.put(("preview", (prev, wmm, hmm)))
            except Exception as exc:  # noqa
                self.msgq.put(("log", "✗ Vorschau: %s" % exc))
                self.msgq.put(("genstatus", ""))

        threading.Thread(target=worker, daemon=True).start()

    def _show_preview(self, prev, wmm, hmm):
        self.last_wmm = wmm
        self.last_hmm = hmm
        self.preview_photo = ImageTk.PhotoImage(prev)
        self.canvas.delete("all")
        cw = self.canvas.winfo_width() or 520
        ch = self.canvas.winfo_height() or 400
        x = max(0, (cw - prev.width) // 2)
        y = max(0, (ch - prev.height) // 2)
        self.canvas.create_image(x, y, anchor="nw", image=self.preview_photo)
        self.canvas.create_text(
            cw // 2, ch - 12,
            text="Gravurfläche: %.1f × %.1f mm" % (wmm, hmm), fill="#333")
        self.gen_status.config(text="Vorschau aktualisiert.")

    def _generate_gcode_async(self, after):
        """Erzeugt G-Code im Hintergrund und ruft after(lines, wmm, hmm) per Queue."""
        if self.src_image is None:
            messagebox.showinfo("Kein Bild", "Bitte zuerst ein Bild laden.")
            return
        params = self._read_params()
        mode = self.mode_var.get()
        self.gen_status.config(text="G-Code wird erzeugt…")

        def worker():
            try:
                img, wmm, hmm = ie.process_image(self.src_image, params)

                def prog(i, total):
                    if i % 25 == 0 or i == total:
                        self.msgq.put(("genstatus", "G-Code… %d/%d" % (i, total)))

                if mode == "vector":
                    lines = ie.vector_to_gcode(img, params, wmm, hmm, progress=prog)
                else:
                    lines = ie.raster_to_gcode(img, params, wmm, hmm, progress=prog)
                self.msgq.put(("gcode_done", (lines, wmm, hmm, after)))
            except Exception as exc:  # noqa
                self.msgq.put(("log", "✗ G-Code: %s" % exc))
                self.msgq.put(("genstatus", ""))

        threading.Thread(target=worker, daemon=True).start()

    def on_generate_save(self):
        def after(lines, wmm, hmm):
            path = filedialog.asksaveasfilename(
                title="G-Code speichern", defaultextension=".gcode",
                initialfile=(os.path.splitext(os.path.basename(self.src_path or "motiv"))[0] + ".gcode"),
                filetypes=[("G-Code", "*.gcode *.nc *.gc"), ("Alle Dateien", "*.*")])
            if not path:
                self.gen_status.config(text="")
                return
            with open(path, "w", encoding="ascii", errors="replace") as fh:
                fh.write("\n".join(lines) + "\n")
            self.gcode_lines = lines
            self.gcode_path = path
            self.file_var.set(path)
            self.gen_status.config(text="Gespeichert: %s (%d Zeilen)" % (os.path.basename(path), len(lines)))
            self.log("G-Code gespeichert: %s (%d Zeilen, %.1f×%.1f mm)" % (path, len(lines), wmm, hmm))

        self._generate_gcode_async(after)

    def on_generate_send(self):
        if self.laser is None:
            messagebox.showwarning("Nicht verbunden", "Bitte zuerst verbinden.")
            return
        if not messagebox.askyesno(
                "Gravur starten",
                "G-Code erzeugen, hochladen und SOFORT starten?\n\n"
                "Stelle sicher: Fokus, Material, Absaugung, Schutzbrille.\n"
                "Der Laser bewegt sich und brennt!"):
            return

        def after(lines, wmm, hmm):
            name = os.path.splitext(os.path.basename(self.src_path or "motiv"))[0] + ".gcode"
            tmp = os.path.join(tempfile.gettempdir(), name)
            with open(tmp, "w", encoding="ascii", errors="replace") as fh:
                fh.write("\n".join(lines) + "\n")
            self.gcode_lines = lines
            self.gcode_path = tmp

            def upload_worker():
                try:
                    self.msgq.put(("log", "Lade '%s' hoch (%d Zeilen)…" % (name, len(lines))))
                    res = self.laser.upload(tmp, remote_name=name)
                    self.msgq.put(("log", "» upload: %s" % str(res).strip()))
                    res2 = self.laser.run_file(name)
                    self.msgq.put(("log", "» start [ESP220]/%s: %s" % (name, str(res2).strip())))
                    self.msgq.put(("genstatus", "Job gestartet: %s" % name))
                except Exception as exc:  # noqa
                    self.msgq.put(("log", "✗ senden: %s" % exc))

            threading.Thread(target=upload_worker, daemon=True).start()

        self._generate_gcode_async(after)

    def on_frame(self):
        """Fährt den Bounding-Box-Rahmen der zuletzt angezeigten Vorschau ab."""
        if self.laser is None:
            messagebox.showwarning("Nicht verbunden", "Bitte zuerst verbinden.")
            return
        if self.last_wmm is None or self.last_hmm is None:
            messagebox.showinfo("Keine Maße",
                                "Bitte zuerst die Vorschau aktualisieren.")
            return
        try:
            power = max(0, int(float(self.frame_power_var.get())))
        except (ValueError, AttributeError):
            power = 0
        try:
            feed = float(self.travel_var.get())
        except (ValueError, AttributeError):
            feed = 6000.0

        wmm, hmm = self.last_wmm, self.last_hmm
        warn = ("\n\nACHTUNG: Laser läuft bei S%d konstant mit – nur einen kleinen,\n"
                "sichtbaren (nicht brennenden) Wert verwenden!" % power) if power > 0 else \
               "\n\nLaser bleibt AUS (nur Bewegung)."
        if not messagebox.askyesno(
                "Rahmen abfahren",
                "Rechteck %.1f × %.1f mm ab aktueller Position abfahren?%s"
                % (wmm, hmm, warn)):
            return

        self.log("Rahmen abfahren: %.1f × %.1f mm, S%d, F%g" % (wmm, hmm, power, feed))
        self.run_async(
            lambda: self.laser.frame_bounds(wmm, hmm, feed=feed, power=power),
            "frame")

    # ------------------------------------------------------------------ #
    # Hilfsfunktionen
    # ------------------------------------------------------------------ #
    def step(self):
        try:
            return float(self.step_var.get())
        except ValueError:
            return 1.0

    def feed(self):
        try:
            return float(self.feed_var.get())
        except ValueError:
            return 3000.0

    def log(self, text):
        self.console.configure(state="normal")
        self.console.insert("end", text.rstrip() + "\n")
        self.console.see("end")
        self.console.configure(state="disabled")

    # ------------------------------------------------------------------ #
    # Asynchrone Ausführung (Netzwerk im Thread, UI über Queue)
    # ------------------------------------------------------------------ #
    def run_async(self, fn, label, *args, **kwargs):
        if self.laser is None:
            messagebox.showwarning("Nicht verbunden", "Bitte zuerst verbinden.")
            return

        def worker():
            try:
                result = fn(*args, **kwargs)
                self.msgq.put(("log", "» %s: %s" % (label, str(result).strip())))
            except Exception as exc:  # noqa
                self.msgq.put(("log", "✗ %s: %s" % (label, exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.msgq.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "status":
                    self._apply_status(payload)
                elif kind == "preview":
                    self._show_preview(*payload)
                elif kind == "genstatus":
                    self.gen_status.config(text=payload)
                elif kind == "gcode_done":
                    lines, wmm, hmm, after = payload
                    self.gen_status.config(text="G-Code fertig (%d Zeilen)." % len(lines))
                    after(lines, wmm, hmm)
                elif kind == "conn":
                    ok, ip = payload
                    if ok:
                        self.conn_lbl.config(text="● verbunden (%s)" % ip, foreground="#0a0")
                        self.log("Verbunden mit %s" % ip)
                    else:
                        self.conn_lbl.config(text="● keine Antwort", foreground="#b00")
                        self.log("Keine Antwort von %s" % ip)
        except queue.Empty:
            pass
        self.root.after(120, self._drain_queue)

    # ------------------------------------------------------------------ #
    # Verbindung / Status
    # ------------------------------------------------------------------ #
    def on_connect(self):
        ip = self.ip_var.get().strip()
        self.laser = LaserClient(ip)

        def worker():
            ok = self.laser.test_connection()
            self.msgq.put(("conn", (ok, ip)))
            if ok and not self.poll_active:
                self.poll_active = True
                self._poll_loop()

        threading.Thread(target=worker, daemon=True).start()

    def _poll_loop(self):
        def worker():
            import time
            while self.poll_active and self.laser is not None:
                try:
                    st = self.laser.status()
                    self.msgq.put(("status", st))
                except LaserError:
                    pass
                time.sleep(0.6)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_status(self, st):
        self.state_var.set("Zustand: %s" % (st["state"] or "?"))
        p = st["mpos"] or ("—", "—", "—")
        self.pos_var.set("X: %-8s Y: %-8s Z: %-8s" % (p[0], p[1], p[2]))

    # ------------------------------------------------------------------ #
    # Maschinen-Aktionen
    # ------------------------------------------------------------------ #
    def jog(self, x=None, y=None, z=None):
        self.run_async(lambda: self.laser.jog(x=x, y=y, z=z, feed=self.feed(), relative=True), "jog")

    def act_home(self):
        self.run_async(lambda: self.laser.home(), "home")

    def act_laser_off(self):
        self.run_async(lambda: self.laser.laser_off(), "laser_off")

    def act_reset(self):
        if messagebox.askyesno("Reset", "Soft-Reset senden? Aktueller Job wird abgebrochen."):
            self.run_async(lambda: self.laser.reset(), "reset")

    def on_browse(self):
        path = filedialog.askopenfilename(
            title="G-Code-Datei wählen",
            filetypes=[("G-Code", "*.gcode *.nc *.gc *.txt"), ("Alle Dateien", "*.*")])
        if path:
            self.file_var.set(path)

    def on_upload(self, then_run=False):
        path = self.file_var.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showwarning("Keine Datei", "Bitte eine gültige Datei wählen.")
            return
        if self.laser is None:
            messagebox.showwarning("Nicht verbunden", "Bitte zuerst verbinden.")
            return
        name = os.path.basename(path)

        def worker():
            try:
                self.msgq.put(("log", "Lade '%s' hoch…" % name))
                res = self.laser.upload(path)
                self.msgq.put(("log", "» upload: %s" % str(res).strip()))
                if then_run:
                    res2 = self.laser.run_file(name)
                    self.msgq.put(("log", "» start [ESP220]/%s: %s" % (name, str(res2).strip())))
            except Exception as exc:  # noqa
                self.msgq.put(("log", "✗ upload: %s" % exc))

        threading.Thread(target=worker, daemon=True).start()

    def on_upload_run(self):
        if messagebox.askyesno("Job starten",
                               "Datei hochladen und Gravur/Schnitt sofort starten?\n"
                               "Stelle sicher, dass alles bereit ist (Fokus, Material, Absaugung)."):
            self.on_upload(then_run=True)

    def on_send_cmd(self):
        text = self.cmd_var.get().strip()
        if not text:
            return
        self.cmd_var.set("")
        self.log("> %s" % text)
        self.run_async(lambda: self.laser.command(text), "cmd")


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    LaserGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
