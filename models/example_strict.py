"""
EXAMPLE MODEL: the baseline with stricter rules.

Here to show the switcher working with two models, and as the simplest way to
make your own: change the settings, keep the pipeline. Delete or rename it
once the team has real models.
"""

from dataclasses import replace

import survey_pattern_detector as spd

NAME = "Example: stricter rules"
AUTHOR = "Example"
DESCRIPTION = ("The baseline, but a block needs 4+ lines that are closer to exactly opposite. "
               "Fewer false alarms, more missed surveys.")


def detect(vessel, track, cfg):
    strict = replace(cfg,
                     min_lines_per_block=max(cfg.min_lines_per_block, 4),
                     antiparallel_tol_deg=min(cfg.antiparallel_tol_deg, 10.0),
                     min_overlap_frac=max(cfg.min_overlap_frac, 0.5))
    return spd.detect_surveys(vessel, track, strict)
