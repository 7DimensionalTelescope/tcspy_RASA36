#%%
"""
GCNWatcher
==========
Watches the Slack channel where the external GCN-bot posts notices and replies
in-thread with RASA36 readiness, so an observer can tell at a glance whether the
telescope could actually act on an alert.

The GCN-bot already computes target observability and tile coverage. What it
cannot see is the state of this site: current weather, whether the roof is open,
and whether the scheduler is running. That gap is what this module fills.

Readings come from the JSON history files written by the weather and safety
monitors rather than from live Alpaca calls, because a device call can block for
far longer than its timeout and this runs on a latency-sensitive path.

The watcher also keeps a short-lived index mapping each notice to its Slack
thread, so `AlertMonitor` can post ToO acceptance and observation outcome into
the same thread once a request for that notice is ingested.
"""

import datetime
import glob
import json
import os
import re
import threading
import time

from tcspy.configuration import mainConfig
from tcspy.utils.connector import SlackConnector


class GCNWatcher(mainConfig):

    def __init__(self, unitnum: int = 36):
        super().__init__(unitnum=unitnum)
        self.slack = None
        self._index_lock = threading.Lock()

    def __repr__(self):
        return f"<GCNWatcher(channel={self.config.get('ALERTBROKER_GCN_CHANNEL')})>"

    # ==================================================================
    # Setup
    # ==================================================================

    def _set_slack(self):
        if not self.slack:
            self.slack = SlackConnector(
                token_path=self.config['SLACK_TOKEN'],
                default_channel_id=self.config['SLACK_DEFAULT_CHANNEL'])

    @property
    def channel(self):
        return self.config.get('ALERTBROKER_GCN_CHANNEL', None)

    @property
    def dryrun(self):
        return bool(self.config.get('ALERTBROKER_GCN_DRYRUN', True))

    # ==================================================================
    # Notice parsing
    # ==================================================================

    # Field names differ per mission: Einstein Probe and SVOM use RA/DEC,
    # Fermi-GBM uses GRB_RA/GRB_DEC. Same for the error radius and the ID.
    _RE_RA = re.compile(r'\*?(?:GRB_)?RA:\s*([-+0-9.]+)\s*deg', re.IGNORECASE)
    _RE_DEC = re.compile(r'\*?(?:GRB_)?DEC:\s*([-+0-9.]+)\s*deg', re.IGNORECASE)
    _RE_ERR = re.compile(
        r'\*?(?:RA_DEC_ERROR|ERROR_RADIUS|GRB_ERROR):\s*([0-9.]+)\s*deg', re.IGNORECASE)
    _RE_EVENT = re.compile(r'GRB_NAME\(Probably\):\s*\*?\s*([^\n*]+)')
    _RE_TRIGGER = re.compile(
        r'\*?(?:TRIGGER_NUM|BURST_ID|ID):\s*\*?\s*([A-Za-z0-9_]+)')
    # The site line follows the "[7DT & RASA36 (Chile)]" heading.
    _RE_SITE = re.compile(
        r'\[7DT\s*&(?:amp;)?\s*RASA36[^\]]*\]\*?\s*(:[a-z_]+:)', re.IGNORECASE)

    def parse_notice(self, text: str):
        """Extract the fields needed to judge and later re-find a notice.

        Returns None when the message carries no RASA36 observability line,
        which is how non-notice chatter in the channel is filtered out.
        """
        site = self._RE_SITE.search(text or '')
        if not site:
            return None

        def _first(pattern, cast=str, default=None):
            match = pattern.search(text)
            if not match:
                return default
            try:
                return cast(match.group(1).strip())
            except (TypeError, ValueError):
                return default

        return {
            'circle': site.group(1),
            'observable': site.group(1) in (':large_green_circle:',
                                            ':large_orange_circle:'),
            'event': _first(self._RE_EVENT, default=''),
            'trigger': _first(self._RE_TRIGGER, default=''),
            'ra': _first(self._RE_RA, float),
            'dec': _first(self._RE_DEC, float),
            'radius_deg': _first(self._RE_ERR, float),
        }

    # ==================================================================
    # Site readiness
    # ==================================================================

    def _latest_record(self, directory: str):
        """Return (data, age_minutes) for the newest JSON file under directory."""
        if not directory or not os.path.isdir(directory):
            return None, None
        files = glob.glob(os.path.join(directory, '*', '*.txt'))
        if not files:
            return None, None
        try:
            with open(max(files, key=os.path.getmtime)) as f:
                data = json.load(f)
        except Exception:
            return None, None
        try:
            stamp = datetime.datetime.fromisoformat(data['update_time'])
            age = (datetime.datetime.utcnow() - stamp).total_seconds() / 60.0
        except Exception:
            age = None
        return data, age

    def _scheduler_running(self):
        """True when an appscheduler process is alive, None if undetermined."""
        try:
            for pid in os.listdir('/proc'):
                if not pid.isdigit():
                    continue
                try:
                    with open(f'/proc/{pid}/cmdline', 'rb') as f:
                        if b'appscheduler.py' in f.read():
                            return True
                except (OSError, IOError):
                    continue
            return False
        except Exception:
            return None

    def build_readiness(self):
        """Compose the in-thread readiness message."""
        stale_min = float(self.config.get('ALERTBROKER_GCN_STALE_MIN', 10))
        now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        lines = [f':satellite: *RASA36 readiness* — UTC {now}', '']

        safety, safety_age = self._latest_record(
            self.config.get('SAFEMONITOR_PATH'))
        if safety is None or safety_age is None or safety_age > stale_min:
            age = 'no data' if safety_age is None else f'{safety_age:.0f} min ago'
            lines.append(f':grey_question: *Roof state unknown* ({age})')
        elif safety.get('is_safe'):
            lines.append(f':large_green_circle: *Roof OPEN* ({safety_age*60:.0f} s ago)')
        else:
            lines.append(f':red_circle: *Roof CLOSED* — safetymonitor unsafe '
                         f'({safety_age*60:.0f} s ago)')

        weather, weather_age = self._latest_record(self.config.get('WEATHER_PATH'))
        if weather is None or weather_age is None or weather_age > stale_min:
            age = 'no data' if weather_age is None else f'{weather_age:.0f} min ago'
            lines.append(f':grey_question: Weather unknown ({age})')
        else:
            mark = (':large_green_circle:' if weather.get('is_safe')
                    else ':red_circle:')
            verdict = 'within limits' if weather.get('is_safe') else 'OUT OF LIMITS'
            lines.append(f'{mark} Weather {verdict} ({weather_age*60:.0f} s ago)')
            limits = weather.get('constraints', {}) or {}
            # Temperature is the one constraint with both a floor and a ceiling.
            temp_range = None
            if limits.get('TEMPERATURE_LOWER') is not None and limits.get('TEMPERATURE_UPPER') is not None:
                temp_range = f"{limits['TEMPERATURE_LOWER']} to {limits['TEMPERATURE_UPPER']}"
            for label, key, unit, limit_text in (
                    ('temperature', 'temperature', 'C', temp_range),
                    ('humidity', 'humidity', '%', limits.get('HUMIDITY')),
                    ('wind', 'windspeed', 'm/s', limits.get('WINDSPEED')),
                    ('rain rate', 'rainrate', '', limits.get('RAINRATE')),
                    ('sky brightness', 'skybrightness', 'mag', limits.get('SKYMAG'))):
                if key not in weather:
                    continue
                suffix = f'   limit {limit_text}' if limit_text is not None else ''
                lines.append(f'> {label:15} {weather[key]} {unit}{suffix}')
            if weather.get('cloudfraction') is not None:
                lines.append(f'> {"cloud fraction":15} {weather["cloudfraction"]} %')

        scheduler = self._scheduler_running()
        if scheduler is True:
            lines.append(':large_green_circle: Scheduler running — appscheduler.py')
        elif scheduler is False:
            lines.append(':red_circle: *Scheduler not running* — no observation will start')
        else:
            lines.append(':grey_question: Scheduler state unknown')

        # The numeric weather limits carry no cloud term, so a fully clouded but
        # otherwise nominal sky reads "safe" while the roof stays shut. Call it
        # out rather than leaving the two lines looking contradictory.
        if (weather and weather.get('is_safe')
                and safety is not None and not safety.get('is_safe')):
            lines.append('')
            lines.append('_Weather is inside every numeric limit but the roof is '
                         'closed — check cloud fraction._')

        return '\n'.join(lines)

    # ==================================================================
    # Thread index — links a notice to its Slack thread
    # ==================================================================

    def _load_json(self, path, default):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return default

    def _save_json(self, path, payload):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, 'w') as f:
            json.dump(payload, f, indent=4)

    @staticmethod
    def _normalize(name):
        """tcspy strips every space from string values (alert.py:310), so names
        must be compared with spaces removed and case ignored."""
        return str(name or '').replace(' ', '').upper()

    def record_thread(self, notice, message_ts):
        """Remember which Slack thread a notice was posted in, pruning old rows."""
        path = self.config.get('ALERTBROKER_GCN_INDEXFILE')
        if not path:
            return
        window_s = float(self.config.get('ALERTBROKER_GCN_WINDOW_HOURS', 48)) * 3600
        now = time.time()
        with self._index_lock:
            entries = [e for e in self._load_json(path, [])
                       if now - e.get('recorded_at', 0) < window_s]
            entries.append({
                'ts': message_ts,
                'trigger': str(notice.get('trigger') or ''),
                'event': self._normalize(notice.get('event')),
                'ra': notice.get('ra'),
                'dec': notice.get('dec'),
                'radius_deg': notice.get('radius_deg'),
                'recorded_at': now,
            })
            self._save_json(path, entries)

    def find_thread(self, comments = None, names = (), ra = None, dec = None):
        """Find the Slack thread for an ingested alert.

        Matching order: the GCN_TRIGGER tag the bot embeds in the mail comments,
        then the event name, then sky position. Position is last because Fermi
        refines coordinates between notices for the same event.
        """
        path = self.config.get('ALERTBROKER_GCN_INDEXFILE')
        if not path:
            return None
        entries = self._load_json(path, [])
        if not entries:
            return None

        trigger = None
        if comments:
            match = re.search(r'GCN_TRIGGER=([A-Za-z0-9_]+)', str(comments))
            if match:
                trigger = match.group(1)
        if trigger:
            for entry in reversed(entries):
                if entry.get('trigger') and entry['trigger'] == trigger:
                    return entry['ts']

        wanted = {self._normalize(n) for n in names if n}
        wanted.discard('')
        if wanted:
            for entry in reversed(entries):
                if entry.get('event') and entry['event'] in wanted:
                    return entry['ts']

        if ra is not None and dec is not None:
            fallback = float(self.config.get('ALERTBROKER_GCN_MATCH_RADIUS_DEG', 1.0))
            for entry in reversed(entries):
                if entry.get('ra') is None or entry.get('dec') is None:
                    continue
                tolerance = entry.get('radius_deg') or fallback
                if self._separation(ra, dec, entry['ra'], entry['dec']) <= tolerance:
                    return entry['ts']
        return None

    @staticmethod
    def _separation(ra1, dec1, ra2, dec2):
        """Angular separation in degrees via the haversine formula."""
        import math
        ra1, dec1, ra2, dec2 = map(math.radians, (float(ra1), float(dec1),
                                                  float(ra2), float(dec2)))
        d_ra = ra2 - ra1
        d_dec = dec2 - dec1
        a = (math.sin(d_dec / 2) ** 2
             + math.cos(dec1) * math.cos(dec2) * math.sin(d_ra / 2) ** 2)
        return math.degrees(2 * math.asin(min(1.0, math.sqrt(a))))

    # ==================================================================
    # Posting
    # ==================================================================

    def post_to_thread(self, message_ts, text):
        """Post into a GCN thread, honouring the dry-run switch."""
        if self.dryrun:
            print(f"[{datetime.datetime.now()}] GCN watcher [DRY-RUN] would reply "
                  f"to ts={message_ts}:\n{text}")
            return None
        self._set_slack()
        return self.slack.post_thread_message(
            message_ts, text=text, channel=self.channel)

    # ==================================================================
    # Poll loop
    # ==================================================================

    def _poll_once(self, last_ts):
        """Handle every new notice since last_ts, returning the newest ts seen."""
        self._set_slack()
        messages = self.slack.get_messages_since(last_ts, channel=self.channel)
        botuser = self.config.get('ALERTBROKER_GCN_BOTUSER')
        newest = last_ts
        # conversations_history returns newest first; replay oldest first so the
        # stored cursor advances monotonically even if a post fails midway.
        for message in sorted(messages, key=lambda m: float(m.get('ts', 0))):
            ts = message.get('ts')
            if ts is None:
                continue
            newest = max(newest, ts, key=float)
            if botuser and message.get('user') != botuser:
                continue
            notice = self.parse_notice(message.get('text', ''))
            if notice is None:
                continue
            if not notice['observable']:
                print(f"[{datetime.datetime.now()}] GCN watcher: skip ts={ts} "
                      f"({notice['event'] or 'unnamed'}) — RASA36 {notice['circle']}")
                continue
            self.record_thread(notice, ts)
            if self.config.get('ALERTBROKER_GCN_POST_READINESS', True):
                try:
                    self.post_to_thread(ts, self.build_readiness())
                except Exception as e:
                    print(f"[{datetime.datetime.now()}] GCN watcher: failed to post "
                          f"readiness for ts={ts}: {e}")
        return newest

    def run(self):
        """Poll the GCN channel forever. Intended to run in a daemon thread."""
        if not self.config.get('ALERTBROKER_GCN_ENABLE', False):
            print(f"[{datetime.datetime.now()}] GCN watcher disabled by config.")
            return
        if not self.channel:
            print(f"[{datetime.datetime.now()}] GCN watcher: no channel configured.")
            return

        interval = float(self.config.get('ALERTBROKER_GCN_POLL_INTERVAL', 10))
        state_path = self.config.get('ALERTBROKER_GCN_STATEFILE')
        state = self._load_json(state_path, {}) if state_path else {}
        # Start from now on a first run so a restart never replays old notices.
        last_ts = state.get('last_ts') or f'{time.time():.6f}'
        print(f"[{datetime.datetime.now()}] GCN watcher started on {self.channel} "
              f"(interval={interval}s, dryrun={self.dryrun}, from ts={last_ts}).")

        while True:
            try:
                newest = self._poll_once(last_ts)
                if newest != last_ts:
                    last_ts = newest
                    if state_path:
                        self._save_json(state_path, {'last_ts': last_ts})
            except Exception as e:
                print(f"[{datetime.datetime.now()}] GCN watcher poll failed: {e}")
            time.sleep(interval)
