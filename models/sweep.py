"""
SWEEP MODEL: back-and-forth signature + coverage regularity
==========================================================

A different way to find lawnmower surveys, built for coarse tracks such as
Global Fishing Watch's hourly, ~1 km-snapped positions.

The baseline looks for long, straight lines first (it needs several fixes per
line) and then chains neighbouring lines. On hourly data a 20 km survey line
may be only two or three fixes, and the turns fall between fixes, so straight
lines are hard to see and short random back-and-forth (a patrol, a ship working
a small spot) can still chain into "blocks".

This model works the other way round:

1. HEADING SIGNATURE. In a sliding window (24 h of track, or 25 fixes when there
   are no timestamps) it measures how the ship's movement is spread over
   directions. Two numbers, both 0..1:
     axial strength       R2 = how much of the movement lies along ONE axis
                                (either way along it)
     directional strength R1 = how much of the movement goes ONE way
   Transit: R1 high. Circling or loitering: R2 low. Lawnmower: R2 high and R1
   low, i.e. back and forth along one axis. Windows with that signature make
   up a candidate survey episode.
2. LEGS. Inside an episode, each run of fixes heading the same way along the
   axis is one leg. A leg can be one hourly step long; it needs no straightness
   test, so gently curved lines (following depth contours) still count.
3. COVERAGE REGULARITY. A real survey covers ground systematically: legs of
   similar length, side by side, evenly spaced, stepping across the area. A
   group of legs only becomes a survey block if
     - the spacing between neighbouring legs is in range (not ~0: a ship
       shuttling along the same line is not mapping new ground),
     - the spacing is regular (low variation),
     - the legs overlap and have similar lengths,
     - the legs step across the area mostly one way, or cover it evenly.
     - each turn shifts the ship across by about one line spacing (a zig-zag
       of V shapes, where each leg starts where the last ended, is not a survey;
       on hourly data, where the turn can fall between fixes, legs that share
       the corner fix must instead run within 8 degrees of exactly opposite),
     - the legs do not cross each other,
     - neighbouring legs stay about as far apart at the far end as at the turn
       (no fans),
     - there are at least 5 legs over at least 12 hours.
   Patrols, which go back and forth over the same strip at irregular offsets,
   fail here.

The blocks then go through the same scoring, survey-area grouping and next-line
prediction as the baseline, so the map, tables and downloads are unchanged.

Good at:  hourly / sparse AIS (GFW, satellite AIS with gaps); curved lines;
          rejecting patrols and shuttles; finding sustained survey campaigns.
Weaker:   very short surveys (under ~a day of back-and-forth); single lines and
          cross-lines on their own; star, spiral or radial patterns; surveys run
          by an AUV or towed sled while the ship itself barely moves (no model
          that only reads the ship's track can see those).
"""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pandas as pd

import survey_pattern_detector as spd

NAME = "Sweep: back-and-forth signature"
AUTHOR = "Quinn"
DESCRIPTION = ("Finds windows where the ship goes back and forth along one axis, splits them into "
               "legs at each reversal, and keeps only evenly spaced, side-by-side legs. Built for "
               "hourly Global Fishing Watch tracks; rejects patrols and shuttles.")

# --- tuning (kept here so the shared Settings panel stays the baseline's) ----------
WINDOW_HOURS = 24.0        # signature window (total width), when there are timestamps
WINDOW_FIXES = 25          # ...or this many fixes without timestamps
MIN_AXIAL = 0.55           # R2: at least this much movement along one axis
MAX_DIRECTIONAL = 0.40     # R1: at most this much net movement one way
MIN_LEG_KM = 4.0           # shortest leg kept
LEG_ALONG_FRAC = 0.8       # a step belongs to a leg when this much of it runs along the axis
                           # (lower lets turns leak into the legs)
