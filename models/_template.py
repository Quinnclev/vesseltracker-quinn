"""
TEMPLATE: copy this file to start your own model
================================================

1. Copy it to models/<yourname>.py (no leading underscore, or it is ignored).
2. Change NAME, AUTHOR and DESCRIPTION.
3. Pick ONE of the options in detect() below and write your idea.
4. Run the app (streamlit run app.py): your model appears in the sidebar's
   "Model" dropdown, and in "Compare models" next to everyone else's.
   Or from the command line:
       python survey_pattern_detector.py benchmarks/synthetic_10_ships.csv --model <yourname>

Rules: detect() must return the same dict as spd.detect_surveys(). Options A and
B do that for you; only option C needs you to build it yourself.
"""

from dataclasses import replace

import survey_pattern_detector as spd

NAME = "My model"
AUTHOR = "Your name"
DESCRIPTION = "One or two sentences on what your model does differently."


def detect(vessel, track, cfg):
    # --- OPTION A: same pipeline, different settings ----------------------
    # my_cfg = replace(cfg, min_line_km=5.0, max_spacing_km=15.0)
    # return spd.detect_surveys(vessel, track, my_cfg)

    # --- OPTION B: your own pattern recogniser ----------------------------
    # The baseline still cleans the track and finds the straight lines; you
    # decide which lines form a lawnmower block. Edit find_my_blocks() below.
    return spd.detect_surveys(vessel, track, cfg, find_blocks_fn=find_my_blocks)

    # --- OPTION C: completely your own approach ---------------------------
    # (machine learning, Hough transform, image-based...). Read
    # spd.detect_surveys() first: you must return every key it returns.


def find_my_blocks(lines, cfg):
    """
    lines: the straight lines in time order, a list of dicts with keys such as
        heading_deg, axis_deg (0-180), length_km, start_lat, start_lon,
        end_lat, end_lon, median_speed_kn, start_time, end_time.
    Return (blocks, skipped):
        blocks  = list of blocks, each a list of indices into `lines`
        skipped = set of line indices inside a block that are not survey lines

    This simple example chains consecutive lines that each run roughly
    opposite to the previous one. Replace it with your idea.
    """
    blocks, current = [], [0] if lines else []
    for k in range(1, len(lines)):
        turn = spd.angle_diff(lines[k]["heading_deg"], lines[current[-1]]["heading_deg"])
        if turn >= 180 - cfg.antiparallel_tol_deg:
            current.append(k)
        else:
            if len(current) >= cfg.min_lines_per_block:
                blocks.append(current)
            current = [k]
    if len(current) >= cfg.min_lines_per_block:
        blocks.append(current)
    return blocks, set()
