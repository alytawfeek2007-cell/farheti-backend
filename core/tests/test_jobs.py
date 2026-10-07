import io
from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone

from core import jobs, payments
from core.models import MIN_DAYS_AHEAD_TO_ACCEPT, Booking, RefundAttempt

from .helpers import (
    B, K, S, FarhetiTestCase, make_booking, make_paid_booking, make_refund_attempt, refund_ok,
)


class ExpireUnpaidBookingsTests(FarhetiTestCase):
    def accepted(self, minutes_left):
        booking = make_booking(status=B.ACCEPTED)
        Booking.objects.filter(pk=booking.pk).update(
            payment_deadline=timezone.now() + timedelta(minutes=minutes_left)
        )
        return booking

    def status(self, booking):
        booking.refresh_from_db()
        return booking.status

    def test_only_accepted_bookings_past_their_deadline_expire(self):
        late = self.accepted(-5)
        on_time = self.accepted(+5)
        pending = make_booking()
        paid, _ = make_paid_booking()
        result = jobs.expire_unpaid_bookings()
        self.assertEqual((result['candidates'], result['done'], result['skipped'], result['failed']), (1, 1, 0, 0))
        self.assertEqual(self.status(late), B.EXPIRED)
        self.assertEqual(self.status(on_time), B.ACCEPTED)
        self.assertEqual(self.status(pending), B.PENDING)
        self.assertEqual(self.status(paid), B.PAID)

    def test_running_twice_is_harmless(self):
        self.accepted(-5)
        jobs.expire_unpaid_bookings()
        again = jobs.expire_unpaid_bookings()
        self.assertEqual((again['candidates'], again['done']), (0, 0))

    def test_dry_run_changes_nothing(self):
        late = self.accepted(-5)
        result = jobs.expire_unpaid_bookings(dry_run=True)
        self.assertEqual((result['candidates'], result['done']), (1, 0))
        self.assertEqual(result['ids'], [late.pk])
        self.assertEqual(self.status(late), B.ACCEPTED)

    def test_batch_limit_takes_the_oldest_deadlines_first(self):
        oldest, middle, newest = self.accepted(-30), self.accepted(-20), self.accepted(-10)
        first = jobs.expire_unpaid_bookings(limit=2)
        self.assertEqual(first['ids'], [oldest.pk, middle.pk])
        self.assertEqual(self.status(newest), B.ACCEPTED)
        second = jobs.expire_unpaid_bookings()
        self.assertEqual(second['ids'], [newest.pk])

    def test_accepted_booking_with_a_null_deadline_never_crashes_the_job(self):
        no_deadline = make_booking(status=B.ACCEPTED, deadline_in=None)
        late = self.accepted(-5)
        result = jobs.expire_unpaid_bookings()
        self.assertEqual((result['done'], result['failed']), (1, 0))
        self.assertEqual(self.status(no_deadline), B.ACCEPTED)
        self.assertEqual(self.status(late), B.EXPIRED)

    def test_a_booking_that_changed_state_meanwhile_is_skipped_not_failed(self):
        self.accepted(-5)
        with mock.patch.object(Booking, 'mark_expired', side_effect=ValidationError('paid meanwhile')):
            result = jobs.expire_unpaid_bookings()
        self.assertEqual((result['done'], result['skipped'], result['failed']), (0, 1, 0))

    def test_one_failure_never_blocks_the_rest(self):
        self.accepted(-30)
        self.accepted(-20)
        with mock.patch.object(Booking, 'mark_expired', side_effect=[RuntimeError('boom'), None]):
            with self.assertLogs('farheti.jobs', 'ERROR'):
                result = jobs.expire_unpaid_bookings()
        self.assertEqual((result['done'], result['failed']), (1, 1))