MAX_TURN_HOURS = 6.0       # a longer gap between one leg and the next starts a new group
MIN_SPACING_KM = 1.0       # closer legs re-run the same ground (and are below GFW's ~1 km grid)
MAX_SPACING_CV = 0.8       # spacing variation allowed (std / mean)
MIN_LENGTH_RATIO = 0.35    # median shorter / longer leg, for neighbours
MIN_OVERLAP = 0.3          # median overlap of neighbouring legs
MIN_PROGRESSION = 0.7      # share of steps across the area that go the same way...
MAX_EVEN_CV = 0.6          # ...or, if not, how evenly the legs cover it
MAX_CROSSING_FRAC = 0.15   # survey legs lie side by side; patrol legs criss-cross
MIN_PARALLEL = 0.33        # the turn between legs shifts the ship by ~one spacing (no V shapes)
MIN_FAN = 0.2              # gap at the far end of two legs vs at the turn (no fans)
MAX_SHARED_TURN_DEG = 8.0  # legs sharing a turn fix (turn fell between hourly fixes) must be
                           # this close to exactly opposite
MIN_LEGS = 5               # at least this many legs (or the Settings value, if higher)
MIN_HOURS = 12.0           # sustained: a sweep lasts at least this long (when there are times)


def detect(vessel, track, cfg):
    # legs can be a single hourly step, so relax the baseline's per-line minimums
    sweep_cfg = replace(cfg, min_points_per_line=2, min_line_km=min(cfg.min_line_km, MIN_LEG_KM))
    return spd.detect_surveys(vessel, track, sweep_cfg,
                              find_lines_fn=find_legs, find_blocks_fn=find_sweeps)


# ============================================================
# 1 + 2: BACK-AND-FORTH EPISODES, SPLIT INTO LEGS
# ============================================================

def _window_sums(z: np.ndarray, centre: np.ndarray, lo: np.ndarray, hi: np.ndarray):
    """Sum of z over steps [lo, hi) for every step, via a cumulative sum."""
    c = np.concatenate([[0], np.cumsum(z)])
    return c[hi] - c[lo]


def signature(x, y, t_hours=None):
    """Per-step axial (R2) and directional (R1) strength over a sliding window."""
    dx, dy = np.diff(x), np.diff(y)
    length = np.hypot(dx, dy)
    theta = np.arctan2(dx, dy)
    n = len(length)
    idx = np.arange(n)
    if t_hours is not None:
        mid = (t_hours[:-1] + t_hours[1:]) / 2
        lo = np.searchsorted(mid, mid - WINDOW_HOURS / 2, side="left")
        hi = np.searchsorted(mid, mid + WINDOW_HOURS / 2, side="right")
    else:
        half = WINDOW_FIXES // 2
        lo, hi = np.clip(idx - half, 0, n), np.clip(idx + half + 1, 0, n)
    total = _window_sums(length, idx, lo, hi)
    z1 = _window_sums(length * np.exp(1j * theta), idx, lo, hi)
    z2 = _window_sums(length * np.exp(2j * theta), idx, lo, hi)
    with np.errstate(invalid="ignore", divide="ignore"):
        r1 = np.where(total > 0, np.abs(z1) / total, 0.0)
        r2 = np.where(total > 0, np.abs(z2) / total, 0.0)
    return r1, r2, total, length, theta


def _runs(mask: np.ndarray, max_hole: int = 0):
    """(start, end) index pairs of True runs, bridging holes up to max_hole long."""
    out, start, hole = [], None, 0
    for i, m in enumerate(mask):
        if m:
            if start is None:
                start = i
            hole, last = 0, i
        elif start is not None:
            hole += 1
            if hole > max_hole:
                out.append((start, last))
                start, hole = None, 0
    if start is not None:
        out.append((start, last))
    return out


