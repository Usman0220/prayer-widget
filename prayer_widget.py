#!/usr/bin/env python3
"""Prayer times desktop widget for XFCE (GTK3 + Cairo, no browser).

Sits flat on the desktop, always below normal windows, translucent dark card.
Left click opens the full 60-day page, right click for menu, drag to move.
Times come from the Aladhan API (University of Islamic Sciences, Karachi)
and are cached in ~/.config/prayer-widget/times.json so the widget still
works offline.
"""

import json
import math
import os
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("PangoCairo", "1.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402
import cairo  # noqa: E402

APP = "prayer-widget"
CFG_DIR = os.path.expanduser("~/.config/" + APP)
CFG_PATH = os.path.join(CFG_DIR, "config.json")
CACHE_PATH = os.path.join(CFG_DIR, "times.json")
FULL_PAGE = os.path.expanduser("~/Documents/prayer.html")

DEFAULTS = {
    "city": "",          # filled in by the first location lookup
    "lat": None,         # nothing is assumed until we locate you
    "lon": None,
    "method": 1,      # 1 = University of Islamic Sciences, Karachi
    "school": 1,      # 1 = Hanafi (Asr)
    "pos": None,      # [x, y] once the user drags it
    "size": None,     # [w, h] once the user resizes it
    "always_on_top": False,
    "dark": True,
    # notifications
    "notify": True,       # desktop popup at each adhan
    "notify_lead": True,  # warning before the adhan
    "lead_minutes": 10,
    "sound": True,        # chime with the adhan popup
    # display
    "clock24": False,     # 12-hour clock by default
    "hijri_shift": 0,     # -1 / 0 / +1 day moon-sighting correction
}

# Aladhan calculation methods, ordered roughly by how common they are here.
METHODS = [
    (1, "Karachi \u00b7 Univ. of Islamic Sciences"),
    (3, "Muslim World League"),
    (2, "ISNA \u00b7 North America"),
    (4, "Umm al-Qura \u00b7 Makkah"),
    (5, "Egyptian General Authority"),
    (7, "Tehran \u00b7 Univ. of Tehran"),
    (8, "Gulf Region"),
    (9, "Kuwait"),
    (10, "Qatar"),
    (11, "Singapore \u00b7 MUIS"),
    (12, "France \u00b7 UOIF"),
    (13, "Turkey \u00b7 Diyanet"),
    (14, "Russia \u00b7 Spiritual Adm."),
    (15, "Moonsighting Committee"),
    (16, "Dubai"),
    (17, "Malaysia \u00b7 JAKIM"),
    (18, "Tunisia"),
    (20, "Indonesia \u00b7 KEMENAG"),
    (23, "Jordan \u00b7 Awqaf"),
]

SOUND_FILES = [
    "/usr/share/sounds/freedesktop/stereo/bell.oga",
    "/usr/share/sounds/freedesktop/stereo/complete.oga",
    "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga",
]

_Notify = None
_notify_tried = False

PRAYERS = ["Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha"]
COUNTED = ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]
CUR_END = {"Fajr": "Sunrise", "Dhuhr": "Asr", "Asr": "Maghrib",
           "Maghrib": "Isha"}

# Indo-Pak "mamnu" (forbidden) windows, same convention as prayer.html:
#   Tulu   sunrise -> +20 min (until Ishraq)
#   Istiwa 10 min before Dhuhr -> Dhuhr (zawal)
#   Ghurub 20 min before Maghrib -> Maghrib (sunset)
FORBIDDEN = [
    ("Tulu", "Sunrise", 0, 20),
    ("Istiwa", "Dhuhr", 10, 0),
    ("Ghurub", "Maghrib", 20, 0),
]

HIJRI_MONTHS = ["Muh", "Saf", "Rab I", "Rab II", "Jum I", "Jum II",
                "Raj", "Sha", "Ram", "Shaw", "Dhul Q", "Dhul H"]
SCHEMA = 2

INK = (0.97, 0.98, 1.0, 1.0)
INK_DIM = (0.78, 0.81, 0.88, 1.0)
ACCENT = (0.52, 0.73, 1.0, 1.0)
WARN = (0.88, 0.55, 0.25, 1.0)
PASSED = (0.76, 0.79, 0.87, 0.78)

NOW_OVERRIDE = None   # set by --at HH:MM for rendering deterministic previews


# --------------------------------------------------------------------------- #
# config + cache
# --------------------------------------------------------------------------- #
def load_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return fallback


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def load_cfg():
    cfg = dict(DEFAULTS)
    cfg.update(load_json(CFG_PATH, {}) or {})
    if cfg.get("city"):
        clean = short_label(cfg["city"])
        if clean != cfg["city"]:
            cfg["city"] = clean
            save_cfg(cfg)
    return cfg


def save_cfg(cfg):
    save_json(CFG_PATH, cfg)


def local_tz():
    """IANA name Aladhan can use, so timings line up with the system clock."""
    tz = os.environ.get("TZ", "")
    if "/" in tz:
        return tz
    for path in ("/etc/timezone", "/var/db/zoneinfo"):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                val = fh.read().strip()
            if "/" in val:
                return val
        except Exception:
            pass
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
def to_min(hhmm):
    try:
        h, m = hhmm.split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return None


def fmt_12h(hhmm):
    h, m = (int(x) for x in hhmm.split(":")[:2])
    ampm = "AM" if h < 12 else "PM"
    h12 = 12 if h % 12 == 0 else h % 12
    return "%d:%02d %s" % (h12, m, ampm)


def clean_time(raw):
    """'05:17 (PKT)' -> '05:17'."""
    raw = (raw or "").strip()
    return raw.split()[0] if raw else ""


def http_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "prayer-widget/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def fetch_month(year, month, cfg):
    params = {
        "latitude": "%.6f" % cfg["lat"],
        "longitude": "%.6f" % cfg["lon"],
        "method": cfg["method"],
        "school": cfg["school"],
    }
    tz = local_tz()
    if tz:
        params["timezonestring"] = tz
    url = "https://api.aladhan.com/v1/calendar/%d/%d?%s" % (
        year, month, urllib.parse.urlencode(params))
    payload = http_json(url)
    rows = (payload or {}).get("data") or []
    days = {}
    for row in rows:
        try:
            g = row["date"]["gregorian"]["date"]        # DD-MM-YYYY
            d, m, y = g.split("-")
            iso = "%s-%s-%s" % (y, m, d)
            t = row["timings"]
            hij = row["date"]["hijri"]
            try:
                month_no = int(hij["month"]["number"])
            except (KeyError, TypeError, ValueError):
                month_no = 0
            month_short = HIJRI_MONTHS[month_no - 1] if 1 <= month_no <= 12 else hij["month"]["en"]
            days[iso] = {
                "Fajr": clean_time(t.get("Fajr")),
                "Sunrise": clean_time(t.get("Sunrise")),
                "Dhuhr": clean_time(t.get("Dhuhr")),
                "Asr": clean_time(t.get("Asr")),
                "Maghrib": clean_time(t.get("Maghrib")),
                "Isha": clean_time(t.get("Isha")),
                "hijri": "%s %s %s" % (hij["day"], month_short, hij["year"]),
                "hijri_parts": {"day": int(hij["day"]), "month": month_no,
                                "year": int(hij["year"])},
            }
        except (KeyError, TypeError, ValueError):
            continue
    if not days:
        raise RuntimeError("empty calendar response")
    return days