class ExpireStalePendingTests(FarhetiTestCase):
    def status(self, booking):
        booking.refresh_from_db()
        return booking.status

    def test_unanswered_requests_expire_after_72_hours(self):
        old = make_booking()
        recent = make_booking()
        Booking.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=73))
        Booking.objects.filter(pk=recent.pk).update(created_at=timezone.now() - timedelta(hours=71))
        result = jobs.expire_stale_pending_bookings()
        self.assertEqual(result['done'], 1)
        self.assertEqual(self.status(old), B.EXPIRED)
        self.assertEqual(self.status(recent), B.PENDING)

    def test_requests_whose_event_is_too_close_to_ever_be_paid_expire(self):
        for days, expected in ((0, B.EXPIRED), (1, B.EXPIRED),
                               (MIN_DAYS_AHEAD_TO_ACCEPT, B.PENDING), (30, B.PENDING)):
            with self.subTest(days_ahead=days):
                booking = make_booking(days_ahead=days)
                jobs.expire_stale_pending_bookings()
                self.assertEqual(self.status(booking), expected)

    def test_other_statuses_are_never_touched(self):
        accepted = make_booking(status=B.ACCEPTED, days_ahead=1)
        Booking.objects.filter(pk=accepted.pk).update(created_at=timezone.now() - timedelta(days=10))
        paid, _ = make_paid_booking(days_ahead=1)
        jobs.expire_stale_pending_bookings()
        self.assertEqual(self.status(accepted), B.ACCEPTED)
        self.assertEqual(self.status(paid), B.PAID)

    def test_dry_run_and_limit(self):
        stale = [make_booking(days_ahead=0) for _ in range(3)]
        dry = jobs.expire_stale_pending_bookings(dry_run=True)
        self.assertEqual((dry['candidates'], dry['done']), (3, 0))
        self.assertTrue(all(self.status(b) == B.PENDING for b in stale))
        first = jobs.expire_stale_pending_bookings(limit=2)
        self.assertEqual((first['candidates'], first['done']), (2, 2))
        self.assertEqual(jobs.expire_stale_pending_bookings()['done'], 1)