def find_legs(chunk, cfg):
    """Legs (start, end positions within the chunk) of every back-and-forth episode."""
    lat, lon = chunk["lat"].to_numpy(), chunk["lon"].to_numpy()
    if len(lat) < 4:
        return []
    x, y = spd.to_local_xy(lat, lon, float(np.median(lat)), float(np.median(lon)))
    times = chunk["time"]
    t_hours = None
    if times.notna().all():
        t_hours = (times - times.iloc[0]).dt.total_seconds().to_numpy() / 3600
    r1, r2, total, length, theta = signature(x, y, t_hours)

    sweeping = (r2 >= MIN_AXIAL) & (r1 <= MAX_DIRECTIONAL) & (total >= 3 * MIN_LEG_KM)
    legs = []
    for s0, s1 in _runs(sweeping, max_hole=2):
        steps = np.arange(s0, s1 + 1)
        w = length[steps]
        if w.sum() < 3 * MIN_LEG_KM:
            continue
        axis = math.atan2((w * np.sin(2 * theta[steps])).sum(), (w * np.cos(2 * theta[steps])).sum()) / 2
        u = np.array([math.sin(axis), math.cos(axis)])
        along = np.diff(x)[steps] * u[0] + np.diff(y)[steps] * u[1]
        sign = np.where(along > LEG_ALONG_FRAC * w, 1, np.where(along < -LEG_ALONG_FRAC * w, -1, 0))

        i = 0
        while i < len(steps):
            if sign[i] == 0:
                i += 1
                continue
            j = i
            while j + 1 < len(steps) and sign[j + 1] == sign[i]:
                j += 1
            if abs(along[i:j + 1].sum()) >= MIN_LEG_KM:
                legs.append((int(steps[i]), int(steps[j]) + 1))   # step k joins points k and k+1
            i = j + 1
    return legs


# ============================================================
# 3: KEEP ONLY REGULAR, SIDE-BY-SIDE SWEEPS
# ============================================================

def find_sweeps(lines, cfg):
    """Group consecutive legs, split where they stop alternating, keep regular groups."""
    groups, cur = [], []
    for k, line in enumerate(lines):
        if cur:
            prev = lines[cur[-1]]
            turn_km = spd.haversine_km(prev["end_lat"], prev["end_lon"], line["start_lat"], line["start_lon"])
            turn_h = (line["start_time"] - prev["end_time"]).total_seconds() / 3600 \
                if not (pd.isna(line["start_time"]) or pd.isna(prev["end_time"])) else 0.0
            joined = (line["chunk"] == prev["chunk"]
                      and turn_km <= cfg.max_connector_km and turn_h <= MAX_TURN_HOURS
                      and spd.angle_diff(line["heading_deg"], prev["heading_deg"])
                      >= 180 - 1.5 * cfg.antiparallel_tol_deg)
            if not joined:
                groups.append(cur)
                cur = []
        cur.append(k)
    if cur:
        groups.append(cur)

    blocks = []
    for g in groups:
        blocks += _regular_runs(g, lines, cfg)
    return blocks, set()


def _cross_positions(idx, lines):
    """Where each leg sits across the group's axis, plus neighbour overlap and length ratio."""
    L = [lines[i] for i in idx]
    lat0 = float(np.mean([l[k] for l in L for k in ("start_lat", "end_lat")]))
    lon0 = float(np.mean([l[k] for l in L for k in ("start_lon", "end_lon")]))
    axis = spd.axial_mean([l["axis_deg"] for l in L], [l["length_km"] for l in L])
    u = np.array([math.sin(math.radians(axis)), math.cos(math.radians(axis))])
    n = np.array([u[1], -u[0]])
    cross, spans, segs = [], [], []
    for l in L:
        x, y = spd.to_local_xy([l["start_lat"], l["end_lat"]], [l["start_lon"], l["end_lon"]], lat0, lon0)
        segs.append(((x[0], y[0]), (x[1], y[1])))
        cross.append(float(np.mean(x) * n[0] + np.mean(y) * n[1]))
        spans.append(sorted([x[0] * u[0] + y[0] * u[1], x[1] * u[0] + y[1] * u[1]]))
    # in a lawnmower the turn moves the ship across by about one line spacing; in a
    # zig-zag of V shapes the next leg starts where the last one ended
    # With hourly fixes the turn often falls between two fixes, so both legs share the
    # fix at the corner and any lawnmower looks like a narrow V; then the legs are
    # judged by how close to exactly opposite they run instead.
    shared = [b["i0"] <= a["i1"] for a, b in zip(L[:-1], L[1:])]
    opposite = [spd.angle_diff(a["heading_deg"], b["heading_deg"]) >= 180 - MAX_SHARED_TURN_DEG
                for a, b in zip(L[:-1], L[1:])]
    parallel = []
    for (p0, p1), (q0, q1), c0, c1, sh, op in zip(segs[:-1], segs[1:], cross[:-1], cross[1:],
                                                  shared, opposite):
        if sh:
            parallel.append(1.0 if op else 0.0)
            continue
        turn = float(np.hypot(q0[0] - p1[0], q0[1] - p1[1]))
        side = abs(c1 - c0)
        parallel.append(min(turn, side) / max(turn, side) if max(turn, side) > 0 else 0.0)
    fan = []   # across-axis gap at the turn end vs the far end: parallel legs keep it
    for (p0, p1), (q0, q1), sh, op in zip(segs[:-1], segs[1:], shared, opposite):
        if sh:
            fan.append(1.0 if op else 0.0)
            continue
        near = abs((q0[0] - p1[0]) * n[0] + (q0[1] - p1[1]) * n[1])
        far = abs((q1[0] - p0[0]) * n[0] + (q1[1] - p0[1]) * n[1])
        fan.append(min(near, far) / max(near, far) if max(near, far) > 0 else 0.0)
    overlap, ratio = [], []
    for (a0, a1), (b0, b1) in zip(spans[:-1], spans[1:]):
        shorter = min(a1 - a0, b1 - b0)
        overlap.append(max(0.0, min(a1, b1) - max(a0, b0)) / shorter if shorter > 0 else 0.0)
        ratio.append(shorter / max(a1 - a0, b1 - b0) if max(a1 - a0, b1 - b0) > 0 else 0.0)
    return np.array(cross), np.array(overlap), np.array(ratio), segs, np.array(parallel), np.array(fan)


