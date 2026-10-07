from django.core.management.base import BaseCommand, CommandError

from core import jobs


class Command(BaseCommand):
    help = 'Park abandoned refunds, finish safe ones, and report refunds that need the owner.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Only report; change nothing.')

    def handle(self, *args, **options):
        dry = options['dry_run']
        res = jobs.process_refunds(dry_run=dry)
        if dry:
            if res['stuck']:
                self.stdout.write(f"[dry run] {res['stuck_total']} refund attempt(s) need attention:")
                for line in res['stuck']:
                    self.stdout.write(f'  {line}')
                if res['stuck_total'] > len(res['stuck']):
                    self.stdout.write(f"  ... and {res['stuck_total'] - len(res['stuck'])} more")
            else:
                self.stdout.write('[dry run] no stuck refunds')
        else:
            # Real runs print only when something happened or a NEW alert fired (keeps cron mail quiet).
            if res['parked'] or res['finished'] or res['failed']:
                self.stdout.write(
                    f"parked as unknown: {res['parked']}, finished automatically: {res['finished']}, "
                    f"could not finish: {res['failed']}"
                )
            if res['alerted_total']:
                self.stdout.write(self.style.ERROR(f"{res['alerted_total']} refund(s) need the owner:"))
                for line in res['alerted']:
                    self.stdout.write(f'  {line}')
                if res['alerted_total'] > len(res['alerted']):
                    self.stdout.write(f"  ... and {res['alerted_total'] - len(res['alerted'])} more (see the log)")
        if res['failed']:
            raise CommandError('Some refunds could not be processed; see the log.')