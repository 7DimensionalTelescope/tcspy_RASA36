#%%
"""
operation_report.py — Retrospective telescope operation-status tracker & annual statistics.

Purpose
-------
Reconstruct, per night, how the RASA36 telescope's dark observing window was spent and roll
the result up into annual statistics. Every minute of the astronomical observing window is
classified into one of three buckets:

    observing    — a science (LIGHT) frame was being taken (with a short gap-bridge so normal
                   slew / autofocus / inter-exposure overhead is not counted as downtime).
    weather_loss — not observing while the weather station OR the safety monitor reported
                   `is_safe == False`. The violated weather constraint (rain / wind / humidity
                   / cloud / temperature) is attributed for a free secondary breakdown.
    other_loss   — not observing while conditions were safe: a technical fault, a long target
                   gap, or manual downtime (i.e. "any other reason" that is not the weather).

Nights with no local record at all (weather + safety + images all absent) are reported as a
distinct `system_down` category rather than attributed to weather or other.

This is a *read-only* analyzer. It touches none of the unattended observing path
(nightobservation.py, appscheduler.py, devices/) and can be run at any time on the MCS.

Data sources (all timestamps are UTC end-to-end, so no timezone conversion is needed)
-------------------------------------------------------------------------------------
  * Observable dark window ....... utils.nightsession.NightSession
  * Weather safe/unsafe + limits . config['WEATHER_PATH']/<YYMMDD>/weatherinfo_*.txt
  * Roof / dome safety ........... config['SAFEMONITOR_PATH']/<YYMMDD>/safemonitorinfo_*.txt
  * Actual observing ............. config['IMAGE_PATH']/*/RASA36_<UTC>_<tag>_..._<exp>s_*.fits
                                   (LIGHT frames only — BIAS/DARK/FLAT are excluded)

Outputs (written to config['OPERATIONSTATS_PATH'], override with --outdir)
--------------------------------------------------------------------------
  * operation_ledger_<Y>.csv  — one row per night
  * operation_summary_<Y>.txt — headline annual numbers
  * operation_stats_<Y>.png   — monthly stacked bars, time-allocation pie, night-count by label

Usage
-----
    python utils/operation_report.py --year 2026
    python utils/operation_report.py --date 20260714          # single night (inspect)
    python utils/operation_report.py --year 2026 --start 20260420 --end 20260714

Limitations
-----------
  * `other_loss` bundles genuine faults with any inter-exposure overhead longer than
    GAP_BRIDGE_MINUTES. Separating "fault" from "overhead" authoritatively would require a live
    per-minute operation-state logger, which is intentionally not part of this retrospective tool.
  * The weather station only logs while the system is running, so `system_down` nights have
    unknown weather and are reported separately rather than split into weather vs. other.
"""

import os
import re
import glob
import json
import argparse
import datetime
from collections import Counter

import numpy as np
from astropy.time import Time
import astropy.units as u

from tcspy.configuration import mainConfig
from tcspy.utils.nightsession import NightSession

# ── Tunable constants ──────────────────────────────────────────────────────────
GAP_BRIDGE_MINUTES  = 10.0   # gaps between consecutive science frames shorter than this stay "observing"
GRID_STEP_SECONDS   = 60.0   # classification grid resolution (matches WEATHER_UPDATETIME)
FULL_NIGHT_FRAC     = 0.90   # obs_frac >= this  -> night label 'full'
MIN_OBS_FRAC        = 0.05   # obs_frac <= this  -> night label 'none_*'
STALE_SAMPLE_FACTOR = 5.0    # a weather/safety sample farther than this * cadence from a grid point is "no data"

CAL_TAGS = ('BIAS', 'DARK', 'FLAT')
# RASA36_<YYYYMMDD>_<HHMMSS>_<tag>_<filter>_<binning>_<exptime>s_<frame>.fits
_IMG_RE = re.compile(r'RASA36_(\d{8})_(\d{6})_([^_]+)_[^_]+_[^_]+_([0-9.]+)s_\d+\.fits$')
# Weather constraint violation priority (most severe first) for picking a single primary cause
_CAUSE_PRIORITY = ('rain', 'wind', 'humidity', 'cloud', 'temperature')


