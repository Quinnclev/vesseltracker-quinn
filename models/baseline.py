"""The team's original rule-based detector, unchanged. Every other model is compared to this."""

import survey_pattern_detector as spd

NAME = "Baseline: lawnmower rules"
AUTHOR = "Original team detector"
DESCRIPTION = ("Splits the track into straight lines, then chains lines that run the opposite way, "
               "evenly spaced and overlapping, into survey blocks. Every threshold is in Settings.")


def detect(vessel, track, cfg):
    return spd.detect_surveys(vessel, track, cfg)
