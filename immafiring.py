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
immafiring.py  --  Linux-Client für GRBL_ESP32 (ESP3D) Laser-Engraver über WiFi.
Teil des Projekts ImmaFiring.

Das Kommunikationsprotokoll wurde aus einer proprietären Android-Hersteller-App rekonstruiert.
Der Engraver läuft mit ESP3D / GRBL_ESP32-Firmware und bietet ein HTTP-Interface:

    GET  http://<IP>/command?commandText=<GRBL-Befehl>&PAGEID=0   -> Befehl senden
    POST http://<IP>/upload?path=/&PAGEID=0                       -> Datei hochladen (multipart)
    [ESP220]<dateiname>                                           -> hochgeladene Datei starten

Diese Datei enthält:
  * die Klasse LaserClient (die Protokoll-Bibliothek)
  * ein Kommandozeilen-Werkzeug (siehe `python3 immafiring.py --help`)

Es werden NUR Module der Python-Standardbibliothek verwendet -> keine Installation nötig.

ACHTUNG / SICHERHEIT: Ein Laser ist gefährlich. Sorge immer für Sichtschutz,
Absaugung und Brandschutz, und lass das Gerät während eines Jobs nie unbeaufsichtigt.
Teste neue Befehle erst mit niedriger Leistung / ohne Werkstück.
"""

import argparse
import os
import re
import sys
import time
import uuid
import urllib.parse
import urllib.request
import urllib.error

DEFAULT_IP = "192.168.0.1"   # Standard-IP des Engravers im Access-Point-Modus
DEFAULT_PORT = 80
DEFAULT_TIMEOUT = 6.0

_UMLAUTS = {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"}


def safe_remote_name(name, max_len=28):
    """
    Macht einen Dateinamen ESP3D/SPIFFS-tauglich.

    SPIFFS auf dem ESP32 erlaubt nur ~31 Zeichen Pfadlänge; Umlaute, Leer-
    und Sonderzeichen können Upload oder [ESP220]-Start scheitern lassen.
    Transliteriert Umlaute, ersetzt alles außer A-Za-z0-9_- durch '_' und
    kürzt auf max_len (inkl. Endung).
    """
    base, ext = os.path.splitext(os.path.basename(name))
    for k, v in _UMLAUTS.items():
        base = base.replace(k, v)
    base = base.encode("ascii", "ignore").decode()
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", base).strip("_") or "job"
    ext = re.sub(r"[^A-Za-z0-9.]+", "", ext) or ".gcode"
    keep = max(1, max_len - len(ext))
    return base[:keep] + ext


class LaserError(Exception):
    """Allgemeiner Fehler bei der Kommunikation mit dem Engraver."""


class LaserClient:
    """
    Client für einen GRBL_ESP32-Laser-Engraver.

    Beispiel:
        laser = LaserClient("192.168.0.1")
        print(laser.test_connection())
        print(laser.status())
        laser.home()
        laser.jog(10, 0, feed=3000)        # 10 mm in X (relativ)
        laser.upload("motiv.gcode")
        laser.run_file("motiv.gcode")
    """

    # Regex für die GRBL-Statuszeile, z. B.:
    #   <Idle|MPos:0.000,0.000,0.000|FS:0,0|...>
    #   <Run,MPos:1.000,2.000,0.000,WPos:...>
    _STATE_RE = re.compile(r"<([A-Za-z]+)")
    _MPOS_RE = re.compile(r"MPos:(-?\d+\.?\d*),(-?\d+\.?\d*),(-?\d+\.?\d*)")
    _WPOS_RE = re.compile(r"WPos:(-?\d+\.?\d*),(-?\d+\.?\d*),(-?\d+\.?\d*)")

    def __init__(self, ip=DEFAULT_IP, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
        self.ip = ip
        self.port = port
        self.timeout = timeout

    # ------------------------------------------------------------------ #
    # Basis-Kommunikation
    # ------------------------------------------------------------------ #
    @property
    def base_url(self):
        if self.port == 80:
            return "http://%s" % self.ip
        return "http://%s:%d" % (self.ip, self.port)

    def command(self, text):
        """
        Sendet einen rohen Befehl an den Engraver und gibt die Textantwort zurück.
        Entspricht: GET /command?commandText=<text>&PAGEID=0
        """
        # commandText sicher URL-kodieren; Leerzeichen werden so zu %20.
        encoded = urllib.parse.quote(text, safe="")
        url = "%s/command?commandText=%s&PAGEID=0" % (self.base_url, encoded)
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            raise LaserError("Befehl '%s' fehlgeschlagen: %s" % (text, exc)) from exc
        except OSError as exc:
            raise LaserError("Verbindung zu %s fehlgeschlagen: %s" % (self.base_url, exc)) from exc

    def test_connection(self):
        """True, wenn der Engraver auf einen Status-Befehl antwortet."""
        try:
            self.command("?")
            return True
        except LaserError:
            return False

    # ------------------------------------------------------------------ #
    # Status / Position
    # ------------------------------------------------------------------ #
    def status(self):
        """
        Fragt den GRBL-Status ab ('?') und liefert ein dict:
            {'state': 'Idle', 'mpos': (x, y, z), 'wpos': (..)|None, 'raw': '<...>'}
        """
        raw = self.command("?")
        state = None
        m = self._STATE_RE.search(raw)
        if m:
            state = m.group(1)
        mpos = None
        m = self._MPOS_RE.search(raw)
        if m:
            mpos = tuple(float(v) for v in m.groups())
        wpos = None
        m = self._WPOS_RE.search(raw)
        if m:
            wpos = tuple(float(v) for v in m.groups())
        return {"state": state, "mpos": mpos, "wpos": wpos, "raw": raw.strip()}

    def monitor(self, interval=0.5, callback=None):
        """
        Pollt den Status fortlaufend (wie der HttpGetPosThread der App).
        callback(status_dict) wird bei jeder Abfrage aufgerufen. Endlos bis Ctrl-C.
        """
        try:
            while True:
                st = self.status()
                if callback:
                    callback(st)
                else:
                    pos = st["mpos"] or ("?", "?", "?")
                    print("\r[%-6s] X=%s Y=%s Z=%s        " %
                          (st["state"] or "?", pos[0], pos[1], pos[2]), end="", flush=True)
                time.sleep(interval)
        except KeyboardInterrupt:
            print()

    # ------------------------------------------------------------------ #
    # Maschinensteuerung (GRBL-Realtime- und Systembefehle)
    # ------------------------------------------------------------------ #
    def home(self):
        """Referenzfahrt ($H)."""
        return self.command("$H")

    def unlock(self):
        """Alarm zurücksetzen / Achsen freigeben ($X)."""
        return self.command("$X")

    def reset(self):
        """
        Soft-Reset (Ctrl-X / 0x18) -- bricht den aktuellen Job ab.

        Hinweis: Das Steuerzeichen geht URL-kodiert (%18) an ESP3D. Ob die
        Firmware es an GRBL durchreicht, am eigenen Gerät OHNE Werkstück
        verifizieren (gilt auch für '!' und '~').
        """
        return self.command("\x18")

    def hold(self):
        """Feed-Hold (!) -- pausiert die Bewegung."""
        return self.command("!")

    def resume(self):
        """Cycle-Start / Resume (~)."""
        return self.command("~")

    def settings(self):
        """GRBL-Einstellungen auslesen ($$)."""
        return self.command("$$")

    def build_info(self):
        """Firmware-/Build-Info ($I)."""
        return self.command("$I")

    def laser_off(self):
        """Laser sofort ausschalten (M5 S0)."""
        return self.command("M5S0")

    # ------------------------------------------------------------------ #
    # Bewegung
    # ------------------------------------------------------------------ #
    def jog(self, x=None, y=None, z=None, feed=3000, relative=True, unit_mm=True):
        """
        Jog-Bewegung. Standard: relativ (G91), in mm (G21).
        Beispiel: jog(x=10) bewegt 10 mm in +X.
        Entspricht dem App-Befehl: $J=G90 G21 F3000 X0Y0
        """
        parts = ["$J="]
        parts.append("G91" if relative else "G90")
        parts.append(" G21" if unit_mm else " G20")
        parts.append(" F%g" % feed)
        if x is not None:
            parts.append(" X%g" % x)
        if y is not None:
            parts.append(" Y%g" % y)
        if z is not None:
            parts.append(" Z%g" % z)
        return self.command("".join(parts))

    def move_to(self, x=None, y=None, feed=3000):
        """Absolute Bewegung zu einer Position (G90 G1). Laser bleibt aus."""
        cmd = ["G90 G21 G1 F%g" % feed]
        if x is not None:
            cmd.append(" X%g" % x)
        if y is not None:
            cmd.append(" Y%g" % y)
        return self.command("".join(cmd))

    def go_origin(self, feed=3000):
        """Zur Arbeits-Nullpunkt-Position fahren (X0 Y0)."""
        return self.command("$J=G90 G21 F%g X0Y0" % feed)

    def set_origin(self):
        """Aktuelle Position als Arbeits-Nullpunkt setzen (G92 X0 Y0 Z0)."""
        return self.command("G92 X0 Y0 Z0")

    def frame_bounds(self, width, height, x0=0.0, y0=0.0, feed=3000,
                     power=0, repeat=1, laser_mode="M3"):
        """
        Fährt den Bounding-Box-Rahmen der Gravurfläche ab (Positionierhilfe).

        Rechteck (x0, y0) .. (x0+width, y0+height) in Werkstück-Koordinaten;
        Standard-Ursprung ist die aktuelle Position (wie bei der Gravur).

        power=0  -> Laser bleibt AUS, nur Bewegung (Kopf zeigt die Lage).
        power>0  -> Laser konstant (M3) auf diesem S-Wert. NIEDRIG halten:
                    es soll ein sichtbarer, NICHT brennender Punkt sein.

        Gibt die gesammelten Antworten als Text zurück.
        """
        if width <= 0 or height <= 0:
            raise LaserError("Rahmenmaße müssen > 0 sein (w=%s, h=%s)" % (width, height))
        x1 = x0 + width
        y1 = y0 + height
        cmds = ["G90", "G21"]
        cmds.append("G0 X%g Y%g F%g" % (x0, y0, feed))    # an Startecke (Laser aus)
        if power > 0:
            cmds.append("%s S%d" % (laser_mode, int(power)))
        corners = [(x1, y0), (x1, y1), (x0, y1), (x0, y0)]
        for _ in range(max(1, int(repeat))):
            for cx, cy in corners:
                cmds.append("G1 X%g Y%g F%g" % (cx, cy, feed))
        cmds.append("M5 S0")                              # Laser sicher aus
        out = [self.command(c).strip() for c in cmds]
        return "\n".join(o for o in out if o)

    # ------------------------------------------------------------------ #
    # Datei-Upload und -Ausführung
    # ------------------------------------------------------------------ #
    def upload(self, filepath, remote_name=None):
        """
        Lädt eine (G-Code-)Datei auf den Engraver hoch.
        Entspricht: POST /upload?path=/&PAGEID=0  (multipart/form-data, Feldname 'file')

        Der Zielname wird mit safe_remote_name() SPIFFS-tauglich gemacht;
        zum Starten denselben (sanitierten) Namen an run_file() geben.
        """
        if remote_name is None:
            remote_name = os.path.basename(filepath)
        remote_name = safe_remote_name(remote_name)
        with open(filepath, "rb") as fh:
            data = fh.read()
        body, content_type = self._build_multipart("file", remote_name, data)
        url = "%s/upload?path=/&PAGEID=0" % self.base_url
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", content_type)
        req.add_header("Content-Length", str(len(body)))
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 30)) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            raise LaserError("Upload von '%s' fehlgeschlagen: %s" % (filepath, exc)) from exc

    def run_file(self, remote_name):
        """
        Startet eine zuvor hochgeladene Datei auf dem Engraver.
        Entspricht dem App-Befehl: [ESP220]<dateiname>
        """
        if not remote_name.startswith("/"):
            remote_name = "/" + remote_name
        return self.command("[ESP220]%s" % remote_name)

    def list_files(self):
        """Versucht, die Dateiliste des Engravers abzurufen ([ESP210] / [ESP200])."""
        try:
            return self.command("[ESP210]")
        except LaserError:
            return self.command("[ESP200]")

    @staticmethod
    def _build_multipart(field_name, filename, content):
        """Erzeugt einen multipart/form-data-Body (ohne externe Abhängigkeiten)."""
        boundary = "----ImmaFiringBoundary%s" % uuid.uuid4().hex
        crlf = b"\r\n"
        lines = []
        lines.append(("--" + boundary).encode())
        disp = 'Content-Disposition: form-data; name="%s"; filename="%s"' % (field_name, filename)
        lines.append(disp.encode())
        lines.append(b"Content-Type: application/octet-stream")
        lines.append(b"")
        body = crlf.join(lines) + crlf + content + crlf
        body += ("--" + boundary + "--").encode() + crlf
        content_type = "multipart/form-data; boundary=%s" % boundary
        return body, content_type

    # ------------------------------------------------------------------ #
    # G-Code zeilenweise streamen (Alternative zum Upload+Run)
    # ------------------------------------------------------------------ #
    def stream_gcode(self, lines, progress=None, delay=0.05):
        """
        Sendet G-Code Zeile für Zeile über das command-Interface.
        Nützlich für kleine Jobs oder zum Testen. 'lines' ist iterierbar.
        progress(i, total, line) wird optional pro Zeile aufgerufen.

        ACHTUNG: ESP3D bestätigt den HTTP-Aufruf oft, BEVOR GRBL die Zeile
        abgearbeitet hat -- es gibt keine echte Flusskontrolle. Bei langen
        Jobs kann der GRBL-Puffer überlaufen und Zeilen verlieren. Für echte
        Jobs upload() + run_file() verwenden. 'delay' (Sekunden) bremst das
        Senden zusätzlich ab.
        """
        lines = [l.strip() for l in lines if l.strip() and not l.strip().startswith(";")]
        total = len(lines)
        for i, line in enumerate(lines, 1):
            self.command(line)
            if progress:
                progress(i, total, line)
            if delay:
                time.sleep(delay)


# ====================================================================== #
# Kommandozeilen-Werkzeug
# ====================================================================== #
def _build_cli():
    p = argparse.ArgumentParser(
        description="Linux-Client für GRBL_ESP32-Laser-Engraver (WiFi).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Beispiele:\n"
               "  python3 immafiring.py --ip 192.168.0.1 status\n"
               "  python3 immafiring.py home\n"
               "  python3 immafiring.py jog --x 10 --feed 3000\n"
               "  python3 immafiring.py cmd '$$'\n"
               "  python3 immafiring.py upload motiv.gcode\n"
               "  python3 immafiring.py run motiv.gcode\n"
               "  python3 immafiring.py monitor\n",
    )
    p.add_argument("--ip", default=DEFAULT_IP, help="IP des Engravers (Standard: %s)" % DEFAULT_IP)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("test", help="Verbindung testen")
    sub.add_parser("status", help="Status + Position abfragen")
    sub.add_parser("home", help="Referenzfahrt ($H)")
    sub.add_parser("unlock", help="Alarm zurücksetzen ($X)")
    sub.add_parser("reset", help="Soft-Reset / Abbruch (Ctrl-X)")
    sub.add_parser("hold", help="Pause (Feed-Hold)")
    sub.add_parser("resume", help="Fortsetzen (~)")
    sub.add_parser("settings", help="GRBL-Einstellungen ($$)")
    sub.add_parser("info", help="Firmware-Info ($I)")
    sub.add_parser("laseroff", help="Laser sofort aus (M5 S0)")
    sub.add_parser("origin", help="Zum Nullpunkt fahren (X0 Y0)")
    sub.add_parser("setorigin", help="Aktuelle Position als Nullpunkt setzen")
    sub.add_parser("files", help="Dateien auf dem Gerät auflisten")

    pc = sub.add_parser("cmd", help="Beliebigen Roh-Befehl senden")
    pc.add_argument("text", help="Befehlstext, z. B. '$$' oder 'G0 X10'")

    pj = sub.add_parser("jog", help="Jog-Bewegung (relativ)")
    pj.add_argument("--x", type=float)
    pj.add_argument("--y", type=float)
    pj.add_argument("--z", type=float)
    pj.add_argument("--feed", type=float, default=3000)
    pj.add_argument("--absolute", action="store_true", help="absolut statt relativ")

    pm = sub.add_parser("moveto", help="Absolute Bewegung (Laser aus)")
    pm.add_argument("--x", type=float)
    pm.add_argument("--y", type=float)
    pm.add_argument("--feed", type=float, default=3000)

    pu = sub.add_parser("upload", help="Datei hochladen")
    pu.add_argument("file")
    pu.add_argument("--name", help="Zielname auf dem Gerät")

    pr = sub.add_parser("run", help="Hochgeladene Datei starten ([ESP220])")
    pr.add_argument("name")

    ps = sub.add_parser("stream", help="G-Code-Datei zeilenweise streamen")
    ps.add_argument("file")

    pf = sub.add_parser("frame", help="Bounding-Box abfahren (Positionierhilfe)")
    pf.add_argument("--width", type=float, required=True, help="Breite (mm)")
    pf.add_argument("--height", type=float, required=True, help="Höhe (mm)")
    pf.add_argument("--x0", type=float, default=0.0, help="Start-X (Standard 0)")
    pf.add_argument("--y0", type=float, default=0.0, help="Start-Y (Standard 0)")
    pf.add_argument("--feed", type=float, default=3000)
    pf.add_argument("--power", type=int, default=0,
                    help="S-Wert; 0=Laser aus. >0 NIEDRIG halten (sichtbar, nicht brennen)")
    pf.add_argument("--repeat", type=int, default=1, help="Anzahl Umläufe")

    pmon = sub.add_parser("monitor", help="Status fortlaufend anzeigen")
    pmon.add_argument("--interval", type=float, default=0.5)
    return p


def main(argv=None):
    args = _build_cli().parse_args(argv)
    laser = LaserClient(args.ip, args.port, args.timeout)
    a = args.action
    try:
        if a == "test":
            print("OK – Engraver antwortet." if laser.test_connection()
                  else "Keine Antwort von %s" % laser.base_url)
        elif a == "status":
            st = laser.status()
            print("Status : %s" % (st["state"] or "?"))
            print("MPos   : %s" % (st["mpos"],))
            print("WPos   : %s" % (st["wpos"],))
            print("Roh    : %s" % st["raw"])
        elif a == "home":
            print(laser.home())
        elif a == "unlock":
            print(laser.unlock())
        elif a == "reset":
            print(laser.reset())
        elif a == "hold":
            print(laser.hold())
        elif a == "resume":
            print(laser.resume())
        elif a == "settings":
            print(laser.settings())
        elif a == "info":
            print(laser.build_info())
        elif a == "laseroff":
            print(laser.laser_off())
        elif a == "origin":
            print(laser.go_origin())
        elif a == "setorigin":
            print(laser.set_origin())
        elif a == "files":
            print(laser.list_files())
        elif a == "cmd":
            print(laser.command(args.text))
        elif a == "jog":
            print(laser.jog(args.x, args.y, args.z, feed=args.feed,
                            relative=not args.absolute))
        elif a == "moveto":
            print(laser.move_to(args.x, args.y, feed=args.feed))
        elif a == "upload":
            print(laser.upload(args.file, args.name))
        elif a == "run":
            print(laser.run_file(args.name))
        elif a == "stream":
            with open(args.file) as fh:
                lines = fh.readlines()
            laser.stream_gcode(
                lines,
                progress=lambda i, t, l: print("\r%d/%d  %s        " % (i, t, l[:40]),
                                                end="", flush=True))
            print("\nFertig.")
        elif a == "frame":
            print(laser.frame_bounds(args.width, args.height, x0=args.x0, y0=args.y0,
                                     feed=args.feed, power=args.power, repeat=args.repeat))
        elif a == "monitor":
            laser.monitor(interval=args.interval)
    except LaserError as exc:
        print("Fehler: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
