"""Static-dynamic overlay and structural feedback."""

from stateforge.fuzzing.feedback.frontier import find_frontiers
from stateforge.fuzzing.feedback.distance import distance_to_targets
from stateforge.fuzzing.feedback.engine import FeedbackReport, compute_feedback
from stateforge.fuzzing.feedback.scorer import score_trace

__all__ = ["find_frontiers", "distance_to_targets", "FeedbackReport",
           "compute_feedback", "score_trace"]
