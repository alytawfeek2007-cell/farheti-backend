from django.core.management.base import BaseCommand

from core import jobs


class Command(BaseCommand):
    help = 'Email a one-time payment reminder for ACCEPTED bookings close to their deadline. Run hourly.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='List candidates, send nothing.')

    def handle(self, *args, **options):
        result = jobs.send_payment_reminders(dry_run=options['dry_run'])
        self.stdout.write(
            f"candidates={result['candidates']} done={result['done']} "
            f"skipped={result['skipped']} failed={result['failed']}"
        )
        if options['dry_run']:
            self.stdout.write(f"would remind bookings: {result['ids']}")