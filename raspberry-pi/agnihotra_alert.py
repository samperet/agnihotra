#!/usr/bin/env python3
"""Agnihotra alert daemon for Raspberry Pi.

Computes the daily Agnihotra sunrise and sunset on-device (no internet
required after the clock is set) and pulses a GPIO pin -- wired to an LED,
an active buzzer, or a relay -- at the moment of each event. Optionally
plays the meditation bell sound too.

The astronomy is a direct port of the Agnihotra Clock web app:
sunrise/sunset are taken when the sun's CENTER crosses the geometric
horizon (solar zenith 90.0 deg, i.e. altitude 0 -- no refraction term),
so the times match the web app to the second.

Quick start:
    python3 agnihotra_alert.py --list            # print upcoming events
    python3 agnihotra_alert.py --test            # fire the alert once
    python3 agnihotra_alert.py                    # run the daemon

Configure your location and pin below, or pass --lat/--lon/--pin.
"""

import argparse
import math
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Configuration -- edit these, or override on the command line.
# ---------------------------------------------------------------------------
LAT = 40.7128            # degrees north (negative = south)
LON = -74.0060           # degrees east  (negative = west)

GPIO_PIN = 17            # BCM pin number driving the LED / buzzer / relay
ACTIVE_HIGH = True       # True for LED/active-buzzer; False for active-low relay boards

# Alerts to fire for each event, in seconds BEFORE it (0 = at the event).
# Mirrors the web app's 5-minute and 1-minute reminders plus the event itself.
PRE_ALERTS = [300, 60, 0]

# Buzzer/LED beep pattern for each alert.
BEEPS = 3
BEEP_ON = 0.20           # seconds the pin is on per beep
BEEP_OFF = 0.20          # seconds the pin is off between beeps

# Optional sound file played on each alert (set to None to disable).
# Copy meditation-bell.mp3 from the repo next to this script.
SOUND_FILE = "meditation-bell.mp3"

GRACE_SECONDS = 30       # fire an alert if we're at most this late for it
# ---------------------------------------------------------------------------


# ----------------------------- Astronomy -----------------------------------
# Faithful port of the NOAA-based solar position math in index.html.

def to_rad(deg):
    return deg * math.pi / 180


def to_deg(rad):
    return rad * 180 / math.pi


def normalize(value, span):
    return ((value % span) + span) % span


def julian_day(year, month, day, utc_hour):
    y, m = year, month
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return (math.floor(365.25 * (y + 4716))
            + math.floor(30.6001 * (m + 1))
            + day + b - 1524.5 + utc_hour / 24)


def geometric_mean_longitude(t):
    return normalize(280.46646 + t * (36000.76983 + t * 0.0003032), 360)


def geometric_mean_anomaly(t):
    return 357.52911 + t * (35999.05029 - 0.0001537 * t)


def earth_orbit_eccentricity(t):
    return 0.016708634 - t * (0.000042037 + 0.0000001267 * t)


def sun_equation_of_center(t):
    anomaly = to_rad(geometric_mean_anomaly(t))
    return (math.sin(anomaly) * (1.914602 - t * (0.004817 + 0.000014 * t))
            + math.sin(2 * anomaly) * (0.019993 - 0.000101 * t)
            + math.sin(3 * anomaly) * 0.000289)


def apparent_sun_longitude(t):
    omega = 125.04 - 1934.136 * t
    return (geometric_mean_longitude(t) + sun_equation_of_center(t)
            - 0.00569 - 0.00478 * math.sin(to_rad(omega)))


def mean_obliquity(t):
    seconds = 21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))
    return 23 + (26 + seconds / 60) / 60


def obliquity_correction(t):
    return mean_obliquity(t) + 0.00256 * math.cos(to_rad(125.04 - 1934.136 * t))


def sun_declination(t):
    return to_deg(math.asin(
        math.sin(to_rad(obliquity_correction(t)))
        * math.sin(to_rad(apparent_sun_longitude(t)))))


def equation_of_time(t):
    epsilon = to_rad(obliquity_correction(t))
    mean_long = to_rad(geometric_mean_longitude(t))
    ecc = earth_orbit_eccentricity(t)
    anomaly = to_rad(geometric_mean_anomaly(t))
    y = math.tan(epsilon / 2) ** 2
    minutes = (y * math.sin(2 * mean_long)
               - 2 * ecc * math.sin(anomaly)
               + 4 * ecc * y * math.sin(anomaly) * math.cos(2 * mean_long)
               - 0.5 * y * y * math.sin(4 * mean_long)
               - 1.25 * ecc * ecc * math.sin(2 * anomaly))
    return to_deg(minutes) * 4


def solar_elevation(year, month, day, utc_minute, lat, lon):
    t = (julian_day(year, month, day, utc_minute / 60) - 2451545) / 36525
    solar_time = normalize(utc_minute + equation_of_time(t) + 4 * lon, 1440)
    hour_angle = solar_time / 4 - 180
    dec = sun_declination(t)
    cos_zenith = (math.sin(to_rad(lat)) * math.sin(to_rad(dec))
                  + math.cos(to_rad(lat)) * math.cos(to_rad(dec))
                  * math.cos(to_rad(hour_angle)))
    return to_deg(math.asin(max(-1.0, min(1.0, cos_zenith))))