def cache_matches(cache, cfg):
    if not cache or cfg.get("lat") is None or cfg.get("lon") is None:
        return False
    try:
        return (abs(float(cache.get("lat")) - float(cfg["lat"])) < 1e-6
                and abs(float(cache.get("lon")) - float(cfg["lon"])) < 1e-6
                and int(cache.get("method")) == int(cfg["method"])
                and int(cache.get("school")) == int(cfg["school"]))
    except (TypeError, ValueError):
        return False


def need_fetch(cache, cfg, force=False):
    if force or not cache_matches(cache, cfg) or cache.get("schema") != SCHEMA:
        return True
    days = cache.get("days") or {}
    today = datetime.now().date()
    for offset in (0, 1, 2):
        if (today + timedelta(days=offset)).isoformat() not in days:
            return True
    return False


def refresh_cache(cfg, force=False):
    """Blocking. Returns (cache, error_or_None)."""
    cache = load_json(CACHE_PATH, None) or {}
    if not need_fetch(cache, cfg, force):
        return cache, None
    days = dict(cache.get("days") or {}) if cache_matches(cache, cfg) else {}
    today = datetime.now().date()
    months = {(today.year, today.month)}
    horizon = today + timedelta(days=2)
    months.add((horizon.year, horizon.month))
    error = None
    for year, month in sorted(months):
        try:
            days.update(fetch_month(year, month, cfg))
        except Exception as exc:  # network, DNS, API hiccup
            error = "%s: %s" % (type(exc).__name__, exc)
    if not days:
        return cache, error or "no data"
    # keep the cache small: drop days older than a week
    cutoff = (today - timedelta(days=7)).isoformat()
    days = {k: v for k, v in days.items() if k >= cutoff}
    cache = {
        "schema": SCHEMA,
        "city": cfg["city"], "lat": cfg["lat"], "lon": cfg["lon"],
        "method": cfg["method"], "school": cfg["school"],
        "updated": datetime.now().timestamp(), "days": days,
    }
    save_json(CACHE_PATH, cache)
    return cache, error


