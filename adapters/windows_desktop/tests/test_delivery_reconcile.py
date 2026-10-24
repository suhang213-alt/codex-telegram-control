"""Regression tests for panel receipt versus actual native acceptance."""

import unittest

from delivery_reconcile import DeliveryEvidence, reconcile_delivery


def evidence(**changes):
    base = dict(age_seconds=150, target_status="active", queue_count=1,
                exact_queue_count=1, user_message_seen=False,
                matching_final_seen=False, evidence_age_seconds=0)
    base.update(changes)
    return DeliveryEvidence(**base)


class DeliveryReconcileTests(unittest.TestCase):
    def test_busy_target_keeps_exact_message_queued_and_surfaces_delay(self):
        decision = reconcile_delivery(evidence())
        self.assertEqual((decision.stage, decision.action, decision.overdue),
                         ("queued_busy", "wait_current_turn", True))

    def test_idle_target_may_wake_only_single_exact_existing_queue_item(self):
        decision = reconcile_delivery(evidence(target_status="notLoaded", age_seconds=10))
        self.assertEqual((decision.stage, decision.action, decision.overdue),
                         ("ready_wake", "wake_existing_queue", False))

    def test_vanished_queue_without_native_user_is_uncertain_not_delivered(self):
        decision = reconcile_delivery(evidence(target_status="idle", queue_count=0,
                                               exact_queue_count=0))
        self.assertEqual((decision.stage, decision.reason, decision.action),
                         ("uncertain", "exact_message_absent_from_queue_and_turn", "inspect"))

    def test_disappeared_queue_during_active_turn_is_not_claimed_as_received(self):
        decision = reconcile_delivery(evidence(queue_count=0, exact_queue_count=0))
        self.assertEqual(decision.stage, "uncertain")

    def test_native_user_then_matching_final_advance_two_distinct_stages(self):
        received = reconcile_delivery(evidence(queue_count=0, exact_queue_count=0,
                                               user_message_seen=True))
        completed = reconcile_delivery(evidence(queue_count=0, exact_queue_count=0,
                                                user_message_seen=True,
                                                matching_final_seen=True))
        self.assertEqual((received.stage, received.action), ("received", "observe_final"))
        self.assertEqual((completed.stage, completed.action, completed.overdue),
                         ("completed", "none", False))

    def test_unverified_final_cannot_claim_completion(self):
        decision = reconcile_delivery(evidence(matching_final_seen=True))
        self.assertEqual(decision.stage, "uncertain")

    def test_missing_or_stale_native_snapshot_cannot_authorize_wake(self):
        for snapshot_age in (None, 16):
            with self.subTest(snapshot_age=snapshot_age):
                decision = reconcile_delivery(evidence(target_status="idle",
                                                       evidence_age_seconds=snapshot_age))
                self.assertEqual((decision.stage, decision.action), ("uncertain", "inspect"))

    def test_other_messages_or_duplicate_exact_matches_cannot_authorize_wake(self):
        for queue_count, exact_count in ((2, 1), (2, 2)):
            with self.subTest(queue_count=queue_count, exact_count=exact_count):
                decision = reconcile_delivery(evidence(target_status="idle",
                                                       queue_count=queue_count,
                                                       exact_queue_count=exact_count))
                self.assertEqual(decision.stage, "uncertain")

    def test_queue_probe_failure_does_not_mean_empty_queue(self):
        decision = reconcile_delivery(evidence(target_status="idle", queue_count=None,
                                               exact_queue_count=None))
        self.assertEqual(decision.reason, "queue_evidence_invalid_or_missing")


if __name__ == "__main__":
    unittest.main()