def event_utc_hours(year, month, day, lat, lon, altitude, rise):
    """UTC time-of-day (in hours, relative to 00:00 UTC of the given date)
    when the sun crosses `altitude` degrees. Returns None on polar day/night.
    altitude=0 -> Agnihotra (sun's center on the geometric horizon)."""
    noon_t = (julian_day(year, month, day, 12) - 2451545) / 36525
    approx_noon = 720 - equation_of_time(noon_t) - 4 * lon
    start = approx_noon - 720
    end = approx_noon + 720

    prev_min = start
    prev_elev = solar_elevation(year, month, day, prev_min, lat, lon) - altitude
    bracket = None

    minute = start + 5
    while minute <= end:
        elev = solar_elevation(year, month, day, minute, lat, lon) - altitude
        crossed = (prev_elev < 0 <= elev) if rise else (prev_elev >= 0 > elev)
        if crossed:
            bracket = (prev_min, minute)
            break
        prev_min = minute
        prev_elev = elev
        minute += 5

    if bracket is None:
        return None

    low, high = bracket
    for _ in range(48):
        mid = (low + high) / 2
        elev = solar_elevation(year, month, day, mid, lat, lon) - altitude
        after = (elev >= 0) if rise else (elev < 0)
        if after:
            high = mid
        else:
            low = mid

    return ((low + high) / 2) / 60


def event_datetime_utc(date, lat, lon, rise):
    hours = event_utc_hours(date.year, date.month, date.day, lat, lon, 0.0, rise)
    if hours is None:
        return None
    base = datetime(date.year, date.month, date.day, tzinfo=timezone.utc)
    return base + timedelta(hours=hours)


def upcoming_events(now_utc, lat, lon):
    """Sorted list of (datetime_utc, name) Agnihotra events still in the future.
    Scans yesterday/today/tomorrow so it is correct in any timezone."""
    today = now_utc.date()
    events = []
    for delta in (-1, 0, 1):
        d = today + timedelta(days=delta)
        for rise, name in ((True, "Sunrise"), (False, "Sunset")):
            dt = event_datetime_utc(d, lat, lon, rise)
            if dt is not None:
                events.append((dt, name))
    events.sort(key=lambda e: e[0])
    return [e for e in events if e[0] > now_utc]


# ------------------------------- Hardware ----------------------------------

class Output:
    """Thin wrapper around gpiozero so --list/--test work off-Pi too."""

    def __init__(self, pin, active_high):
        self.device = None
        try:
            from gpiozero import OutputDevice
            self.device = OutputDevice(pin, active_high=active_high,
                                       initial_value=False)
        except Exception as exc:  # not on a Pi, or no GPIO access
            print(f"[warn] GPIO unavailable ({exc}); alerts will be console-only.")

    def on(self):
        if self.device:
            self.device.on()

    def off(self):
        if self.device:
            self.device.off()


def play_sound():
    if not SOUND_FILE:
        return
    for player in (["mpg123", "-q", SOUND_FILE], ["ffplay", "-nodisp", "-autoexit",
                   "-loglevel", "quiet", SOUND_FILE]):
        try:
            subprocess.run(player, check=False)
            return
        except FileNotFoundError:
            continue


def fire_alert(output, name, lead, event_dt):
    when = "now" if lead == 0 else f"in {lead // 60} min"
    local = event_dt.astimezone()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {name} Agnihotra {when} "
          f"(event at {local.strftime('%H:%M:%S')})", flush=True)
    for _ in range(BEEPS):
        output.on()
        time.sleep(BEEP_ON)
        output.off()
        time.sleep(BEEP_OFF)
    play_sound()


# --------------------------------- Main ------------------------------------

def list_events(lat, lon):
    now = datetime.now(timezone.utc)
    print(f"Location: {lat:.4f}, {lon:.4f}")
    print(f"Now:      {now.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}\n")
    for dt, name in upcoming_events(now, lat, lon)[:6]:
        local = dt.astimezone()
        delta = dt - now
        mins = int(delta.total_seconds() // 60)
        print(f"  {name:8s} Agnihotra  {local.strftime('%a %Y-%m-%d %H:%M:%S')}"
              f"   (in {mins // 60}h {mins % 60:02d}m)")


def run_daemon(output, lat, lon):
    print("Agnihotra alert daemon started. Ctrl-C to stop.", flush=True)
    leads = sorted(set(PRE_ALERTS), reverse=True)
    while True:
        events = upcoming_events(datetime.now(timezone.utc), lat, lon)
        if not events:
            time.sleep(3600)  # polar day/night: re-check in an hour
            continue
        event_dt, name = events[0]
        for lead in leads:
            fire_at = event_dt - timedelta(seconds=lead)
            wait = (fire_at - datetime.now(timezone.utc)).total_seconds()
            if wait > 0:
                time.sleep(wait)
                fire_alert(output, name, lead, event_dt)
            elif wait > -GRACE_SECONDS:
                fire_alert(output, name, lead, event_dt)
            # else: this lead already passed -> skip it
        # event is now in the past; loop picks up the next one


def main():
    parser = argparse.ArgumentParser(description="Agnihotra GPIO alert for Raspberry Pi")
    parser.add_argument("--lat", type=float, default=LAT)
    parser.add_argument("--lon", type=float, default=LON)
    parser.add_argument("--pin", type=int, default=GPIO_PIN)
    parser.add_argument("--active-low", action="store_true",
                        help="pin is active-low (common for relay boards)")
    parser.add_argument("--list", action="store_true", help="print upcoming events and exit")
    parser.add_argument("--test", action="store_true", help="fire one alert and exit")
    args = parser.parse_args()

    if args.list:
        list_events(args.lat, args.lon)
        return

    output = Output(args.pin, active_high=not args.active_low)

    if args.test:
        fire_alert(output, "Test", 0, datetime.now(timezone.utc))
        return

    try:
        run_daemon(output, args.lat, args.lon)
    except KeyboardInterrupt:
        output.off()
        print("\nStopped.")


if __name__ == "__main__":
    sys.exit(main())
