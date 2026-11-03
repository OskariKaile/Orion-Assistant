"""Orion's proactive monitor -- the part of Orion that speaks first.

A background daemon thread watches local system state (CPU load, battery)
and the weather, and announces anything worth knowing without being asked.

Each kind of alert has a cooldown so Orion never nags, and alerts are held
back while Orion is muted, listening, thinking or speaking so it never talks
over itself or interrupts a command. If `psutil` isn't installed the system
checks are skipped silently.
"""

import datetime as _dt
import subprocess
import threading
import time

import briefing
import config

try:
    import psutil
except Exception:  # psutil not installed
    psutil = None

# Windows device-property key for a peripheral's battery level. Wireless mice,
# keyboards and headsets that support it expose their charge here, which a
# short PowerShell query can read.
_BATTERY_KEY = "{104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2"
_PS_BATTERY = (
    "Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
    "ForEach-Object { $b = ($_ | Get-PnpDeviceProperty -KeyName '"
    + _BATTERY_KEY + "' -ErrorAction SilentlyContinue).Data; "
    "if ($b -ne $null -and $b -ne '') { \"$($_.FriendlyName)|$b\" } }"
)


def _peripheral_batteries():
    """Return [(device_name, battery_percent), ...] for wireless peripherals
    that report a battery level to Windows. Empty list on any failure."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-Command", _PS_BATTERY],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:
        return []
    results = []
    for line in out.splitlines():
        name, sep, raw = line.partition("|")
        if not sep:
            continue
        try:
            pct = int(raw.strip())
        except ValueError:
            continue
        if name.strip():
            results.append((name.strip(), pct))
    return results


def _eod_clock(dt):
    h = dt.hour
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    if dt.minute == 0:
        return f"{h12}{suffix}"
    return f"{h12}:{dt.minute:02d}{suffix}"


class ProactiveMonitor:
    """Watches system state and lets Orion speak up on its own."""

    def __init__(self, orion):
        self._orion = orion
        self._last = {}              # alert key -> monotonic time it last fired
        self._cpu_since = None       # monotonic time sustained high CPU began
        self._weather_state = None   # last known weather category
        self._last_weather_poll = 0.0
        self._last_peripheral_poll = 0.0
        self._eod_nudged_for = None  # date the end-of-day nudge last fired
        self._eod_skipped_for = None # date the user asked to skip the nudge

    def start(self):
        """Launch the monitor on a daemon thread (no-op if disabled)."""
        if not config.PROACTIVE_ALERTS:
            return
        threading.Thread(target=self._run, daemon=True).start()

    # --- speaking ---
    def _announce(self, key, text):
        """Speak `text` if `key` is off cooldown and Orion is free to talk."""
        now = time.monotonic()
        if now - self._last.get(key, -1e9) < config.PROACTIVE_COOLDOWN:
            return
        orion = self._orion
        if not orion.running:
            return
        # Never talk over a conversation, a command, or while muted.
        if orion.state in ("muted", "listening", "thinking", "speaking"):
            return
        if orion.speaker.is_speaking():
            return
        self._last[key] = now
        orion.speaker.say(text)
        orion.show_last(text)

    # --- main loop ---
    def _run(self):
        # Stagger the first check so it doesn't collide with startup speech.
        time.sleep(config.PROACTIVE_INTERVAL)
        while self._orion.running:
            try:
                self._check_cpu()
                self._check_battery()
                self._check_peripherals()
                self._check_weather()
                self._check_end_of_day()
            except Exception:
                pass
            time.sleep(config.PROACTIVE_INTERVAL)

    # --- individual checks ---
    def _check_cpu(self):
        """Alert when the CPU has stayed hot for a sustained stretch."""
        if psutil is None:
            return
        load = psutil.cpu_percent(interval=None)
        if load < config.CPU_ALERT_PERCENT:
            self._cpu_since = None
            return
        if self._cpu_since is None:
            self._cpu_since = time.monotonic()
        elif time.monotonic() - self._cpu_since >= config.CPU_ALERT_SUSTAIN:
            self._announce(
                "cpu",
                f"Sir, your CPU has been running hot, around {round(load)} "
                "percent. Something may be working it hard.",
            )

    def _check_battery(self):
        """Alert when the battery is low and the machine is unplugged."""
        if psutil is None:
            return
        batt = psutil.sensors_battery()
        if batt is None:  # desktop, no battery
            return
        if not batt.power_plugged and \
                batt.percent <= config.BATTERY_ALERT_PERCENT:
            self._announce(
                "battery",
                f"Sir, battery is down to {round(batt.percent)} percent. "
                "You may want to plug in.",
            )

    def _check_peripherals(self):
        """Alert when a wireless peripheral (mouse, keyboard, headset) is low."""
        if not config.PROACTIVE_PERIPHERALS:
            return
        now = time.monotonic()
        if now - self._last_peripheral_poll < config.PERIPHERAL_POLL_SECONDS:
            return
        self._last_peripheral_poll = now

        for name, pct in _peripheral_batteries():
            if pct <= config.PERIPHERAL_ALERT_PERCENT:
                self._announce(
                    f"peripheral:{name.lower()}",
                    f"Sir, your {name} battery is low, at {pct} percent.",
                )

    def _check_end_of_day(self):
        """At the configured hour, flag tomorrow's earliest items (calendar
        events AND reminders) so the user knows whether to wind down."""
        if not getattr(config, "END_OF_DAY_NUDGE", True):
            return
        now = _dt.datetime.now()
        hour = getattr(config, "END_OF_DAY_HOUR", 21)
        if now.hour < hour:
            return
        if self._eod_nudged_for == now.date():
            return
        if self._eod_skipped_for == now.date():
            self._eod_nudged_for = now.date()
            return
        # Pull tomorrow from the unified agenda so reminders count too.
        early_hour = getattr(config, "END_OF_DAY_EARLY_HOUR", 10)
        tomorrow_start = (now.replace(hour=0, minute=0, second=0,
                                        microsecond=0)
                          + _dt.timedelta(days=1))
        tomorrow_end = tomorrow_start + _dt.timedelta(days=1)
        early = []  # list of (datetime, label)
        try:
            import agenda
            events = agenda.upcoming_calendar(tomorrow_start, tomorrow_end)
        except Exception:
            events = []
        for e in events:
            dt = e.get("start")
            if dt and not e.get("all_day") and dt.hour < early_hour:
                early.append((dt, e["title"]))
        try:
            import reminders
            for r in reminders.list_pending():
                try:
                    dt = _dt.datetime.fromisoformat(r["fires_at"])
                except (KeyError, ValueError):
                    continue
                if tomorrow_start <= dt < tomorrow_end and dt.hour < early_hour:
                    early.append((dt, f"reminder to {r['text']}"))
        except Exception:
            pass
        self._eod_nudged_for = now.date()
        if not early:
            return
        early.sort(key=lambda t: t[0])
        # Format up to the first two items so the line stays short.
        first = early[0]
        msg = f"Sir, heads up -- you have {first[1]} at {_eod_clock(first[0])} tomorrow"
        if len(early) >= 2:
            second = early[1]
            msg += f", and {second[1]} at {_eod_clock(second[0])}"
        if len(early) > 2:
            msg += f", plus {len(early) - 2} more"
        msg += ". You may want to wind down."
        self._announce("eod", msg)

    def skip_tonight(self):
        """Called by Orion when the user says 'skip tonight's nudge'."""
        self._eod_skipped_for = _dt.datetime.now().date()

    def _check_weather(self):
        """Announce when the weather changes category (rain, snow, clearing)."""
        if not config.PROACTIVE_WEATHER:
            return
        now = time.monotonic()
        if now - self._last_weather_poll < config.WEATHER_POLL_SECONDS:
            return
        self._last_weather_poll = now

        category = briefing.weather_category()
        if category is None:
            return
        prev, self._weather_state = self._weather_state, category
        if prev is None or prev == category:
            return  # first reading, or no change -- nothing to say

        messages = {
            "rain": "Sir, it has started raining outside.",
            "snow": "Sir, it is snowing now.",
            "storm": "Sir, a thunderstorm has moved in.",
            "clear": "Sir, the weather has cleared up outside.",
        }
        msg = messages.get(category)
        if msg:
            self._announce("weather", msg)