class OperationStats(mainConfig):
    """Retrospective operation-status analyzer for one telescope unit."""

    def __init__(self, unitnum: int = 36):
        super().__init__(unitnum=unitnum)
        self.weather_path = self.config['WEATHER_PATH']
        self.safety_path = self.config['SAFEMONITOR_PATH']
        self.image_path = self.config['IMAGE_PATH']
        self.outdir = self.config['OPERATIONSTATS_PATH']
        self.weather_cadence = float(self.config.get('WEATHER_UPDATETIME', 60))
        self.safety_cadence = float(self.config.get('SAFEMONITOR_UPDATETIME', 60))
        self._light_index = None  # lazily-built (times, exptimes) of every LIGHT frame

    # ── low-level parsing helpers ────────────────────────────────────────────

    @staticmethod
    def _iso_to_unix(iso_str: str):
        """Parse an ISO 'update_time' (UTC) into Unix seconds, or None on failure."""
        try:
            # Records use e.g. '2026-07-15T00:51:58.487'; fromisoformat handles it.
            return datetime.datetime.fromisoformat(iso_str).replace(
                tzinfo=datetime.timezone.utc).timestamp()
        except Exception:
            return None

    def _night_dir_names(self, t0_unix: float, t1_unix: float):
        """YYMMDD subdir names covering the window (records live under (update_time - 12h))."""
        names = []
        for t in (t0_unix, t1_unix):
            d = datetime.datetime.fromtimestamp(t - 12 * 3600, datetime.timezone.utc)
            names.append(d.strftime('%y%m%d'))
        return list(dict.fromkeys(names))

    def _load_status_records(self, base_path: str, prefix: str, t0_unix: float, t1_unix: float):
        """Load JSON status records (weather or safety) whose update_time falls in the window.

        Returns a list of (t_unix, record_dict) sorted by time. A generous ±1 h margin around
        the window is kept so carry-forward near dusk/dawn has a neighbour to use.
        """
        recs = []
        margin = 3600.0
        for name in self._night_dir_names(t0_unix, t1_unix):
            for fpath in glob.glob(os.path.join(base_path, name, f'{prefix}*.txt')):
                try:
                    with open(fpath, 'r', encoding='utf-8') as f:
                        rec = json.load(f)
                except Exception:
                    continue  # skip truncated / half-written files
                t = self._iso_to_unix(rec.get('update_time', ''))
                if t is None or not (t0_unix - margin <= t <= t1_unix + margin):
                    continue
                recs.append((t, rec))
        recs.sort(key=lambda r: r[0])
        return recs

    @staticmethod
    def _weather_primary_cause(rec: dict):
        """Return the primary violated weather constraint for an unsafe record, mirroring
        mainWeather._is_safe (< / > directions). Returns '' if nothing (or fields missing)."""
        c = rec.get('constraints') or {}
        violated = []
        try:
            if rec.get('rainrate') is not None and rec['rainrate'] >= c.get('RAINRATE', np.inf):
                violated.append('rain')
            if rec.get('windspeed') is not None and rec['windspeed'] >= c.get('WINDSPEED', np.inf):
                violated.append('wind')
            if rec.get('humidity') is not None and rec['humidity'] >= c.get('HUMIDITY', np.inf):
                violated.append('humidity')
            if rec.get('skybrightness') is not None and rec['skybrightness'] <= c.get('SKYMAG', -np.inf):
                violated.append('cloud')  # low sky brightness = clouds / bright moon
            temp = rec.get('temperature')
            if temp is not None and not (c.get('TEMPERATURE_LOWER', -np.inf) < temp < c.get('TEMPERATURE_UPPER', np.inf)):
                violated.append('temperature')
        except Exception:
            return ''
        for cause in _CAUSE_PRIORITY:
            if cause in violated:
                return cause
        return ''

    @staticmethod
    def _safe_to_float(is_safe):
        """1.0 safe, 0.0 explicitly unsafe, nan unknown (sensor disconnected / None)."""
        if is_safe is True:
            return 1.0
        if is_safe is False:
            return 0.0
        return np.nan

    @staticmethod
    def _nearest_index(times: np.ndarray, grid: np.ndarray, max_dt: float):
        """For each grid time, index of the nearest sample within max_dt, else -1."""
        n = len(times)
        out = np.full(len(grid), -1, dtype=int)
        if n == 0:
            return out
        right = np.searchsorted(times, grid, side='left')
        left = right - 1
        for i, g in enumerate(grid):
            best, best_dt = -1, max_dt
            for j in (left[i], right[i]):
                if 0 <= j < n:
                    dt = abs(times[j] - g)
                    if dt <= best_dt:
                        best, best_dt = j, dt
            out[i] = best
        return out

    # ── observing signal (LIGHT frames) ──────────────────────────────────────

    def _build_light_index(self):
        """Index every LIGHT frame once as sorted (start_unix, exptime_seconds) arrays."""
        if self._light_index is not None:
            return self._light_index
        starts, exps = [], []
        for fpath in glob.glob(os.path.join(self.image_path, '*', '*.fits')):
            m = _IMG_RE.search(os.path.basename(fpath))
            if not m:
                continue
            date_s, time_s, tag, exp_s = m.groups()
            if tag.upper() in CAL_TAGS:
                continue  # calibration frames are not science observing
            t = self._iso_to_unix(f'{date_s[:4]}-{date_s[4:6]}-{date_s[6:]}T'
                                  f'{time_s[:2]}:{time_s[2:4]}:{time_s[4:]}')
            if t is None:
                continue
            try:
                exp = float(exp_s)
            except ValueError:
                continue
            starts.append(t)
            exps.append(exp)
        order = np.argsort(starts) if starts else []
        self._light_index = (np.array(starts)[order] if starts else np.array([]),
                             np.array(exps)[order] if exps else np.array([]))
        return self._light_index

    def _observing_intervals(self, t0_unix: float, t1_unix: float):
        """Merged [start, end] observing intervals within the window (gap-bridged)."""
        starts, exps = self._build_light_index()
        if len(starts) == 0:
            return []
        gap = GAP_BRIDGE_MINUTES * 60.0
        # frames whose [t, t+exp] can touch the window
        lo = np.searchsorted(starts, t0_unix - 3600.0, side='left')
        hi = np.searchsorted(starts, t1_unix, side='right')
        merged = []
        for k in range(lo, hi):
            s, e = starts[k], starts[k] + exps[k]
            if merged and s - merged[-1][1] <= gap:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        # clip to window
        out = []
        for s, e in merged:
            s, e = max(s, t0_unix), min(e, t1_unix)
            if e > s:
                out.append((s, e))
        return out

    # ── per-night classification ─────────────────────────────────────────────

    def analyze_night(self, date_token: str):
        """Classify one night. `date_token` is the YYYYMMDD of the night's evening (sunset date),
        matching the record-directory / image-folder labelling convention.

        NightSession(Time('D T12:00')) returns the night that *ends* on the morning of D
        (evening of D-1 -> morning of D). So to analyze the night whose evening is `date_token`,
        we reference the following day's noon.
        """
        night_date = datetime.datetime.strptime(date_token, '%Y%m%d').date()
        ref_date = night_date + datetime.timedelta(days=1)
        ref = Time(ref_date.strftime('%Y-%m-%dT12:00:00'))
        obsnight = NightSession(ref).obsnight_utc
        t0 = obsnight.sunset_observation.unix
        t1 = obsnight.sunrise_observation.unix
        observable_h = max((t1 - t0) / 3600.0, 0.0)

        row = dict(date=date_token, observable_h=round(observable_h, 3),
                   observing_h=0.0, weather_h=0.0, other_h=0.0, down=False,
                   obs_frac=0.0, weather_frac=0.0, other_frac=0.0,
                   n_weather=0, n_safety=0, n_light=0,
                   label='system_down', dominant_weather_cause='')

        if observable_h <= 0:
            return row  # degenerate window (should not happen at Chile latitudes)

        weather = self._load_status_records(self.weather_path, 'weatherinfo', t0, t1)
        safety = self._load_status_records(self.safety_path, 'safemonitorinfo', t0, t1)
        obs_intervals = self._observing_intervals(t0, t1)
        row['n_weather'], row['n_safety'], row['n_light'] = len(weather), len(safety), len(obs_intervals)

        # No local record whatsoever -> system was down / not attempted.
        if not weather and not safety and not obs_intervals:
            row['down'] = True
            return row

        grid = t0 + GRID_STEP_SECONDS * np.arange(int(np.ceil(observable_h * 3600 / GRID_STEP_SECONDS)))
        n = len(grid)

        # observing mask
        obs_mask = np.zeros(n, dtype=bool)
        for s, e in obs_intervals:
            i0 = max(0, int(np.ceil((s - t0) / GRID_STEP_SECONDS)))
            i1 = min(n, int(np.floor((e - t0) / GRID_STEP_SECONDS)) + 1)
            obs_mask[i0:i1] = True

        # weather / safety safe-state per grid point (nearest sample, carry both sides)
        w_times = np.array([r[0] for r in weather]) if weather else np.array([])
        w_safe = np.array([self._safe_to_float(r[1].get('is_safe')) for r in weather]) if weather else np.array([])
        s_times = np.array([r[0] for r in safety]) if safety else np.array([])
        s_safe = np.array([self._safe_to_float(r[1].get('is_safe')) for r in safety]) if safety else np.array([])

        w_idx = self._nearest_index(w_times, grid, STALE_SAMPLE_FACTOR * self.weather_cadence)
        s_idx = self._nearest_index(s_times, grid, STALE_SAMPLE_FACTOR * self.safety_cadence)

        weather_unsafe = np.array([w_idx[i] >= 0 and w_safe[w_idx[i]] == 0.0 for i in range(n)])
        roof_unsafe = np.array([s_idx[i] >= 0 and s_safe[s_idx[i]] == 0.0 for i in range(n)])
        unsafe = weather_unsafe | roof_unsafe

        # bucketing
        observing = obs_mask
        weather_loss = (~observing) & unsafe
        other_loss = (~observing) & (~unsafe)

        step_h = GRID_STEP_SECONDS / 3600.0
        row['observing_h'] = round(float(observing.sum()) * step_h, 3)
        row['weather_h'] = round(float(weather_loss.sum()) * step_h, 3)
        row['other_h'] = round(float(other_loss.sum()) * step_h, 3)

        total = observable_h if observable_h > 0 else 1.0
        row['obs_frac'] = round(row['observing_h'] / total, 3)
        row['weather_frac'] = round(row['weather_h'] / total, 3)
        row['other_frac'] = round(row['other_h'] / total, 3)

        # dominant weather cause among weather_loss grid points
        causes = Counter()
        for i in np.where(weather_loss)[0]:
            if weather_unsafe[i]:
                cause = self._weather_primary_cause(weather[w_idx[i]][1]) or 'weather'
            else:
                cause = 'roof'
            causes[cause] += 1
        row['dominant_weather_cause'] = causes.most_common(1)[0][0] if causes else ''

        row['label'] = self._night_label(row['obs_frac'], row['weather_h'], row['other_h'])
        return row

    @staticmethod
    def _night_label(obs_frac, weather_h, other_h):
        if obs_frac >= FULL_NIGHT_FRAC:
            return 'full'
        weather_dominates = weather_h >= other_h
        if obs_frac > MIN_OBS_FRAC:
            return 'partial_weather' if weather_dominates else 'partial_other'
        return 'none_weather' if weather_dominates else 'none_other'

    # ── annual aggregation ───────────────────────────────────────────────────

    def _record_dates_in_year(self, year: int):
        """Set of datetime.date whose YYMMDD record dirs (weather/safety/image) fall in `year`."""
        dates = set()
        yy = f'{year % 100:02d}'
        for base in (self.weather_path, self.safety_path):
            for name in glob.glob(os.path.join(base, f'{yy}*')):
                b = os.path.basename(name)
                if len(b) == 6 and b.isdigit():
                    try:
                        dates.add(datetime.date(2000 + int(b[:2]), int(b[2:4]), int(b[4:6])))
                    except ValueError:
                        pass
        # image folders are named YYYY-MM-DD_...
        for name in glob.glob(os.path.join(self.image_path, f'{year}-*')):
            b = os.path.basename(name)
            try:
                dates.add(datetime.date(int(b[:4]), int(b[5:7]), int(b[8:10])))
            except (ValueError, IndexError):
                pass
        return {d for d in dates if d.year == year}

    def analyze_year(self, year: int, start: str = None, end: str = None):
        """Return (ledger rows, summary dict) for every night in the year's span."""
        record_dates = self._record_dates_in_year(year)
        today = datetime.date.today()

        if start:
            start_d = datetime.datetime.strptime(start, '%Y%m%d').date()
        elif record_dates:
            start_d = min(record_dates)
        else:
            start_d = datetime.date(year, 1, 1)

        if end:
            end_d = datetime.datetime.strptime(end, '%Y%m%d').date()
        else:
            # cap at yesterday so tonight's not-yet-observed session isn't counted as "down"
            end_d = min(datetime.date(year, 12, 31), today - datetime.timedelta(days=1))

        rows = []
        d = start_d
        while d <= end_d:
            try:
                rows.append(self.analyze_night(d.strftime('%Y%m%d')))
            except Exception as e:
                print(f'  ! {d.strftime("%Y%m%d")} failed: {e}')
            d += datetime.timedelta(days=1)

        return rows, self._summarize(rows, year)

    @staticmethod
    def _summarize(rows, year):
        s = dict(year=year, n_nights=len(rows))
        for key in ('observable_h', 'observing_h', 'weather_h', 'other_h'):
            s[key] = round(sum(r[key] for r in rows), 2)
        s['down_h'] = round(sum(r['observable_h'] for r in rows if r['down']), 2)
        obs_total = s['observable_h'] or 1.0
        s['efficiency_pct'] = round(100 * s['observing_h'] / obs_total, 1)
        s['weather_pct'] = round(100 * s['weather_h'] / obs_total, 1)
        s['other_pct'] = round(100 * s['other_h'] / obs_total, 1)
        s['down_pct'] = round(100 * s['down_h'] / obs_total, 1)
        s['label_counts'] = dict(Counter(r['label'] for r in rows))
        s['weather_cause_counts'] = dict(Counter(
            r['dominant_weather_cause'] for r in rows if r['dominant_weather_cause']))
        return s

    # ── outputs ──────────────────────────────────────────────────────────────

    def _write_csv(self, rows, path):
        import csv
        cols = ['date', 'observable_h', 'observing_h', 'weather_h', 'other_h', 'down',
                'obs_frac', 'weather_frac', 'other_frac', 'n_weather', 'n_safety', 'n_light',
                'label', 'dominant_weather_cause']
        with open(path, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: r[c] for c in cols})

    def _write_summary(self, summary, path):
        lines = [
            f'RASA36 Operation Statistics — {summary["year"]}',
            '=' * 44,
            f'Nights analyzed      : {summary["n_nights"]}',
            f'Observable dark time : {summary["observable_h"]:.1f} h',
            '',
            f'Observing            : {summary["observing_h"]:.1f} h   ({summary["efficiency_pct"]:.1f}%)',
            f'Weather loss         : {summary["weather_h"]:.1f} h   ({summary["weather_pct"]:.1f}%)',
            f'Other loss           : {summary["other_h"]:.1f} h   ({summary["other_pct"]:.1f}%)',
            f'System down/unknown  : {summary["down_h"]:.1f} h   ({summary["down_pct"]:.1f}%)',
            '',
            'Nights by outcome:',
        ]
        for label in ('full', 'partial_weather', 'partial_other', 'none_weather',
                      'none_other', 'system_down'):
            if label in summary['label_counts']:
                lines.append(f'  {label:16s}: {summary["label_counts"][label]}')
        if summary['weather_cause_counts']:
            lines.append('')
            lines.append('Dominant weather cause (nights):')
            for cause, cnt in sorted(summary['weather_cause_counts'].items(),
                                     key=lambda kv: -kv[1]):
                lines.append(f'  {cause:16s}: {cnt}')
        text = '\n'.join(lines) + '\n'
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return text

    def _make_plots(self, rows, summary, path):
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        # monthly aggregation
        months = list(range(1, 13))
        buckets = {k: np.zeros(12) for k in ('observing_h', 'weather_h', 'other_h', 'down_h')}
        for r in rows:
            m = int(r['date'][4:6]) - 1
            if r['down']:
                buckets['down_h'][m] += r['observable_h']
            else:
                buckets['observing_h'][m] += r['observing_h']
                buckets['weather_h'][m] += r['weather_h']
                buckets['other_h'][m] += r['other_h']

        fig, axes = plt.subplots(1, 3, figsize=(18, 5.5))
        colors = dict(observing_h='#2e7d32', weather_h='#1565c0',
                      other_h='#ef6c00', down_h='#9e9e9e')
        labels = dict(observing_h='Observing', weather_h='Weather',
                      other_h='Other', down_h='Down/unknown')

        # (a) monthly stacked bars
        ax = axes[0]
        bottom = np.zeros(12)
        for k in ('observing_h', 'weather_h', 'other_h', 'down_h'):
            ax.bar(months, buckets[k], bottom=bottom, color=colors[k], label=labels[k])
            bottom += buckets[k]
        ax.set_xticks(months)
        ax.set_xlabel('Month')
        ax.set_ylabel('Hours')
        ax.set_title(f'Monthly time allocation — {summary["year"]}')
        ax.legend(fontsize=8)

        # (b) annual pie
        ax = axes[1]
        vals = [summary['observing_h'], summary['weather_h'], summary['other_h'], summary['down_h']]
        pie_labels = [f'Observing\n{summary["efficiency_pct"]:.0f}%',
                      f'Weather\n{summary["weather_pct"]:.0f}%',
                      f'Other\n{summary["other_pct"]:.0f}%',
                      f'Down\n{summary["down_pct"]:.0f}%']
        pie_colors = [colors['observing_h'], colors['weather_h'], colors['other_h'], colors['down_h']]
        nonzero = [(v, l, c) for v, l, c in zip(vals, pie_labels, pie_colors) if v > 0]
        if nonzero:
            vv, ll, cc = zip(*nonzero)
            ax.pie(vv, labels=ll, colors=cc, startangle=90, wedgeprops=dict(width=0.45))
        ax.set_title('Annual time allocation')

        # (c) night-count by outcome label
        ax = axes[2]
        order = ['full', 'partial_weather', 'partial_other', 'none_weather',
                 'none_other', 'system_down']
        counts = [summary['label_counts'].get(l, 0) for l in order]
        bar_colors = ['#2e7d32', '#1565c0', '#ef6c00', '#1565c0', '#ef6c00', '#9e9e9e']
        ax.bar(range(len(order)), counts, color=bar_colors)
        ax.set_xticks(range(len(order)))
        ax.set_xticklabels(order, rotation=40, ha='right', fontsize=8)
        ax.set_ylabel('Nights')
        ax.set_title('Nights by outcome')

        fig.tight_layout()
        fig.savefig(path, dpi=110, bbox_inches='tight')
        plt.close(fig)

    def run_annual_report(self, year: int, start: str = None, end: str = None,
                          outdir: str = None):
        """Produce the CSV + TXT + PNG annual report and return (rows, summary)."""
        outdir = outdir or self.outdir
        os.makedirs(outdir, exist_ok=True)
        rows, summary = self.analyze_year(year, start=start, end=end)

        csv_path = os.path.join(outdir, f'operation_ledger_{year}.csv')
        txt_path = os.path.join(outdir, f'operation_summary_{year}.txt')
        png_path = os.path.join(outdir, f'operation_stats_{year}.png')

        self._write_csv(rows, csv_path)
        text = self._write_summary(summary, txt_path)
        try:
            self._make_plots(rows, summary, png_path)
        except Exception as e:
            # CSV + TXT are the primary deliverables and are already written; the plot is a
            # convenience. A RecursionError here means the local matplotlib/Python pair can't
            # render (e.g. matplotlib 3.10 on Python 3.14) — the data files are unaffected.
            print(f'  ! plot generation skipped ({type(e).__name__}: {e}); CSV + TXT are complete.')
            png_path = None

        print(text)
        print(f'Wrote: {csv_path}')
        print(f'Wrote: {txt_path}')
        if png_path:
            print(f'Wrote: {png_path}')
        return rows, summary


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='RASA36 telescope operation-status report')
    parser.add_argument('--unitnum', type=int, default=36)
    parser.add_argument('--year', type=int, help='Analyze a full year and write the report')
    parser.add_argument('--date', type=str, help='Analyze a single night YYYYMMDD (print only)')
    parser.add_argument('--start', type=str, help='Year mode: first night YYYYMMDD (inclusive)')
    parser.add_argument('--end', type=str, help='Year mode: last night YYYYMMDD (inclusive)')
    parser.add_argument('--outdir', type=str, help='Override output directory')
    args = parser.parse_args()

    stats = OperationStats(unitnum=args.unitnum)

    if args.date:
        row = stats.analyze_night(args.date)
        print(json.dumps(row, indent=2))
    elif args.year:
        stats.run_annual_report(args.year, start=args.start, end=args.end, outdir=args.outdir)
    else:
        parser.error('Provide either --date YYYYMMDD or --year YYYY')


if __name__ == '__main__':
    main()

# %%