class ProcessRefundsTests(FarhetiTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch('core.paymob.refund_transaction', return_value=refund_ok('9001'))
        self.gateway = patcher.start()
        self.addCleanup(patcher.stop)
        self.n = 0

    def paid(self):
        self.n += 1
        return make_paid_booking(txn_id=f'60{self.n}')

    def attempt(self, status, **kwargs):
        booking, payment = self.paid()
        return booking, payment, make_refund_attempt(payment, status, **kwargs)

    # ---- parking
    def test_abandoned_in_flight_attempts_are_parked_as_unknown(self):
        _, _, stale = self.attempt(S.IN_FLIGHT)
        _, _, fresh = self.attempt(S.IN_FLIGHT, started_ago=timedelta(seconds=3))
        with self.assertLogs('farheti.payments', 'ERROR'):
            result = jobs.process_refunds()
        self.assertEqual(result['parked'], 1)
        self.assertEqual(RefundAttempt.objects.get(pk=stale.pk).status, S.UNKNOWN)
        self.assertEqual(RefundAttempt.objects.get(pk=fresh.pk).status, S.IN_FLIGHT)
        self.gateway.assert_not_called()

    # ---- resuming
    def test_gateway_refunded_attempts_are_finished_without_calling_paymob(self):
        booking, payment, attempt = self.attempt(S.GATEWAY_REFUNDED, refund_reference='9001', attempts=1)
        result = jobs.process_refunds()
        self.assertEqual((result['finished'], result['failed'], result['skipped']), (1, 0, 0))
        self.assertEqual(RefundAttempt.objects.get(pk=attempt.pk).status, S.DONE)
        booking.refresh_from_db()
        self.assertEqual(booking.status, B.CANCELLED)
        self.assertEqual(result['stuck_total'], 0)
        self.gateway.assert_not_called()

    def test_attempts_over_the_auto_resume_cap_are_left_for_the_owner(self):
        _, _, attempt = self.attempt(
            S.GATEWAY_REFUNDED, refund_reference='9001', attempts=jobs.AUTO_RESUME_MAX_ATTEMPTS
        )
        with self.assertLogs('farheti.payments', 'CRITICAL'):
            result = jobs.process_refunds()
        self.assertEqual(result['finished'], 0)
        self.assertEqual(RefundAttempt.objects.get(pk=attempt.pk).status, S.GATEWAY_REFUNDED)
        self.assertEqual(result['stuck_total'], 1)

    def test_reconcile_is_counted_as_failed_and_not_retried_automatically(self):
        booking, _, attempt = self.attempt(S.GATEWAY_REFUNDED, refund_reference='9001', attempts=1)
        Booking.objects.filter(pk=booking.pk).update(status=B.COMPLETED)
        with self.assertLogs('farheti.payments', 'ERROR'):
            result = jobs.process_refunds()
        self.assertEqual((result['finished'], result['skipped'], result['failed']), (0, 0, 1))
        self.assertTrue(RefundAttempt.objects.get(pk=attempt.pk).last_error.startswith('RECONCILE'))
        again = jobs.process_refunds()  # excluded from auto-resume now
        self.assertEqual(again['failed'], 0)

    def test_overlap_with_another_worker_is_counted_as_skipped(self):
        _, _, attempt = self.attempt(S.GATEWAY_REFUNDED, refund_reference='9001', attempts=1)

        def other_worker_got_there_first(*, attempt_id, actor=None):
            RefundAttempt.objects.filter(pk=attempt_id).update(started_at=timezone.now())
            raise payments.FlowConflict('This refund is currently being processed.')

        with mock.patch('core.payments.retry_refund_attempt', side_effect=other_worker_got_there_first):
            result = jobs.process_refunds()
        self.assertEqual((result['finished'], result['skipped'], result['failed']), (0, 1, 0))

    def test_an_unexpected_error_is_counted_as_failed(self):
        self.attempt(S.GATEWAY_REFUNDED, refund_reference='9001', attempts=1)
        with mock.patch('core.payments.retry_refund_attempt', side_effect=RuntimeError('boom')):
            with self.assertLogs('farheti.payments', 'ERROR'):
                result = jobs.process_refunds()
        self.assertEqual((result['skipped'], result['failed']), (0, 1))

    # ---- stuck report
    def listed(self, result, attempt):
        return any(f'attempt #{attempt.pk} ' in line for line in result['stuck'])

    def test_stuck_report_selection(self):
        _, _, unknown = self.attempt(S.UNKNOWN)
        _, _, failed = self.attempt(S.FAILED)
        _, _, no_claim = self.attempt(S.IN_FLIGHT, started_ago=None)
        _, _, stale_gw = self.attempt(S.GATEWAY_REFUNDED, attempts=jobs.AUTO_RESUME_MAX_ATTEMPTS)
        _, _, done = self.attempt(S.DONE)
        _, _, fresh_gw = self.attempt(S.GATEWAY_REFUNDED, started_ago=timedelta(seconds=3))
        _, _, cleared = self.attempt(
            S.FAILED, last_error=f'{payments.CLEARED_NOT_REFUNDED_PREFIX}: verified not refunded at Paymob'
        )
        result = jobs.process_refunds(dry_run=True)
        for attempt in (unknown, failed, no_claim, stale_gw):
            self.assertTrue(self.listed(result, attempt), attempt.status)
        for attempt in (done, fresh_gw, cleared):
            self.assertFalse(self.listed(result, attempt), attempt.status)

    def test_owner_cleared_failed_attempt_is_reported_again_after_24_hours(self):
        _, _, cleared = self.attempt(
            S.FAILED, last_error=f'{payments.CLEARED_NOT_REFUNDED_PREFIX}: verified not refunded at Paymob'
        )
        self.assertFalse(self.listed(jobs.process_refunds(dry_run=True), cleared))
        RefundAttempt.objects.filter(pk=cleared.pk).update(
            updated_at=timezone.now() - timedelta(hours=jobs.CLEARED_REPORT_AFTER_HOURS + 1)
        )
        self.assertTrue(self.listed(jobs.process_refunds(dry_run=True), cleared))

    # ---- alerting
    def test_each_stuck_refund_is_alerted_once_per_window(self):
        _, _, attempt = self.attempt(S.UNKNOWN)
        with self.assertLogs('farheti.payments', 'CRITICAL') as logs:
            first = jobs.process_refunds()
        self.assertEqual((first['stuck_total'], first['alerted_total']), (1, 1))
        self.assertIn('STUCK REFUND NEEDS THE OWNER', logs.output[0])
        second = jobs.process_refunds()
        self.assertEqual((second['stuck_total'], second['alerted_total']), (1, 0))

    def test_alert_repeats_after_the_window_or_a_status_change(self):
        _, _, attempt = self.attempt(S.UNKNOWN)
        with self.assertLogs('farheti.payments', 'CRITICAL'):
            jobs.process_refunds()
        RefundAttempt.objects.filter(pk=attempt.pk).update(status=S.FAILED)
        with self.assertLogs('farheti.payments', 'CRITICAL'):
            changed = jobs.process_refunds()
        self.assertEqual(changed['alerted_total'], 1)
        from django.core.cache import cache
        cache.clear()  # the cache entry "expires"
        with self.assertLogs('farheti.payments', 'CRITICAL'):
            self.assertEqual(jobs.process_refunds()['alerted_total'], 1)

    def test_a_broken_cache_never_hides_a_money_problem(self):
        self.attempt(S.UNKNOWN)
        with mock.patch('core.jobs.cache') as broken:
            broken.add.side_effect = RuntimeError('cache down')
            with self.assertLogs('farheti.payments', 'CRITICAL'):
                result = jobs.process_refunds()
        self.assertEqual(result['alerted_total'], 1)

    def test_printed_lines_are_capped_but_alerts_are_not(self):
        for _ in range(3):
            self.attempt(S.UNKNOWN)
        with mock.patch.object(jobs, 'MAX_STUCK_LINES', 2):
            with self.assertLogs('farheti.payments', 'CRITICAL'):
                result = jobs.process_refunds()
        self.assertEqual(result['stuck_total'], 3)
        self.assertEqual(len(result['stuck']), 2)
        self.assertEqual(result['alerted_total'], 3)
        self.assertEqual(len(result['alerted']), 2)

    # ---- dry run
    def test_dry_run_changes_nothing_alerts_nothing_and_labels_what_would_happen(self):
        _, _, stale = self.attempt(S.IN_FLIGHT)
        _, _, resumable = self.attempt(S.GATEWAY_REFUNDED, refund_reference='9001', attempts=1)
        _, _, capped = self.attempt(S.GATEWAY_REFUNDED, attempts=jobs.AUTO_RESUME_MAX_ATTEMPTS)
        with self.assertNoLogs('farheti.payments', level='CRITICAL'):
            result = jobs.process_refunds(dry_run=True)
        self.assertEqual((result['parked'], result['finished'], result['alerted_total']), (0, 0, 0))
        self.assertEqual(RefundAttempt.objects.get(pk=stale.pk).status, S.IN_FLIGHT)
        self.assertEqual(RefundAttempt.objects.get(pk=resumable.pk).status, S.GATEWAY_REFUNDED)
        by_id = {int(line.split('attempt #')[1].split(' ')[0]): line for line in result['stuck']}
        self.assertTrue(by_id[stale.pk].startswith('WOULD PARK AS UNKNOWN: '))
        self.assertTrue(by_id[resumable.pk].startswith('WOULD RESUME: '))
        self.assertFalse(by_id[capped.pk].startswith('WOULD'))
        self.gateway.assert_not_called()


class CommandSmokeTests(FarhetiTestCase):
    def run_command(self, name, *args):
        out = io.StringIO()
        call_command(name, *args, stdout=out, stderr=io.StringIO())
        return out.getvalue()

    def test_expire_bookings_command(self):
        booking = make_booking(status=B.ACCEPTED)
        Booking.objects.filter(pk=booking.pk).update(payment_deadline=timezone.now() - timedelta(minutes=1))
        self.run_command('expire_bookings', '--dry-run')
        booking.refresh_from_db()
        self.assertEqual(booking.status, B.ACCEPTED)
        self.run_command('expire_bookings')
        booking.refresh_from_db()
        self.assertEqual(booking.status, B.EXPIRED)

    def test_check_refunds_command(self):
        _, payment = make_paid_booking()
        attempt = make_refund_attempt(payment, S.IN_FLIGHT, kind=K.CANCELLATION)
        self.run_command('check_refunds', '--dry-run')
        self.assertEqual(RefundAttempt.objects.get(pk=attempt.pk).status, S.IN_FLIGHT)
        with self.assertLogs('farheti.payments', 'ERROR'):
            self.run_command('check_refunds')
        self.assertEqual(RefundAttempt.objects.get(pk=attempt.pk).status, S.UNKNOWN)
