"""Add per-channel EPG time offset (delayed re-broadcast display shift)."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dispatcharr_channels", "0038_add_catchup_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="channel",
            name="epg_time_offset_minutes",
            field=models.IntegerField(
                blank=True,
                default=None,
                help_text=(
                    "Shift EPG program times for this channel by this many minutes. "
                    "Positive means the channel airs programs later than the EPG "
                    "source's times (e.g. a delayed re-broadcast)."
                ),
                null=True,
            ),
        ),
    ]
