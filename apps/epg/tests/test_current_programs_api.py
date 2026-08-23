from datetime import datetime
from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from apps.epg.models import EPGSource, EPGData, ProgramData

User = get_user_model()

CURRENT_PROGRAMS_URL = "/api/epg/current-programs/"


class CurrentProgramsAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", password="testpass123"
        )
        self.user.user_level = 10
        self.user.save()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.now = timezone.now()

        # Create an XMLTV source with programmes
        self.source = EPGSource.objects.create(
            name="Test XMLTV",
            source_type="xmltv",
            url="http://example.com/epg.xml",
        )
        self.epg_data = EPGData.objects.create(
            tvg_id="test.channel",
            name="Test Channel",
            epg_source=self.source,
        )
        self.program = ProgramData.objects.create(
            epg=self.epg_data,
            start_time=self.now - timezone.timedelta(hours=1),
            end_time=self.now + timezone.timedelta(hours=1),
            title="Current Show",
            description="A show currently airing",
            tvg_id="test.channel",
        )

        # Dummy EPG source
        self.dummy_source = EPGSource.objects.create(
            name="Dummy EPG",
            source_type="dummy",
        )
        self.dummy_epg = EPGData.objects.create(
            tvg_id="dummy.channel",
            name="Dummy Channel",
            epg_source=self.dummy_source,
        )

    def test_returns_program_for_current_time_window(self):
        response = self.client.post(
            CURRENT_PROGRAMS_URL,
            {"epg_data_ids": [self.epg_data.id]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["title"], "Current Show")
        self.assertEqual(response.data[0]["epg_data_id"], self.epg_data.id)

    def test_program_payload_has_expected_fields(self):
        response = self.client.post(
            CURRENT_PROGRAMS_URL,
            {"epg_data_ids": [self.epg_data.id]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        payload = response.data[0]
        expected_keys = {
            "id",
            "start_time",
            "end_time",
            "title",
            "description",
            "sub_title",
            "tvg_id",
            "epg_data_id",
        }
        self.assertTrue(expected_keys.issubset(set(payload.keys())))
        self.assertEqual(payload["epg_data_id"], self.epg_data.id)

    @patch("apps.epg.api_views.find_current_program_for_tvg_id", return_value=None)
    def test_returns_empty_when_no_program_matches(self, mock_find):
        # Create EPG data with no DB programme and fallback returns None
        epg_no_prog = EPGData.objects.create(
            tvg_id="no.programme",
            name="No Programme Channel",
            epg_source=self.source,
        )
        response = self.client.post(
            CURRENT_PROGRAMS_URL,
            {"epg_data_ids": [epg_no_prog.id]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    @patch(
        "apps.epg.api_views.find_current_program_for_tvg_id",
        return_value="timeout",
    )
    def test_returns_parsing_sentinel_on_timeout(self, mock_find):
        epg_no_prog = EPGData.objects.create(
            tvg_id="timeout.channel",
            name="Timeout Channel",
            epg_source=self.source,
        )
        response = self.client.post(
            CURRENT_PROGRAMS_URL,
            {"epg_data_ids": [epg_no_prog.id]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertTrue(response.data[0]["parsing"])
        self.assertEqual(response.data[0]["epg_data_id"], epg_no_prog.id)

    def test_400_when_both_channel_uuids_and_epg_data_ids(self):
        response = self.client.post(
            CURRENT_PROGRAMS_URL,
            {"channel_uuids": ["abc"], "epg_data_ids": [1]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not both", response.data["error"])

    def test_skips_dummy_epg_sources(self):
        response = self.client.post(
            CURRENT_PROGRAMS_URL,
            {"epg_data_ids": [self.dummy_epg.id]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_enforces_50_id_limit(self):
        # Create 55 EPG entries, each with a current programme so DB lookup
        # handles them all (no fallback to find_current_program_for_tvg_id).
        ids = []
        for i in range(55):
            epg = EPGData.objects.create(
                tvg_id=f"limit.{i}",
                name=f"Limit Channel {i}",
                epg_source=self.source,
            )
            ProgramData.objects.create(
                epg=epg,
                start_time=self.now - timezone.timedelta(hours=1),
                end_time=self.now + timezone.timedelta(hours=1),
                title=f"Show {i}",
                tvg_id=f"limit.{i}",
            )
            ids.append(epg.id)

        response = self.client.post(
            CURRENT_PROGRAMS_URL,
            {"epg_data_ids": ids},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # The view truncates to 50 IDs, so at most 50 results
        self.assertLessEqual(len(response.data), 50)

    def test_400_for_non_integer_epg_data_ids(self):
        response = self.client.post(
            CURRENT_PROGRAMS_URL,
            {"epg_data_ids": ["abc", "def"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("valid integers", response.data["error"])

    def test_channel_uuids_honours_epg_override(self):
        """Hand-assigned EPG on ChannelOverride must resolve via channel_uuids."""
        from apps.channels.models import Channel, ChannelGroup, ChannelOverride

        group = ChannelGroup.objects.create(name="Override Current Prog")
        channel = Channel.objects.create(
            channel_number=9.0,
            name="Provider Name",
            channel_group=group,
            epg_data=None,
            auto_created=True,
        )
        ChannelOverride.objects.create(channel=channel, epg_data=self.epg_data)

        response = self.client.post(
            CURRENT_PROGRAMS_URL,
            {"channel_uuids": [str(channel.uuid)]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["title"], "Current Show")
        self.assertEqual(response.data[0]["channel_uuid"], str(channel.uuid))

    def test_explicit_zero_offset_returns_unshifted_program(self):
        response = self.client.post(
            CURRENT_PROGRAMS_URL,
            {"epg_data_ids": [self.epg_data.id], "time_offset_minutes": 0},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        payload = response.data[0]
        self.assertEqual(payload["title"], "Current Show")
        # offset 0 must leave the serializer output untouched
        self.assertTrue(payload["start_time"].endswith("Z"))

    @patch("apps.epg.api_views.find_current_program_for_tvg_id", return_value=None)
    def test_time_offset_shifts_lookup_and_returned_times(self, mock_find):
        # Use a dedicated EPG entry so the no-offset control cannot match the
        # "Current Show" created in setUp on self.epg_data.
        epg_delayed = EPGData.objects.create(
            tvg_id="test.channel.delayed",
            name="Delayed Channel",
            epg_source=self.source,
        )
        # Source program only covers now-3h..now-1.5h, so without an offset
        # it does not match "now".
        ProgramData.objects.create(
            epg=epg_delayed,
            start_time=self.now - timezone.timedelta(hours=3),
            end_time=self.now - timezone.timedelta(minutes=90),
            title="Delayed Show",
            tvg_id="test.channel.delayed",
        )

        response = self.client.post(
            CURRENT_PROGRAMS_URL,
            {"epg_data_ids": [epg_delayed.id]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

        # With +120 the lookup moves to now-2h and returned times shift +2h
        response = self.client.post(
            CURRENT_PROGRAMS_URL,
            {"epg_data_ids": [epg_delayed.id], "time_offset_minutes": 120},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        payload = response.data[0]
        self.assertEqual(payload["title"], "Delayed Show")
        self.assertEqual(
            datetime.fromisoformat(payload["start_time"]),
            self.now - timezone.timedelta(hours=1),
        )
        self.assertEqual(
            datetime.fromisoformat(payload["end_time"]),
            self.now + timezone.timedelta(minutes=30),
        )

    @patch(
        "apps.epg.api_views.find_current_program_for_tvg_id",
        return_value={
            "id": 7,
            "start_time": "2025-01-01T10:00:00Z",
            "end_time": "2025-01-01T11:00:00Z",
            "title": "Indexed Show",
            "tvg_id": "indexed.channel",
        },
    )
    def test_fallback_result_is_shifted_and_gets_as_of(self, mock_find):
        epg_indexed = EPGData.objects.create(
            tvg_id="indexed.channel",
            name="Indexed Channel",
            epg_source=self.source,
        )
        response = self.client.post(
            CURRENT_PROGRAMS_URL,
            {"epg_data_ids": [epg_indexed.id], "time_offset_minutes": 90},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        payload = response.data[0]
        self.assertEqual(payload["title"], "Indexed Show")
        self.assertEqual(
            datetime.fromisoformat(payload["start_time"]),
            datetime.fromisoformat("2025-01-01T10:00:00Z")
            + timezone.timedelta(minutes=90),
        )
        self.assertEqual(
            datetime.fromisoformat(payload["end_time"]),
            datetime.fromisoformat("2025-01-01T11:00:00Z")
            + timezone.timedelta(minutes=90),
        )
        # Fallback receives the shifted reference time, not raw "now"
        mock_find.assert_called_once()
        as_of = mock_find.call_args.kwargs["as_of"]
        self.assertLess(
            abs((as_of - (self.now - timezone.timedelta(minutes=90))).total_seconds()),
            60,
        )

    def test_channel_branch_applies_channel_time_offset(self):
        from apps.channels.models import Channel, ChannelGroup

        group = ChannelGroup.objects.create(name="Offset Current Prog")
        channel = Channel.objects.create(
            channel_number=11.0,
            name="Delayed Channel",
            channel_group=group,
            epg_data=self.epg_data,
            auto_created=False,
            epg_time_offset_minutes=120,
        )
        # Source program only covers now-3h..now-1.5h
        ProgramData.objects.create(
            epg=self.epg_data,
            start_time=self.now - timezone.timedelta(hours=3),
            end_time=self.now - timezone.timedelta(minutes=90),
            title="Delayed Channel Show",
            tvg_id="test.channel",
        )

        response = self.client.post(
            CURRENT_PROGRAMS_URL,
            {"channel_uuids": [str(channel.uuid)]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        payload = response.data[0]
        self.assertEqual(payload["title"], "Delayed Channel Show")
        self.assertEqual(payload["channel_uuid"], str(channel.uuid))
        self.assertEqual(
            datetime.fromisoformat(payload["start_time"]),
            self.now - timezone.timedelta(hours=1),
        )

        # Control: without the offset the lookup uses raw "now", so the
        # channel matches the unshifted "Current Show" window (now-1h..now+1h)
        # instead of the delayed program.
        # The offset-change post_save signal dispatches the recording
        # reschedule task; keep this display-only test hermetic.
        with patch(
            "apps.channels.tasks.reschedule_upcoming_recordings_for_offset_change"
        ):
            channel.epg_time_offset_minutes = None
            channel.save()
        response = self.client.post(
            CURRENT_PROGRAMS_URL,
            {"channel_uuids": [str(channel.uuid)]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["title"], "Current Show")

    def test_accepts_boundary_time_offsets(self):
        for boundary in (1440, -1440):
            with self.subTest(boundary=boundary):
                response = self.client.post(
                    CURRENT_PROGRAMS_URL,
                    {
                        "epg_data_ids": [self.epg_data.id],
                        "time_offset_minutes": boundary,
                    },
                    format="json",
                )
                self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_400_for_out_of_range_time_offset_minutes(self):
        for bad in (1441, -1441):
            with self.subTest(boundary=bad):
                response = self.client.post(
                    CURRENT_PROGRAMS_URL,
                    {
                        "epg_data_ids": [self.epg_data.id],
                        "time_offset_minutes": bad,
                    },
                    format="json",
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("time_offset_minutes", response.data["error"])

    def test_400_for_non_integer_time_offset_minutes(self):
        for bad in ("abc", True, 1.5, "90.5"):
            with self.subTest(value=bad):
                response = self.client.post(
                    CURRENT_PROGRAMS_URL,
                    {
                        "epg_data_ids": [self.epg_data.id],
                        "time_offset_minutes": bad,
                    },
                    format="json",
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("time_offset_minutes", response.data["error"])

    def test_blank_time_offset_means_no_shift(self):
        response = self.client.post(
            CURRENT_PROGRAMS_URL,
            {
                "epg_data_ids": [self.epg_data.id],
                "time_offset_minutes": "",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["title"], "Current Show")

    def test_auth_required(self):
        anon_client = APIClient()
        response = anon_client.post(
            CURRENT_PROGRAMS_URL,
            {"epg_data_ids": [self.epg_data.id]},
            format="json",
        )
        self.assertIn(
            response.status_code,
            [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN],
        )
