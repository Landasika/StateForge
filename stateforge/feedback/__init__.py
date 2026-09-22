"""Static-dynamic overlay and structural feedback."""

from stateforge.feedback.frontier import find_frontiers
from stateforge.feedback.distance import distance_to_targets
from stateforge.feedback.engine import FeedbackReport, compute_feedback
from stateforge.feedback.scorer import score_trace

__all__ = ["find_frontiers", "distance_to_targets", "FeedbackReport",
           "compute_feedback", "score_trace"]
