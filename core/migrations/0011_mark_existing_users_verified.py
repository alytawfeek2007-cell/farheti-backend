from django.db import migrations
from django.utils import timezone


def mark_existing_users_verified(apps, schema_editor):
    User = apps.get_model('auth', 'User')
    EmailVerification = apps.get_model('core', 'EmailVerification')
    already = set(EmailVerification.objects.values_list('user_id', flat=True))
    now = timezone.now()
    EmailVerification.objects.bulk_create(
        [
            EmailVerification(user_id=pk, verified_at=now)
            for pk in User.objects.values_list('pk', flat=True)
            if pk not in already
        ],
        batch_size=500,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0010_email_auth'),
    ]

    operations = [
        migrations.RunPython(mark_existing_users_verified, migrations.RunPython.noop),
    ]