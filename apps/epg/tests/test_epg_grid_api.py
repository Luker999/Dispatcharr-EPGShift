from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from rest_framework import status
from rest_framework.test import APIClient

from apps.channels.models import Channel, ChannelGroup
from apps.epg.models import EPGData, EPGSource, ProgramData

User = get_user_model()

GRID_URL = "/api/epg/grid/"


class EpgGridOffsetWindowTests(TestCase):
    """The grid fetch window [now - 1h, now + 24h] is widened by the
    largest |epg_time_offset_minutes| so shifted channels keep coverage
    for their full displayed range."""

    def setUp(self):
        user = User.objects.create_user(
            username="testuser", password="testpass123"
        )
        user.user_level = 10
        user.save()
        self.client = APIClient()
        self.client.force_authenticate(user=user)

        self.now = timezone.now()
        self.source = EPGSource.objects.create(
            name="Grid XMLTV",
            source_type="xmltv",
            url="http://example.com/epg.xml",
        )
        self.epg_data = EPGData.objects.create(
            tvg_id="grid.channel",
            name="Grid Channel",
            epg_source=self.source,
        )
        group = ChannelGroup.objects.create(name="Grid Group")
        self.channel = Channel.objects.create(
            channel_number=10.0,
            name="Grid Channel",
            channel_group=group,
            epg_data=self.epg_data,
        )

    def _make_program(self, title, start, end):
        return ProgramData.objects.create(
            epg=self.epg_data,
            start_time=start,
            end_time=end,
            title=title,
            tvg_id="grid.channel",
        )

    def _titles(self):
        response = self.client.get(GRID_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {program["title"] for program in response.data["data"]}

    def test_base_window_excludes_padding_programme_without_offset(self):
        self._make_program(
            "Inside Base",
            self.now,
            self.now + timezone.timedelta(minutes=30),
        )
        # 15 min past the un-widened horizon; only reachable as offset
        # padding, never as part of the base grid window.
        self._make_program(
            "Offset Padding",
            self.now + timezone.timedelta(hours=24, minutes=15),
            self.now + timezone.timedelta(hours=25),
        )

        self.assertEqual(
            self._titles(),
            {"Inside Base"},
        )

    def test_window_widens_by_max_abs_offset(self):
        self._make_program(
            "Inside Base",
            self.now,
            self.now + timezone.timedelta(minutes=30),
        )
        self._make_program(
            "Offset Padding",
            self.now + timezone.timedelta(hours=24, minutes=15),
            self.now + timezone.timedelta(hours=25),
        )

        self.channel.epg_time_offset_minutes = 1440
        self.channel.save()

        self.assertEqual(
            self._titles(),
            {"Inside Base", "Offset Padding"},
        )
