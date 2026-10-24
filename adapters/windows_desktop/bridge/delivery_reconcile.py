"""Classify an already accepted panel message from read-only native evidence.

This module never queues a message, resumes a thread, or reads a transcript.
The caller must bind ``user_message_seen`` to the exact accepted operation
(message digest, target and a native item after acceptance). A final belongs
to that same native turn. A queue match means the exact original message, not
merely another queued message in the target thread.
"""

from __future__ import annotations

from dataclasses import dataclass
import math


ACTIVE = frozenset({"active", "inprogress", "in_progress", "running", "working", "needsattention", "processing"})
IDLE = frozenset({"idle", "notloaded"})
MAX_EVIDENCE_AGE_SECONDS = 15
OVERDUE_SECONDS = 120


@dataclass(frozen=True, slots=True)
class DeliveryEvidence:
    """A single, consistent read-only snapshot for one accepted operation.

    ``queue_count`` is the total native queue length, and ``exact_queue_count``
    counts entries whose target and text match this operation. None means that
    the queue could not be read; it must never be interpreted as an empty queue.
    ``evidence_age_seconds`` is the age of the native status/queue snapshot.
    """

    age_seconds: float
    target_status: str
    queue_count: int | None
    exact_queue_count: int | None
    user_message_seen: bool
    matching_final_seen: bool
    evidence_age_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class DeliveryDecision:
    stage: str
    reason: str
    action: str
    overdue: bool


def _valid_age(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def reconcile_delivery(evidence: DeliveryEvidence) -> DeliveryDecision:
    """Return a truthful state and a *recommendation*, never perform an action.

    ``ready_wake`` permits only a caller-controlled wake of the *existing*
    queued message. The caller must recheck the native state immediately before
    waking. ``uncertain`` is not synonymous with failure: it prohibits both a
    success claim and an automatic second ``queue --message``.
    """

    if not isinstance(evidence, DeliveryEvidence):
        raise TypeError("DeliveryEvidence required")
    valid_age = _valid_age(evidence.age_seconds)
    overdue = valid_age and evidence.age_seconds >= OVERDUE_SECONDS

    def decision(stage: str, reason: str, action: str) -> DeliveryDecision:
        return DeliveryDecision(stage, reason, action, overdue if stage != "completed" else False)

    if not valid_age or type(evidence.user_message_seen) is not bool or type(evidence.matching_final_seen) is not bool:
        return decision("uncertain", "invalid_evidence", "inspect")
    if evidence.matching_final_seen and not evidence.user_message_seen:
        return decision("uncertain", "final_without_exact_user", "inspect")
    if evidence.user_message_seen:
        if evidence.matching_final_seen:
            return decision("completed", "matching_native_final", "none")
        return decision("received", "exact_native_user_message", "observe_final")

    # A stale or unavailable snapshot cannot prove either an empty queue or a
    # safe wake boundary. Exact native user evidence above remains authoritative.
    if not _valid_age(evidence.evidence_age_seconds) or evidence.evidence_age_seconds > MAX_EVIDENCE_AGE_SECONDS:
        return decision("uncertain", "native_snapshot_stale_or_missing", "inspect")
    count, matches = evidence.queue_count, evidence.exact_queue_count
    if (type(count) is not int or type(matches) is not int or count < 0 or matches < 0 or matches > count):
        return decision("uncertain", "queue_evidence_invalid_or_missing", "inspect")
    if matches == 0:
        return decision("uncertain", "exact_message_absent_from_queue_and_turn", "inspect")
    if matches != 1:
        return decision("uncertain", "ambiguous_exact_queue_matches", "inspect")

    status = evidence.target_status.casefold() if isinstance(evidence.target_status, str) else ""
    if status in ACTIVE:
        return decision("queued_busy", "target_turn_in_progress", "wait_current_turn")
    if status in IDLE and count == 1:
        return decision("ready_wake", "single_exact_message_in_idle_queue", "wake_existing_queue")
    if status in IDLE:
        return decision("uncertain", "other_queued_messages_precede_target", "inspect")
    return decision("uncertain", "target_state_unverified", "inspect")
