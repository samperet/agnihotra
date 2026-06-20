# Agnihotra DIY sensor (Raspberry Pi)

Turn a Raspberry Pi into an Agnihotra alarm: it computes the daily **Agnihotra
sunrise and sunset** on-device from your latitude/longitude and pulses a GPIO
pin — wired to an **LED**, an **active buzzer**, or a **relay** — at each event.
It can also play the meditation bell.

The astronomy is a direct port of the Agnihotra Clock web app: events fire when
the sun's **center crosses the geometric horizon** (solar zenith 90.0°), so the
times match the web app exactly. No internet is needed once the Pi's clock is
set (it ships with NTP enabled by default).

---

## 1. Hardware

Any Raspberry Pi with GPIO (Zero / 3 / 4 / 5). Pick **one** output device on
**BCM GPIO 17 = physical pin 11**, with ground on **physical pin 6**:

### Option A — LED (visual)
```
GPIO17 (pin 11) ──[ 330Ω ]──►|── GND (pin 6)
                              LED
```
Long LED leg (anode) toward the resistor/GPIO, short leg (cathode) to GND.

### Option B — Active buzzer (audible, simplest)
```
GPIO17 (pin 11) ── buzzer (+)
GND   (pin 6)  ── buzzer (–)
```
Use an **active** buzzer (built-in tone). Small ones draw little current and can
run straight off the pin; for louder buzzers drive them through an NPN
transistor (e.g. 2N2222: base→1kΩ→GPIO17, emitter→GND, collector→buzzer–,
buzzer+→5V).

### Option C — Relay module (drive a lamp, bell, gong striker, etc.)
```
GPIO17 (pin 11) ── IN
5V     (pin 2)  ── VCC
GND    (pin 6)  ── GND
```
Most relay boards are **active-low** — run with `--active-low` (or set
`ACTIVE_HIGH = False`). Switch your mains/AC load on the relay's COM/NO side, and
**follow proper electrical safety for anything above low voltage.**

> Using a different pin? Pass `--pin <BCM>` or edit `GPIO_PIN`. Pin numbers are
> BCM (Broadcom) numbering, not physical positions.

---

## 2. Install

```bash
# On the Pi:
mkdir -p ~/agnihotra-pi && cd ~/agnihotra-pi
# copy agnihotra_alert.py here (and meditation-bell.mp3 from the repo, optional)

# gpiozero is preinstalled on Raspberry Pi OS. If missing:
sudo apt update && sudo apt install -y python3-gpiozero
# Pi 5 needs the lgpio backend:
sudo apt install -y python3-lgpio

# Optional sound playback:
sudo apt install -y mpg123
```

Make sure the Pi's clock is correct (timezone + NTP):
```bash
sudo raspi-config        # Localisation Options -> Timezone
timedatectl              # check "System clock synchronized: yes"
```

---

## 3. Configure

Edit the constants at the top of `agnihotra_alert.py`:

| Setting       | Meaning                                              |
|---------------|------------------------------------------------------|
| `LAT`, `LON`  | Your location in decimal degrees (W and S negative)  |
| `GPIO_PIN`    | BCM pin driving your LED/buzzer/relay                 |
| `ACTIVE_HIGH` | `False` for active-low relay boards                  |
| `PRE_ALERTS`  | Seconds-before to fire: `[300, 60, 0]` = 5 min, 1 min, and at the event |
| `BEEPS`, `BEEP_ON`, `BEEP_OFF` | Pulse pattern per alert            |
| `SOUND_FILE`  | Path to a sound file, or `None` to disable           |

All of `--lat`, `--lon`, `--pin`, `--active-low` can also be passed on the
command line.

---

## 4. Test

```bash
# Print the next several Agnihotra events (compare against the web app):
python3 agnihotra_alert.py --list

# Fire the alert once (beeps the pin + plays the bell):
python3 agnihotra_alert.py --test

# Run it for real:
python3 agnihotra_alert.py
```

`--list` works on a regular computer too (GPIO just prints a warning), which is
handy for verifying your coordinates produce the right times.

---

## 5. Run automatically on boot (systemd)

```bash
# Adjust User/paths in the unit file first if you didn't use pi + ~/agnihotra-pi
sudo cp agnihotra-alert.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agnihotra-alert.service

# Watch it:
systemctl status agnihotra-alert.service
journalctl -u agnihotra-alert.service -f
```

The service waits for time sync on boot and restarts itself on failure, so the
Pi will alert at every Agnihotra sunrise and sunset indefinitely.

---

## How it works

- `event_utc_hours(...)` brackets the horizon crossing in 5-minute steps around
  solar noon, then binary-searches to ~second precision — the same routine the
  web app uses, with `altitude = 0` (geometric horizon) for Agnihotra.
- The daemon scans yesterday/today/tomorrow each cycle, takes the next future
  event, sleeps until each configured pre-alert, fires, then repeats. Scanning a
  3-day window keeps it correct across midnight and in any timezone.
- Everything is computed locally, so it keeps working with no network.