def geocode_many(query, count=8):
    """Search a city name, newest Open-Meteo geocoder, up to `count` hits."""
    url = ("https://geocoding-api.open-meteo.com/v1/search?"
           + urllib.parse.urlencode({"name": query, "count": count, "language": "en"}))
    results = (http_json(url, timeout=12) or {}).get("results") or []
    out = []
    for hit in results:
        try:
            sub = ", ".join(x for x in (hit.get("admin1"), hit.get("country")) if x)
            out.append((hit.get("name") or "?", sub,
                        float(hit["latitude"]), float(hit["longitude"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def geocode(query):
    url = ("https://geocoding-api.open-meteo.com/v1/search?"
           + urllib.parse.urlencode({"name": query, "count": 1, "language": "en"}))
    results = (http_json(url, timeout=12) or {}).get("results") or []
    if not results:
        raise RuntimeError("city not found")
    hit = results[0]
    label = hit["name"]
    if hit.get("admin1"):
        label += ", " + hit["admin1"]
    return label, float(hit["latitude"]), float(hit["longitude"])


def short_label(name):
    name = (name or "").split(",")[0].strip()
    for suffix in (" Sadar Tehsil", " Tehsil", " District", " Division", " City"):
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
            break
    if len(name) > 24:
        name = name[:23].rstrip() + "\u2026"
    return name or "My location"


# --------------------------------------------------------------------------- #
# desktop notifications
# --------------------------------------------------------------------------- #
def notify_backend():
    """libnotify via GI when it is installed, else None (notify-send fallback)."""
    global _Notify, _notify_tried
    if _notify_tried:
        return _Notify
    _notify_tried = True
    try:
        gi.require_version("Notify", "0.7")
        from gi.repository import Notify
        Notify.init("Prayer Widget")
        _Notify = Notify
    except Exception:
        _Notify = None
    return _Notify


def sound_files():
    return [p for p in SOUND_FILES if os.path.exists(p)]


def detect_location():
    """Best-effort location from the network: (label, lat, lon).

    No GeoClue on this box, so IP geolocation it is. ipwho.is first
    (https), ip-api.com as a fallback.
    """
    providers = [
        ("https://ipwho.is/",
         lambda j: (j.get("city"), j.get("latitude"), j.get("longitude")),
         lambda j: j.get("success") is not False),
        ("http://ip-api.com/json/?fields=status,city,lat,lon",
         lambda j: (j.get("city"), j.get("lat"), j.get("lon")),
         lambda j: j.get("status") == "success"),
    ]
    problems = []
    for url, take, ok in providers:
        try:
            payload = http_json(url, timeout=8)
            if not ok(payload):
                problems.append("rejected answer")
                continue
            city, lat, lon = take(payload)
            if lat is None or lon is None:
                problems.append("no coordinates")
                continue
            return short_label(city), float(lat), float(lon)
        except Exception as exc:
            problems.append("%s: %s" % (type(exc).__name__, exc))
    raise RuntimeError("; ".join(problems) or "no provider answered")


# --------------------------------------------------------------------------- #
# widget
# --------------------------------------------------------------------------- #
class PrayerWidget(Gtk.Window):
    W = 330
    PAD = 22
    H = 376
    MIN_W = 262
    MIN_H = 344
    GRIP = 22

    def __init__(self, preview=False):
        Gtk.Window.__init__(self, type=Gtk.WindowType.TOPLEVEL)
        self.preview = preview
        self.cfg = load_cfg()
        self.cache = load_json(CACHE_PATH, {}) or {}
        self.err = None
        self.status = None
        self.refreshing = False
        self.locating = False
        self._press = None
        self._drag = False
        self._resize = None
        self._alloc = (self.W, self.H)
        self._save_timer = 0
        self._cursor_name = None
        self._fired = set()
        self._fired_day = None

        self.set_decorated(False)
        self.set_resizable(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.stick()
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.apply_layer()
        pos = self.cfg.get("pos")
        w, h = self.saved_size(*(pos if isinstance(pos, list) and len(pos) == 2
                                 else (None, None)))
        self.set_default_size(w, h)
        # a minimum, not an exact size: the card adapts to whatever the
        # user drags it to
        self.set_size_request(self.MIN_W, self.MIN_H)

        screen = self.get_screen()
        self.composited = bool(screen.is_composited())
        if self.composited:
            visual = screen.get_rgba_visual()
            if visual:
                self.set_visual(visual)
        self.set_app_paintable(True)

        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK |
                        Gdk.EventMask.BUTTON_RELEASE_MASK |
                        Gdk.EventMask.BUTTON1_MOTION_MASK |
                        Gdk.EventMask.POINTER_MOTION_MASK)

        self.connect("draw", self.on_draw)
        self.connect("button-press-event", self.on_press)
        self.connect("button-release-event", self.on_release)
        self.connect("motion-notify-event", self.on_motion)
        self.connect("size-allocate", self.on_size_allocate)
        self.connect("destroy", Gtk.main_quit)

        self.place_window()
        if preview:
            return
        display = self.get_screen().get_display()
        screen = self.get_screen()
        if screen:
            screen.connect("monitors-changed", self.on_monitors_changed)
        if display:
            display.connect("monitor-added", self.on_monitors_changed)
            display.connect("monitor-removed", self.on_monitors_changed)
        self.show_all()
        GLib.timeout_add_seconds(1, self.tick)
        GLib.timeout_add_seconds(6 * 3600, self.refresh_async)
        if self.has_location():
            self.refresh_async()
        else:
            self.locate_async()

    def has_location(self):
        return self.cfg.get("lat") is not None and self.cfg.get("lon") is not None

    def locate_async(self):
        if self.locating:
            return False
        self.locating = True
        self.queue_draw()

        def work():
            try:
                label, lat, lon = detect_location()
            except Exception as exc:
                GLib.idle_add(self.locate_failed, str(exc))
                return
            GLib.idle_add(self.locate_done, label, lat, lon)

        threading.Thread(target=work, daemon=True).start()
        return False

    def locate_done(self, label, lat, lon):
        self.locating = False
        first_time = not self.has_location()
        self.cfg.update({"city": label, "lat": lat, "lon": lon})
        save_cfg(self.cfg)
        self.cache = {}
        self.refresh_async(force=True)
        if not first_time:
            self.notify("Location updated \u00b7 " + label)
        return False

    def locate_failed(self, msg):
        self.locating = False
        self.notify("location failed \u00b7 set city")
        self.queue_draw()
        return False

    # ---------------- window behaviour ---------------- #
    def apply_layer(self):
        """Normal window pinned below every other window: above the wallpaper,
        under browsers/terminals. (DESKTOP hint hides it beneath xfdesktop.)
        """
        if self.cfg.get("always_on_top"):
            self.set_keep_below(False)
            self.set_type_hint(Gdk.WindowTypeHint.NORMAL)
            self.set_keep_above(True)
        else:
            self.set_keep_above(False)
            self.set_type_hint(Gdk.WindowTypeHint.NORMAL)
            self.set_keep_below(True)

    def place_window(self):
        pos = self.cfg.get("pos")
        w, h = self.saved_size(*(pos if isinstance(pos, list) and len(pos) == 2
                                 else (None, None)))
        if isinstance(pos, list) and len(pos) == 2:
            x, y, w, h = self.clamp_geometry(pos[0], pos[1], w, h)
            if [w, h] != list(self.cfg.get("size") or [0, 0]):
                self.set_default_size(w, h)
                self._alloc = (w, h)
            self.move(x, y)
            return
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geo = monitor.get_workarea()
        x = geo.x + geo.width - w - 24
        y = geo.y + 24
        x, y, w, h = self.clamp_geometry(x, y, w, h)
        self.move(x, y)

    # ---------------- monitor geometry ---------------- #
    def workarea_for(self, x=None, y=None):
        """(x, y, w, h) of the monitor holding the point, else the primary one."""
        display = Gdk.Display.get_default()
        areas = []
        if display:
            for i in range(display.get_n_monitors()):
                g = display.get_monitor(i).get_workarea()
                areas.append((g.x, g.y, g.width, g.height))
        if not areas:
            return (0, 0, max(self.W, 640), max(self.H, 480))
        if x is not None and y is not None:
            for area in areas:
                ax, ay, aw, ah = area
                if ax <= x < ax + aw and ay <= y < ay + ah:
                    return area
        if display:
            primary = display.get_primary_monitor() or display.get_monitor(0)
            g = primary.get_workarea()
            return (g.x, g.y, g.width, g.height)
        return areas[0]

    def clamp_geometry(self, x, y, w, h):
        """Keep the card inside the work area: monitor removed, resolution
        changed or a stale saved position can otherwise strand it off-screen."""
        ax, ay, aw, ah = self.workarea_for(x, y)
        w = max(self.MIN_W, min(int(w), aw))
        h = max(self.MIN_H, min(int(h), ah))
        x = min(max(ax, int(x)), ax + aw - w)
        y = min(max(ay, int(y)), ay + ah - h)
        return x, y, w, h

    def on_monitors_changed(self, *_a):
        x, y = self.get_position()
        w, h = self._alloc
        cx, cy, cw, ch = self.clamp_geometry(x, y, w, h)
        if (cw, ch) != (w, h):
            self.resize(cw, ch)
        if (cx, cy) != (x, y):
            self.move(cx, cy)
        if self.cfg.get("size") != [cw, ch] or self.cfg.get("pos") not in (None, [cx, cy]):
            self.cfg["size"] = [cw, ch]
            if self.cfg.get("pos") is not None:
                self.cfg["pos"] = [cx, cy]
            save_cfg(self.cfg)
        self.queue_draw()
        return False

    def saved_size(self, x=None, y=None):
        size = self.cfg.get("size")
        if isinstance(size, list) and len(size) == 2:
            try:
                w, h = int(size[0]), int(size[1])
            except (TypeError, ValueError):
                pass
            else:
                _ax, _ay, aw, ah = self.workarea_for(x, y)
                return (max(self.MIN_W, min(w, aw)), max(self.MIN_H, min(h, ah)))
        return (self.W, self.H)

    def now(self):
        return NOW_OVERRIDE or datetime.now()

    def tick(self):
        now = self.now()
        try:
            self.check_notifications(now)
        except Exception:
            pass
        # Belt and braces: if a release was swallowed (pointer left the card,
        # WM took the grab, ...) stop the move/resize gesture instead of
        # letting every later mouse move keep resizing the window.
        if (self._resize is not None or self._press) and not self.pointer_button_down():
            self.end_gesture()
        self.queue_draw()
        return True  # keep ticking

    # ---------------- adhan notifications ---------------- #
    def clock(self, hhmm):
        """Time string in the user's preferred clock (12h with AM/PM or 24h)."""
        if not hhmm:
            return hhmm
        return hhmm[:5] if self.cfg.get("clock24") else fmt_12h(hhmm)

    def popup(self, title, body, critical=False, sound=False):
        """Desktop notification: libnotify if available, notify-send if not."""
        if self.preview:
            return
        Notify = notify_backend()
        shown = False
        if Notify is not None:
            try:
                n = Notify.Notification.new(title, body, "alarm-clock")
                try:
                    n.set_urgency(Notify.Urgency.CRITICAL if critical
                                  else Notify.Urgency.NORMAL)
                except Exception:
                    pass
                n.add_action("default", "Open 60-day view",
                             lambda *_: self.open_full_page())
                n.show()
                shown = True
            except Exception:
                shown = False
        if not shown:
            try:
                cmd = ["notify-send", "-a", "prayer-widget", "-i", "alarm-clock"]
                if critical:
                    cmd += ["-u", "critical"]
                subprocess.Popen(cmd + [title, body],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except Exception:
                pass
        if sound:
            self.play_sound()

    def play_sound(self):
        """Chime with the popup, in the background so the clock never stalls."""
        if not self.cfg.get("sound"):
            return
        files = sound_files()
        if not files:
            return

        def work():
            for path in files:
                for cmd in (["paplay", path], ["aplay", "-q", path]):
                    try:
                        done = subprocess.run(cmd, timeout=30,
                                              stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL)
                        if done.returncode == 0:
                            return
                    except (FileNotFoundError, subprocess.SubprocessError):
                        continue

        threading.Thread(target=work, daemon=True).start()

    def adhan_body(self, name, day):
        city = self.cfg.get("city") or "your location"
        if name == "Fajr" and day.get("Sunrise"):
            return "%s \u00b7 sunrise %s" % (city, self.clock(day["Sunrise"]))
        nxt = CUR_END.get(name)
        if nxt and day.get(nxt):
            return "%s \u00b7 next %s %s" % (city, nxt, self.clock(day[nxt]))
        return city

    def check_notifications(self, now):
        """Fire the adhan popup and the lead reminder, once each per prayer.

        A prayer fires when `now` is within the first 70 seconds of its time,
        so a restart later in the day never replays an old adhan.
        """
        if self.preview or not self.cfg.get("notify", True):
            return
        iso, day = self.current_day()
        if not day or iso != self.today_iso():
            return
        if self._fired_day != iso:
            self._fired_day = iso
            self._fired = set()
        lead = max(0, int(self.cfg.get("lead_minutes") or 0))
        for name in COUNTED:
            raw = day.get(name)
            if not raw:
                continue
            dt = self._dt(iso, raw)
            if 0 <= (now - dt).total_seconds() <= 70:
                key = (iso, name, "adhan")
                if key not in self._fired:
                    self._fired.add(key)
                    self.popup("%s \u00b7 %s" % (name, self.clock(raw)),
                               self.adhan_body(name, day),
                               critical=(name == "Fajr"), sound=True)
            if lead and self.cfg.get("notify_lead", True):
                rem = dt - timedelta(minutes=lead)
                if 0 <= (now - rem).total_seconds() <= 70:
                    key = (iso, name, "lead")
                    if key not in self._fired:
                        self._fired.add(key)
                        self.popup("%s in %d minutes" % (name, lead),
                                   "%s at %s \u00b7 %s" % (
                                       name, self.clock(raw),
                                       self.cfg.get("city") or "your location"),
                                   sound=False)

    def open_full_page(self):
        if not os.path.exists(FULL_PAGE):
            self.notify("Full page not found: " + FULL_PAGE)
            return
        try:
            subprocess.Popen(["xdg-open", FULL_PAGE],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            self.notify("Could not open page: %s" % exc)

    # ---------------- input ---------------- #
    def on_press(self, _w, ev):
        if ev.button == 3:
            self.menu()
            return True
        if ev.button == 1:
            alloc = self.get_allocation()
            if (ev.x >= alloc.width - self.GRIP and
                    ev.y >= alloc.height - self.GRIP):
                # [press_x, press_y, w0, h0, dragging]
                self._resize = [ev.x_root, ev.y_root, alloc.width, alloc.height,
                                False]
                self._press = None
                self._drag = False
                return True
            self._press = (ev.x_root, ev.y_root, self.get_position())
            self._drag = False
        return False

    def on_motion(self, _w, ev):
        if self._resize:
            # A release that happens outside the card (or after the window
            # manager takes the grab) never reaches us. Never keep resizing
            # on bare pointer motion: drop the gesture as soon as button 1
            # is no longer down.
            if not (ev.state & Gdk.ModifierType.BUTTON1_MASK):
                self.end_gesture()
                return False
            x0, y0, w0, h0, dragging = self._resize
            dx, dy = int(ev.x_root - x0), int(ev.y_root - y0)
            if not dragging:
                if abs(dx) + abs(dy) < 4:
                    return True   # a plain click on the corner, not a drag
                self._resize[4] = True
            nw = max(self.MIN_W, w0 + dx)
            nh = max(self.MIN_H, h0 + dy)
            if (nw, nh) != (self._alloc[0], self._alloc[1]):
                self.resize(nw, nh)
            return True
        if not self._press or not (ev.state & Gdk.ModifierType.BUTTON1_MASK):
            self.hover_grip(ev)
            return False
        x0, y0, (wx, wy) = self._press
        dx, dy = int(ev.x_root - x0), int(ev.y_root - y0)
        if not self._drag and abs(dx) + abs(dy) > 5:
            self._drag = True
        if self._drag:
            self.move(wx + dx, wy + dy)
        return True

    def pointer_button_down(self):
        """Is button 1 still held? Guards against lost release events."""
        gdk_win = self.get_window()
        display = Gdk.Display.get_default()
        if not gdk_win or not display:
            return True
        seat = display.get_default_seat()
        device = seat.get_pointer() if seat else None
        if device is None:
            return True
        try:
            res = gdk_win.get_device_position(device)
            state = res[3] if isinstance(res, tuple) else res.state
        except Exception:
            return True
        return bool(state & Gdk.ModifierType.BUTTON1_MASK)

    def end_gesture(self):
        """Forget move/resize state and keep whatever size is on screen."""
        was_resizing = self._resize is not None
        self._resize = None
        self._press = None
        self._drag = False
        if was_resizing:
            alloc = self.get_allocation()
            self.persist_size(alloc.width, alloc.height)

    def on_grab_broken(self, *_a):
        self.end_gesture()
        return False

    def hover_grip(self, ev):
        alloc = self.get_allocation()
        over = (ev.x >= alloc.width - self.GRIP and
                ev.y >= alloc.height - self.GRIP)
        name = "se-resize" if over else None
        if name == self._cursor_name:
            return
        self._cursor_name = name
        gdk_win = self.get_window()
        if not gdk_win:
            return
        if name:
            cursor = Gdk.Cursor.new_from_name(self.get_display(), name)
            gdk_win.set_cursor(cursor)
        else:
            gdk_win.set_cursor(None)

    def on_release(self, _w, ev):
        if ev.button != 1:
            return False
        if self._resize:
            self.end_gesture()
            return True
        if not self._press:
            return False
        if self._drag:
            x, y = self.get_position()
            w, h = self._alloc
            cx, cy, cw, ch = self.clamp_geometry(x, y, w, h)
            if (cx, cy) != (x, y):
                self.move(cx, cy)
            if (cw, ch) != (w, h):
                self.resize(cw, ch)
            self.cfg["pos"] = [cx, cy]
            save_cfg(self.cfg)
        elif self.header_hit(ev.x, ev.y, self.get_allocation().width,
                                      self.get_allocation().height):
            self.change_city()
        else:
            self.open_full_page()
        self._press = None
        self._drag = False
        return True

    def on_size_allocate(self, _w, alloc):
        self._alloc = (alloc.width, alloc.height)
        if alloc.width < self.MIN_W or alloc.height < self.MIN_H:
            return
        if self._save_timer:
            GLib.source_remove(self._save_timer)
        self._save_timer = GLib.timeout_add(700, self.persist_size,
                                            alloc.width, alloc.height)

    def persist_size(self, w, h):
        self._save_timer = 0
        x, y = self.get_position()
        _cx, _cy, w, h = self.clamp_geometry(x, y, w, h)
        if self.cfg.get("size") != [w, h]:
            self.cfg["size"] = [w, h]
            save_cfg(self.cfg)
        if (w, h) != tuple(self._alloc):
            self.resize(w, h)
        return False

    def menu(self):
        m = Gtk.Menu()
        item = Gtk.MenuItem(label="Refresh times")
        item.connect("activate", lambda *_: self.refresh_async(force=True))
        m.append(item)

        item = Gtk.MenuItem(label="Open 60-day page")
        item.connect("activate", lambda *_: self.open_full_page())
        m.append(item)

        item = Gtk.MenuItem(label="Detect my location")
        item.connect("activate", lambda *_: self.locate_async())
        m.append(item)

        item = Gtk.MenuItem(label="Select city\u2026")
        item.connect("activate", lambda *_: self.change_city())
        m.append(item)

        item = Gtk.MenuItem(label="Notifications")
        item.set_submenu(self.notifications_menu())
        m.append(item)

        item = Gtk.MenuItem(label="Settings")
        item.set_submenu(self.settings_menu())
        m.append(item)

        item = Gtk.CheckMenuItem(label="Always on top")
        item.set_active(bool(self.cfg.get("always_on_top")))
        item.connect("toggled", self.toggle_on_top)
        m.append(item)

        item = Gtk.MenuItem(label="Reset size")
        item.connect("activate", self.reset_size)
        m.append(item)

        m.append(Gtk.SeparatorMenuItem())
        item = Gtk.MenuItem(label="Quit")
        item.connect("activate", lambda *_: Gtk.main_quit())
        m.append(item)

        m.show_all()
        m.popup_at_pointer(None)

    def notifications_menu(self):
        sub = Gtk.Menu()
        for label, key in (("Adhan popup", "notify"),
                           ("10-minute reminder", "notify_lead"),
                           ("Sound", "sound")):
            item = Gtk.CheckMenuItem(label=label)
            item.set_active(bool(self.cfg.get(key, True)))
            item.connect("toggled", self.set_flag, key)
            sub.append(item)
        sub.append(Gtk.SeparatorMenuItem())
        item = Gtk.MenuItem(label="Test notification")
        item.connect("activate", lambda *_: self.popup(
            "Maghrib test \u00b7 " + self.clock("17:11"),
            "Adhan popups are working",
            sound=self.cfg.get("sound")))
        sub.append(item)
        return sub

    def settings_menu(self):
        sub = Gtk.Menu()

        item = Gtk.MenuItem(label="Calculation method")
        item.set_submenu(self.radio_menu(
            [(label, value) for value, label in METHODS],
            int(self.cfg.get("method") or 1), self.set_method))
        sub.append(item)

        item = Gtk.MenuItem(label="Asr (school)")
        item.set_submenu(self.radio_menu(
            [("Hanafi (later Asr)", 1), ("Standard / Shafi (earlier Asr)", 0)],
            int(self.cfg.get("school") or 0), self.set_school))
        sub.append(item)

        item = Gtk.MenuItem(label="Clock")
        item.set_submenu(self.radio_menu(
            [("12-hour (5:11 PM)", False), ("24-hour (17:11)", True)],
            bool(self.cfg.get("clock24")), self.set_clock))
        sub.append(item)

        item = Gtk.MenuItem(label="Hijri date")
        item.set_submenu(self.radio_menu(
            [("Standard", 0), ("One day earlier (\u22121)", -1),
             ("One day later (+1)", 1)],
            int(self.cfg.get("hijri_shift") or 0), self.set_hijri_shift))
        sub.append(item)

        return sub

    def radio_menu(self, options, current, handler):
        sub = Gtk.Menu()
        group = None
        for label, value in options:
            item = Gtk.RadioMenuItem(label=label)
            if group is None:
                group = item
            else:
                item.join_group(group)
            item.set_active(value == current)
            item.connect("toggled", handler, value)
            sub.append(item)
        return sub

    def set_flag(self, item, key):
        self.cfg[key] = item.get_active()
        save_cfg(self.cfg)
        self.queue_draw()

    def set_method(self, item, value):
        if item.get_active():
            self.apply_choice("method", int(value))

    def set_school(self, item, value):
        if item.get_active():
            self.apply_choice("school", int(value))

    def apply_choice(self, key, value):
        """Method/school change the numbers, so drop the cache and refetch."""
        if int(self.cfg.get(key, -1)) == int(value):
            return
        self.cfg[key] = int(value)
        save_cfg(self.cfg)
        self.cache = {}
        self._fired = set()
        self.refresh_async(force=True)
        self.queue_draw()

    def set_clock(self, item, value):
        if item.get_active() and bool(self.cfg.get("clock24")) != bool(value):
            self.cfg["clock24"] = bool(value)
            save_cfg(self.cfg)
            self.queue_draw()

    def set_hijri_shift(self, item, value):
        if item.get_active() and int(self.cfg.get("hijri_shift") or 0) != int(value):
            self.cfg["hijri_shift"] = int(value)
            save_cfg(self.cfg)
            self.queue_draw()

    def toggle_on_top(self, item):
        self.cfg["always_on_top"] = item.get_active()
        save_cfg(self.cfg)
        self.apply_layer()

    def notify(self, text):
        self.status = text
        self.queue_draw()
        GLib.timeout_add_seconds(7, self._clear_err)

    def _clear_err(self):
        self.err = None
        self.status = None
        self.queue_draw()
        return False

    def change_city(self):
        dlg = Gtk.Dialog(title="Select city")
        dlg.set_default_size(360, 380)
        dlg.set_keep_above(True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL)
        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)

        entry = Gtk.Entry()
        entry.set_placeholder_text("Search a city, e.g. Faisalabad")
        box.add(entry)

        hint = Gtk.Label(label="Type at least two letters, then pick a match.")
        hint.set_xalign(0)
        hint.set_opacity(0.62)
        box.add(hint)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(250)
        box.add(scroller)

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        listbox.set_activate_on_single_click(False)
        scroller.add(listbox)

        state = {"seq": 0, "src": 0, "rows": []}

        def clear():
            for child in listbox.get_children():
                listbox.remove(child)
            state["rows"] = []

        def show(places, seq):
            if seq != state["seq"]:
                return False
            clear()
            for name, sub, lat, lon in places:
                row = Gtk.ListBoxRow()
                inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
                inner.set_border_width(6)
                title = Gtk.Label()
                title.set_markup("<b>%s</b>" % GLib.markup_escape_text(name))
                title.set_xalign(0)
                inner.add(title)
                if sub:
                    small = Gtk.Label(label=sub)
                    small.set_xalign(0)
                    small.set_opacity(0.62)
                    inner.add(small)
                row.add(inner)
                row.place = (name, sub, lat, lon)
                listbox.add(row)
                state["rows"].append(row)
            listbox.show_all()
            if state["rows"]:
                listbox.select_row(state["rows"][0])
            return False

        def do_search():
            state["src"] = 0
            query = entry.get_text().strip()
            state["seq"] += 1
            seq = state["seq"]
            if len(query) < 2:
                clear()
                return False

            def work():
                try:
                    places = geocode_many(query)
                except Exception:
                    places = []
                GLib.idle_add(show, places, seq)

            threading.Thread(target=work, daemon=True).start()
            return False

        def queue_search(*_a):
            if state["src"]:
                GLib.source_remove(state["src"])
            state["src"] = GLib.timeout_add(320, do_search)

        def choose(_lb=None, row=None):
            if row is None or not getattr(row, "place", None):
                return
            name, sub, lat, lon = row.place
            label = name + (", " + sub if sub else "")
            dlg.destroy()
            self.set_location(label, lat, lon)

        entry.connect("changed", queue_search)
        entry.connect("activate", lambda *_: choose(None, listbox.get_selected_row()))
        listbox.connect("row-activated", choose)

        dlg.show_all()
        entry.grab_focus()
        dlg.run()
        dlg.destroy()

    def set_location(self, label, lat, lon):
        self.cfg.update({"city": short_label(label), "lat": float(lat), "lon": float(lon)})
        save_cfg(self.cfg)
        self.cache = {}
        self.notify("city set \u00b7 " + short_label(label))
        self.refresh_async(force=True)

    def after_city_change(self):
        self.cache = {}
        self.refresh_async(force=True)
        return False

    def refresh_async(self, force=False):
        if self.refreshing or not self.has_location():
            return False
        self.refreshing = True
        cfg = dict(self.cfg)

        def work():
            cache, err = refresh_cache(cfg, force=force)
            GLib.idle_add(self.apply_cache, cache, err)

        threading.Thread(target=work, daemon=True).start()
        return False

    def apply_cache(self, cache, err):
        self.refreshing = False
        if cache:
            self.cache = cache
        if err:
            self.err = "Offline: %s" % err
            GLib.timeout_add_seconds(8, self._clear_err)
        self.queue_draw()
        return False

    # ---------------- data helpers ---------------- #
    def today_iso(self):
        return datetime.now().date().isoformat()

    def day(self, iso):
        days = (self.cache or {}).get("days") or {}
        return days.get(iso)

    def current_day(self):
        """Today's times, falling back to the nearest cached day."""
        days = (self.cache or {}).get("days") or {}
        if not days:
            return None, None
        today = self.today_iso()
        if today in days:
            return today, days[today]
        for iso in sorted(days):
            if iso >= today:
                return iso, days[iso]
        iso = sorted(days)[-1]
        return iso, days[iso]

    def _dt(self, iso, hhmm):
        base = datetime.strptime(iso, "%Y-%m-%d")
        h, m = (int(x) for x in hhmm.split(":")[:2])
        return base.replace(hour=h, minute=m, second=0, microsecond=0)

    def schedule(self, now):
        """(current_name, current_dt, next_name, next_dt) around `now`.

        `current` is the prayer whose window is still open: Fajr closes at
        sunrise, the others at the next prayer, Isha at tomorrow's Fajr.
        Highlighting follows `current`, so during the Maghrib window Maghrib
        stays lit and the countdown runs to Isha, instead of Isha lighting up
        the second Maghrib begins. Between sunrise and Dhuhr there is no
        current prayer, only a next one.
        """
        iso, day = self.current_day()
        if not day:
            return None
        days = self.cache.get("days") or {}
        cur_name = cur_dt = None
        upcoming = None
        for name in COUNTED:
            raw = day.get(name)
            if not raw:
                continue
            dt = self._dt(iso, raw)
            if dt <= now:
                cur_name, cur_dt = name, dt
            elif upcoming is None:
                upcoming = (name, dt)
        if cur_name and cur_name in CUR_END:
            close = day.get(CUR_END[cur_name])
            if close and now >= self._dt(iso, close):
                cur_name = cur_dt = None
        if upcoming:
            return cur_name, cur_dt, upcoming[0], upcoming[1]
        # everything has passed -> tomorrow's Fajr
        tmr = (datetime.strptime(iso, "%Y-%m-%d") + timedelta(days=1)).isoformat()
        nxt = days.get(tmr) or {}
        if nxt.get("Fajr"):
            next_dt = self._dt(tmr, nxt["Fajr"])
        else:
            next_dt = self._dt(iso, day["Fajr"]) + timedelta(days=1)
        return cur_name, cur_dt, "Fajr", next_dt

    def forbidden(self, iso, day):
        """Today's three no-prayer windows as datetimes."""
        out = []
        for name, anchor, before, after in FORBIDDEN:
            raw = day.get(anchor) if day else None
            if not raw:
                continue
            t = self._dt(iso, raw)
            out.append({
                "name": name,
                "start": t - timedelta(minutes=before),
                "end": t + timedelta(minutes=after),
            })
        return out

    def forbidden_now(self, windows, now):
        for win in windows:
            if win["start"] <= now < win["end"]:
                return win
        return None

    def forbidden_note(self, key, iso, raw):
        """The small amber second line shown under Sunrise/Dhuhr/Maghrib."""
        t = self._dt(iso, raw)
        if key == "Sunrise":
            a, b = t, t + timedelta(minutes=20)
        elif key == "Dhuhr":
            a, b = t - timedelta(minutes=10), t
        elif key == "Maghrib":
            a, b = t - timedelta(minutes=20), t
        else:
            return None
        return "forbidden time %s – %s" % (self.clock(a.strftime("%H:%M")),
                                          self.clock(b.strftime("%H:%M")))

    # ---------------- hijri date ---------------- #
    def hijri_parts(self, day):
        """(day, month, year) from the cached record, when it has them."""
        parts = (day or {}).get("hijri_parts")
        if isinstance(parts, dict):
            try:
                return (int(parts["day"]), int(parts["month"]), int(parts["year"]))
            except (KeyError, TypeError, ValueError):
                pass
        raw = ((day or {}).get("hijri") or "").split()
        if len(raw) >= 3:
            try:
                # short month names contain spaces: "6 Jum II 1448"
                return (int(raw[0]), HIJRI_MONTHS.index(" ".join(raw[1:-1])) + 1,
                        int(raw[-1]))
            except ValueError:
                pass
        return None

    def hijri_text(self, iso, day):
        """Hijri label, moved by the moon-sighting correction in the config."""
        if not day:
            return ""
        parts = self.hijri_parts(day)
        shift = int(self.cfg.get("hijri_shift") or 0)
        if not parts or not shift:
            return day.get("hijri", "")
        d, m, y = parts
        step = 1 if shift > 0 else -1
        for _ in range(abs(shift)):
            d += step
            if d > 30:              # tabular month length; Aladhan gives days
                d, m = 1, m + 1     # only for +/-1, where 29 vs 30 never
            elif d < 1:             # matters for a single step
                m -= 1
                if m < 1:
                    m, y = 12, y - 1
                d = 29
            if m > 12:
                m, y = 1, y + 1
        return "%d %s %d" % (d, HIJRI_MONTHS[m - 1], y)

    # ---------------- drawing ---------------- #
    def on_draw(self, widget, cr):
        alloc = widget.get_allocation()
        self.draw_body(cr, alloc.width, alloc.height)
        return False

    def draw_body(self, cr, w, h):
        if self.composited:
            cr.set_operator(cairo.OPERATOR_CLEAR)
            cr.paint()
            cr.set_operator(cairo.OPERATOR_OVER)
            self.card(cr, 0.5, 0.5, w - 1, h - 1, 18)
        else:
            # square, opaque card: no alpha to blend with, so no rounded edges
            cr.set_source_rgba(0.062, 0.070, 0.090, 1)
            cr.paint()
            self.card(cr, 0, 0, w, h, 0)

        k, pad = self.metrics(w, h)
        y = pad
        if self.cfg.get("city"):
            city = self.cfg["city"].upper()
        else:
            city = "LOCATING" if self.locating else "PRAYER TIMES"
        iso, day = self.current_day()
        hijri = self.hijri_text(iso, day)
        hij_w = 0
        if hijri:
            hij_w = self.text(cr, w - pad, y - 2, hijri, 10.5 * k,
                              (0.75, 0.78, 0.86, 0.95), align="right")
        label = (city + " \u25be") if self.has_location() else city
        self.text(cr, pad, y - 2, label, 11 * k, INK_DIM, tracking=1.0,
                  maxw=max(40, w - 2 * pad - hij_w - 12))
        y += 22 * k

        sched = self.schedule(self.now())
        if not sched:
            if self.locating:
                self.text(cr, pad, y + 26 * k, "Finding you\u2026", 19 * k, INK, bold=True)
                self.text(cr, pad, y + 54 * k, "one quick location lookup", 11.5 * k, INK_DIM)
            elif not self.has_location():
                self.text(cr, pad, y + 26 * k, "Location needed", 19 * k, INK, bold=True)
                self.text(cr, pad, y + 54 * k, "right click \u2192 Set city", 11.5 * k, INK_DIM)
            else:
                self.text(cr, pad, y + 26 * k, "Fetching times\u2026", 17 * k, INK_DIM)
            self.grip(cr, w, h)
            self.footer(cr, w, h)
            return False

        cur_name, cur_dt, next_name, next_dt = sched
        now = self.now()
        left = max(0, int((next_dt - now).total_seconds()))
        hrs, rem = divmod(left, 3600)
        mins, secs = divmod(rem, 60)
        countdown = "%d:%02d:%02d" % (hrs, mins, secs) if hrs else "%02d:%02d" % (mins, secs)

        # hero: the prayer in effect right now, counting toward the next one
        if cur_name:
            kicker, hero, hero_dt, hero_ink = "NOW", cur_name, cur_dt, ACCENT
            tail = "until " + next_name
        else:
            kicker, hero, hero_dt, hero_ink = "NEXT PRAYER", next_name, next_dt, INK
            tail = "remaining"

        self.text(cr, pad, y, kicker, 10 * k, (0.60, 0.68, 0.85, 1), tracking=1.6)
        y += 16 * k
        self.text(cr, pad, y, hero, 21 * k, hero_ink, bold=True,
                  maxw=max(60, w - 2 * pad - 90 * k))
        self.text(cr, w - pad, y + 5 * k, self.clock(hero_dt.strftime("%H:%M")),
                  13 * k, INK_DIM if not cur_name else ACCENT, align="right")
        y += 32 * k

        self.text(cr, pad, y, countdown, 40 * k, ACCENT, bold=True,
                  maxw=w - 2 * pad)
        self.text(cr, w - pad, y + 22 * k, tail, 10.5 * k,
                  (0.72, 0.76, 0.84, 1), align="right")
        y += 50 * k

        # progress through the window of the prayer currently in effect
        if cur_dt:
            prev_dt = cur_dt
        else:
            sun = day.get("Sunrise")
            sun_dt = self._dt(iso, sun) if sun else None
            if sun_dt and sun_dt <= now < next_dt:
                prev_dt = sun_dt  # the Fajr window closed at sunrise
            else:
                prev_dt = next_dt - timedelta(hours=8)
        span = max(1.0, (next_dt - prev_dt).total_seconds())
        frac = min(1.0, max(0.0, (now - prev_dt).total_seconds() / span))
        # Invert so the bar shows "time left": full at window start, empty before next prayer
        bar_frac = 1.0 - frac
        self.bar(cr, pad, y, w - 2 * pad, max(2.5, 3.5 * k), bar_frac)
        y += 18 * k

        # forbidden window that is running right now gets a loud amber strip
        windows = self.forbidden(iso, day)
        active = self.forbidden_now(windows, now)
        if active:
            self.warn_strip(cr, pad, y, w - 2 * pad, active, now, k)
            y += 22 * k

        # today's rows: the prayer in effect is the lit one, so at 17:10 with
        # Maghrib at 17:11 Asr still glows; Maghrib's own window then lights
        # up from 17:11 while the countdown runs to Isha. Sunrise/Dhuhr/
        # Maghrib carry their forbidden window underneath.
        rows = []
        for key in PRAYERS:
            raw = day.get(key) if day else None
            if not raw:
                continue
            rows.append((key, raw, self.forbidden_note(key, iso, raw)))
        bottom = h - pad - 20 * k
        units = sum(1.42 if sub else 1.0 for _, _, sub in rows) or 1.0
        unit = max(14.0, (bottom - y) / units)
        row_size = max(9.5, min(14 * k, unit * 0.55))
        active_forbidden = {"Sunrise": "Tulu", "Dhuhr": "Istiwa",
                            "Maghrib": "Ghurub"}
        top = y
        for key, raw, sub in rows:
            hgt = unit * (1.42 if sub else 1.0)
            dt = self._dt(iso, raw)
            is_now = key == cur_name
            passed = dt <= now
            if is_now:
                self.round_rect(cr, pad - 8, top + 2, w - 2 * pad + 16,
                                max(10.0, hgt - 4), 8)
                cr.set_source_rgba(0.52, 0.73, 1.0, 0.17)
                cr.fill()
                cr.set_source_rgba(*ACCENT)
                cr.rectangle(pad - 8, top + hgt * 0.28, 2.5, hgt * 0.44)
                cr.fill()
            if sub:
                sub_size = max(8.0, row_size * 0.62)
                content = row_size * 1.35 + sub_size * 1.5 + 1
                ty = top + max(1.0, (hgt - content) / 2.0)
            else:
                sub_size = 0
                ty = top + (hgt - row_size * 1.4) / 2.0
            if key == "Sunrise":
                color = ACCENT if is_now else (0.74, 0.77, 0.85, 0.95)
                size = row_size * 0.92
            else:
                color = ACCENT if is_now else (INK if not passed else PASSED)
                size = row_size
            self.text(cr, pad + (4 if is_now else 0), ty, key, size, color,
                      bold=is_now)
            self.text(cr, w - pad, ty, self.clock(raw), size,
                      ACCENT if is_now else (INK if not passed else PASSED),
                      align="right", bold=is_now)
            if sub:
                hot = bool(active) and active["name"] == active_forbidden.get(key)
                self.text(cr, w - pad, ty + row_size * 1.45, sub, sub_size,
                          WARN if hot else (0.91, 0.77, 0.52, 0.98),
                          align="right", bold=hot)
            top += hgt

        self.grip(cr, w, h)
        self.footer(cr, w, h)
        return False

    def warn_strip(self, cr, x, y, w, win, now, k):
        """Amber band while a forbidden (mamnu) window is running."""
        h = 19 * k
        self.round_rect(cr, x, y, w, h, 6)
        cr.set_source_rgba(0.95, 0.70, 0.32, 0.15)
        cr.fill()
        cr.set_source_rgba(*WARN)
        cr.rectangle(x, y, 2.5, h)
        cr.fill()
        secs = max(0, int((win["end"] - now).total_seconds()))
        m, s = divmod(secs, 60)
        size = 9.5 * k
        right = "ends %s (%d:%02d)" % (self.clock(win["end"].strftime("%H:%M")), m, s)
        right_w = self.text(cr, x + w - 9, y + h * 0.26, right, size, WARN,
                            align="right")
        self.text(cr, x + 9, y + h * 0.26,
                  "forbidden time \u00b7 " + win["name"].lower(), size,
                  (0.98, 0.82, 0.55, 1),
                  maxw=max(30, w - right_w - 26))

    def metrics(self, w, h=None):
        """Type scale + padding derived from both dimensions, so a tall card
        gets airier rows and a wide one gets bigger type without either
        squeezing the other."""
        h = h or self.H
        k = min(w / float(self.W), h / float(self.H))
        k = max(0.72, min(1.8, k))
        pad = max(14, min(34, int(round(self.PAD * k))))
        return k, pad

    def header_hit(self, x, y, w, h):
        k, pad = self.metrics(w, h)
        return y <= pad + 22 * k and x <= w - 60

    def reset_size(self, *_a):
        x, y = self.get_position()
        _x, _y, w, h = self.clamp_geometry(x, y, self.W, self.H)
        self.cfg["size"] = [w, h]
        save_cfg(self.cfg)
        self.resize(w, h)

    def grip(self, cr, w, h):
        """Little diagonal grip in the corner: drag it to resize."""
        x1, y1 = w - 8.5, h - 8.5
        cr.set_line_width(1.2)
        cr.set_source_rgba(1, 1, 1, 0.26)
        for i in (0, 5, 10):
            cr.move_to(x1 - i, y1)
            cr.line_to(x1, y1 - i)
            cr.stroke()

    def footer(self, cr, w, h):
        k, pad = self.metrics(w, h)
        y = h - pad + 2
        iso, _ = self.current_day()
        stamp = self.cache.get("updated")
        note = datetime.strptime(iso, "%Y-%m-%d").strftime("%a %d %b") if iso else ""
        if stamp:
            age = (self.now().timestamp() - stamp) / 60.0
            note += "  \u00b7  " + ("just updated" if age < 2 else "updated %d min ago" % age)
        size = 9.8 * k
        if self.err:
            right, right_ink = "offline", WARN
        elif self.status:
            right, right_ink = self.status, ACCENT
        else:
            right, right_ink = "click for 60-day view", (0.64, 0.68, 0.77, 0.9)
        right_w = self.text(cr, w - pad, y - 10, right, size, right_ink, align="right")
        self.text(cr, pad, y - 10, note, size, (0.70, 0.74, 0.82, 0.95),
                  maxw=max(60, w - 2 * pad - right_w - 10))

    # ---------------- primitives ---------------- #
    def round_rect(self, cr, x, y, w, h, r):
        r = min(r, w / 2, h / 2)
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def card(self, cr, x, y, w, h, r):
        # Without a compositor an ARGB window shows nothing, so fall back to
        # a fully opaque card instead of relying on the alpha channel.
        a = 0.975 if self.composited else 1.0
        self.round_rect(cr, x, y, w, h, r)
        grad = cairo.LinearGradient(0, 0, 0, h)
        grad.add_color_stop_rgba(0, 0.062, 0.070, 0.090, a)
        grad.add_color_stop_rgba(1, 0.031, 0.035, 0.046, a)
        cr.set_source(grad)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.11)
        cr.set_line_width(1)
        cr.stroke()

    def bar(self, cr, x, y, w, h, frac):
        self.round_rect(cr, x, y, w, h, h / 2)
        cr.set_source_rgba(1, 1, 1, 0.16)
        cr.fill()
        fw = max(h, w * frac)
        self.round_rect(cr, x, y, fw, h, h / 2)
        cr.set_source_rgba(*ACCENT)
        cr.fill()

    def text(self, cr, x, y, text, size, color, bold=False, align="left", tracking=0.0,
             maxw=None):
        if not text:
            return 0
        layout = PangoCairo.create_layout(cr)
        desc = Pango.FontDescription()
        desc.set_family("DejaVu Sans")
        desc.set_absolute_size(size * Pango.SCALE)
        desc.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
        layout.set_font_description(desc)
        layout.set_text(text, -1)
        layout.set_auto_dir(False)
        if tracking:
            attr = Pango.attr_letter_spacing_new(int(tracking * Pango.SCALE))
            attrs = Pango.AttrList()
            attrs.insert(attr)
            layout.set_attributes(attrs)
        if maxw:
            layout.set_width(int(maxw * Pango.SCALE))
            layout.set_ellipsize(Pango.EllipsizeMode.END)
            layout.set_single_paragraph_mode(True)
        tw, th = layout.get_pixel_size()
        if align == "right":
            x -= tw
        elif align == "center":
            x -= tw / 2.0
        cr.move_to(x, y)
        cr.set_source_rgba(*color)
        PangoCairo.show_layout(cr, layout)
        return tw


def arg_value(flag):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if len(sys.argv) > i + 1:
            return sys.argv[i + 1]
    return None


def main():
    global NOW_OVERRIDE
    if "--check" in sys.argv:
        cfg = load_cfg()
        if cfg.get("lat") is None or cfg.get("lon") is None:
            label, lat, lon = detect_location()
            cfg.update({"city": label, "lat": lat, "lon": lon})
            save_cfg(cfg)
        cache, err = refresh_cache(cfg, force=True)
        days = (cache or {}).get("days") or {}
        iso = datetime.now().date().isoformat()
        print("city:", cfg["city"], "at %.4f, %.4f" % (cfg["lat"], cfg["lon"]))
        print("days cached:", len(days), "error:", err)
        print("today:", json.dumps(days.get(iso, {}), indent=2))
        return 0

    if "--shot" in sys.argv:
        out = arg_value("--shot") or "/tmp/prayer-widget.png"
        at = arg_value("--at")
        if at:
            hh, mm = (int(x) for x in at.split(":")[:2])
            NOW_OVERRIDE = datetime.now().replace(hour=hh, minute=mm,
                                                  second=0, microsecond=0)
        Gtk.init(None)
        win = PrayerWidget(preview=True)
        win.composited = True
        w, h = win.saved_size()
        size = arg_value("--size")
        if size and "x" in size.lower():
            sw, sh = size.lower().split("x", 1)
            w, h = max(win.MIN_W, int(sw)), max(win.MIN_H, int(sh))
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        win.draw_body(cairo.Context(surf), w, h)
        surf.write_to_png(out)
        print("wrote %s (%dx%d)%s" % (out, w, h, " at " + at if at else ""))
        return 0

    # one widget at a time
    os.makedirs(CFG_DIR, exist_ok=True)
    lock = open(os.path.join(CFG_DIR, "instance.lock"), "w")
    try:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, ImportError):
        print("prayer widget is already running")
        return 0

    Gtk.init(None)
    win = PrayerWidget()
    win.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
