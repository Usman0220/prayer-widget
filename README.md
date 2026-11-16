# Prayer Times Desktop Widget

A card that sits on the XFCE desktop: next prayer, live countdown, and today's
six times. It stays **under** every normal window (browser, terminal, file
manager) and over the wallpaper — the way a desktop tile should behave.

- **Left click** the city name (top line) → city picker with search results
- **Left click** anywhere else → full 60-day page (`~/Documents/prayer.html`)
- **Drag** to move (position is remembered)
- **Drag the corner grip** (bottom-right) to resize; size is remembered
- **Right click** → menu: Refresh, Open 60-day page, Detect my location,
  Select city…, Notifications, Settings, Always on top, Reset size, Quit

## Adhan notifications

At each prayer the widget raises a desktop popup through libnotify
(`xfce4-notifyd` here), with a chime; clicking the popup opens the 60-day page.
Fajr is sent as *critical*. A second popup warns 10 minutes ahead.

Right click → **Notifications** to turn the popup, the reminder and the sound
on/off, and **Test notification** to check the whole path. Defaults: all three
on, reminder lead 10 minutes (`lead_minutes` in the config). The chime prefers
`bell.oga`, then `complete.oga`, then `alarm-clock-elapsed.oga`, played with
`paplay` (falls back to `aplay`); if libnotify is missing, `notify-send` is used.

A prayer only fires within 70 seconds of its time, so restarting the widget
later in the day never replays an old adhan. Sound plays on a background
thread and can never stall the countdown.

## Settings

Right click → **Settings**:

| Setting | Options |
|---------|---------|
| Calculation method | Karachi, MWL, ISNA, Umm al-Qura, Egyptian, Tehran, Gulf, Kuwait, Qatar, Singapore, France, Turkey, Russia, Moonsighting, Dubai, JAKIM, Tunisia, KEMENAG, Jordan |
| Asr (school) | Hanafi (later) or Standard/Shafi (earlier) |
| Clock | 12-hour (`5:11 PM`) or 24-hour (`17:11`) |
| Hijri date | Standard, one day earlier, one day later (moon-sighting difference) |

Changing method or school clears the cached calendar and refetches immediately,
because those change the numbers. Clock and Hijri are display-only.

## Location

No city is hard-coded. On first start the widget asks the network where you are
(`ipwho.is`, falling back to `ip-api.com`), then fetches times for those
coordinates. Lookup happens once and is stored in `config.json`; after that the
widget starts instantly and refreshes the calendar twice a day.

If the lookup fails you get `Location needed` on the card — right click →
Select city… to set it by hand. `Detect my location` in the menu repeats the
automatic lookup (useful after moving or when a VPN changes your egress).

## Times

- Method 1 = University of Islamic Sciences, Karachi (Fajr/Isha 18°)
- School 1 = Hanafi Asr
- Both (and the calculation method list above) are changeable from the
  **Settings** menu; no config editing needed.
- Aladhan API, with the system timezone passed explicitly so times match the
  clock. Three days are cached; if a refresh fails the card keeps working and
  shows `offline` in the footer.

## Forbidden (mamnu) times

The same Indo-Pak convention as the 60-day page, shown as a small amber line
under the prayer that anchors each window:

| Window | Range |
|--------|-------|
| Tulu | sunrise → sunrise + 20 min (until Ishraq) |
| Istiwa | Dhuhr − 10 min → Dhuhr (zawal) |
| Ghurub | Maghrib − 20 min → Maghrib (sunset) |

While one of them is running, an amber strip appears under the progress bar
(`forbidden time · ghurub · ends 5:11 PM (1:02)`) and that row's line lights up.
No forbidden time should be started inside these windows; the times themselves are
unchanged.

## Resizing

The card is resizable: drag the diagonal grip in the bottom-right corner (or
let the window manager resize it). Type scales with the smaller of the two
dimensions, rows spread into whatever height is left, and the size is stored
in `config.json` as `size`. Minimum is 262×344 so nothing ever overlaps.
**Reset size** in the right-click menu returns it to 330×376.

## Off-screen protection

Saved `pos`/`size` are clamped to the monitor's work area on startup, after a
drag, after a resize, and whenever monitors are added/removed or the primary
monitor changes. So unplugging a second screen or changing resolution can no
longer strand the card outside the visible desktop: it snaps back into the
nearest work area.

## Files

| Path | What |
|------|------|
| `prayer_widget.py` | the widget (GTK3 + Cairo) |
| `run.sh` / `restart.sh` | start / restart (single instance enforced by a lock) |
| `~/.config/autostart/prayer-widget.desktop` | starts it on login |
| `~/.config/prayer-widget/config.json` | city, coordinates, method/school, clock, Hijri shift, notification switches, position, size |
| `~/.config/prayer-widget/times.json` | cached calendar |
| `~/Documents/prayer.html` | full 60-day page opened on click |

## Commands

```sh
./restart.sh                                  # apply code/config changes
python3 prayer_widget.py --check              # print today's times, no GUI
python3 prayer_widget.py --shot out.png       # render the card offscreen
python3 prayer_widget.py --shot out.png --at 17:12 --size 450x410
```

## Notes

- The window is a borderless NORMAL window with `keep_below` + `skip_taskbar` +
  `sticky`. XFWM's `DESKTOP` hint would hide it beneath xfdesktop's wallpaper
  layer, and `DOCK` would float it above normal windows — hence `keep_below`.
- With a compositor (xfwm4 compositing, currently enabled) the card is a
  translucent rounded panel. Without one, the drawing code falls back to an
  opaque square card instead of an invisible ARGB window.
- Disable compositing again with:
  `xfconf-query -c xfwm4 -p /general/use_compositing -s false`
- Sunrise is listed but not counted in the countdown (it is not a prayer).