def _crosses(s, t) -> bool:
    """Do two segments properly intersect (touching at an end doesn't count)?"""
    def side(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    d1, d2 = side(*s, t[0]), side(*s, t[1])
    d3, d4 = side(*t, s[0]), side(*t, s[1])
    return d1 * d2 < 0 and d3 * d4 < 0


def crossing_frac(segs) -> float:
    """Crossing leg pairs per leg."""
    n = len(segs)
    hits = sum(_crosses(segs[i], segs[j]) for i in range(n) for j in range(i + 1, n))
    return hits / n


def is_regular(idx, lines, cfg) -> bool:
    if len(idx) < max(cfg.min_lines_per_block, MIN_LEGS):
        return False
    cross, overlap, ratio, segs, parallel, fan = _cross_positions(idx, lines)
    if np.median(parallel) < MIN_PARALLEL or np.median(fan) < MIN_FAN:
        return False
    steps = np.diff(cross)
    spacing = np.abs(steps)
    if np.median(spacing) < MIN_SPACING_KM or np.median(spacing) > cfg.max_spacing_km:
        return False
    if spacing.std() / spacing.mean() > MAX_SPACING_CV:
        return False
    if np.median(overlap) < MIN_OVERLAP or np.median(ratio) < MIN_LENGTH_RATIO:
        return False
    if crossing_frac(segs) > MAX_CROSSING_FRAC:
        return False
    overall = np.sign(cross[-1] - cross[0]) or 1.0
    if (np.sign(steps) == overall).mean() >= MIN_PROGRESSION:
        return True
    gaps = np.diff(np.sort(cross))            # interleaved: are the legs evenly spread?
    gaps = gaps[gaps > MIN_SPACING_KM / 2]
    return len(gaps) >= 2 and gaps.std() / gaps.mean() <= MAX_EVEN_CV


def _hours(idx, lines) -> float:
    """How long a run of legs lasts; infinite without timestamps (the test is skipped)."""
    t0, t1 = lines[idx[0]]["start_time"], lines[idx[-1]]["end_time"]
    if pd.isna(t0) or pd.isna(t1):
        return math.inf
    return (t1 - t0).total_seconds() / 3600


def _regular_runs(group, lines, cfg):
    """Longest regular runs within a group: grow greedily, restart where a leg breaks it."""
    out, start = [], 0
    need = max(cfg.min_lines_per_block, MIN_LEGS)
    while start <= len(group) - need:
        end = start + need
        if not is_regular(group[start:end], lines, cfg):
            start += 1
            continue
        while end < len(group) and is_regular(group[start:end + 1], lines, cfg):
            end += 1
        if _hours(group[start:end], lines) >= MIN_HOURS:
            out.append(group[start:end])
        start = end
    return out
