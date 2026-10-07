import logging

from django.core.management.base import BaseCommand, CommandError

from core import jobs


class Command(BaseCommand):
    help = 'Expire unpaid ACCEPTED bookings past their deadline and stale PENDING requests.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Only list what would expire.')

    def handle(self, *args, **options):
        dry = options['dry_run']
        passes = (
            ('unpaid accepted', jobs.expire_unpaid_bookings),
            ('unanswered pending', jobs.expire_stale_pending_bookings),
        )
        crashed, failed = [], False
        for name, job in passes:
            try:
                res = job(dry_run=dry)
            except Exception:
                logging.getLogger('farheti.jobs').exception('expire_bookings: pass %r crashed', name)
                crashed.append(name)
                continue  # the other pass still runs
            if dry:
                self.stdout.write(f"{name}: {res['candidates']} would expire")
                if res['ids']:
                    self.stdout.write(f"  booking ids: {res['ids']}")
            elif res['candidates']:  # real runs stay silent when there is nothing to do
                self.stdout.write(
                    f"{name}: {res['candidates']} found, {res['done']} expired, "
                    f"{res['skipped']} skipped, {res['failed']} failed"
                )
            if res['failed']:
                failed = True
            if res['candidates'] >= jobs.BATCH_LIMIT:
                self.stdout.write('  batch limit reached; the next run continues.')
        if crashed or failed:
            raise CommandError(f'expire_bookings had problems (crashed passes: {crashed}); see the log.')