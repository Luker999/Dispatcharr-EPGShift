"""Phase C: EPG time offset in XMLTV, M3U and XC output.

Covers schedule-variant export identity (the shared helper), per-variant
programme time shifts, <channel> deduplication, raw-query window widening,
XC JSON time shifting with unchanged numeric identity, and XMLTV chunk-cache
invalidation on a real offset change.
"""

import re
import xml.etree.ElementTree as ET
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, Client, RequestFactory, SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.channels.models import (
    Channel,
    ChannelGroup,
    ChannelProfile,
    ChannelProfileMembership,
)
from apps.channels.utils import derive_schedule_variant_ids
from apps.epg.models import EPGData, EPGSource, ProgramData
from apps.output.views import xc_get_epg

XMLTV_TIME_FORMAT = "%Y%m%d%H%M%S %z"
TVP_ID_RE = re.compile(r'tvg-id="([^"]*)"')


def _response_text(response):
    """Read body from HttpResponse or StreamingHttpResponse."""
    if getattr(response, "streaming", False):
        return b"".join(response.streaming_content).decode()
    return response.content.decode()


def _parse_xmltv(content):
    root = ET.fromstring(content)
    return root, root.findall("channel"), root.findall("programme")


def _channel_ids(channels):
    return {channel.get("id") for channel in channels}


def _programmes_for(programmes, channel_id):
    return [p for p in programmes if p.get("channel") == channel_id]


def _times(programme_list):
    return {(p.get("start"), p.get("stop")) for p in programme_list}


def _fmt(dt):
    return dt.strftime(XMLTV_TIME_FORMAT)


class EpgOffsetOutputMixin:
    """Isolate HTTP endpoint tests from network ACL, logging, DB teardown,
    Redis chunk caching, and Celery dispatch (mirrors OutputEndpointTestMixin)."""

    def _epg_response_without_redis(self, cache_key, source, **kwargs):
        from django.http import StreamingHttpResponse

        response = StreamingHttpResponse(source(), content_type="application/xml")
        response["Content-Disposition"] = 'attachment; filename="Dispatcharr.xml"'
        response["Cache-Control"] = "no-cache"
        return response

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.group = ChannelGroup.objects.create(name=f"Offset {uuid4().hex[:8]}")
        # New profiles auto-include every channel via signal; clear that.
        self.profile = ChannelProfile.objects.create(
            name=f"off {uuid4().hex[:8]}"
        )
        ChannelProfileMembership.objects.filter(channel_profile=self.profile).delete()
        self.epg_source = EPGSource.objects.create(
            name=f"src {uuid4().hex[:8]}", source_type="xmltv"
        )
        self._network_patch = patch(
            "apps.output.views.network_access_allowed",
            return_value=True,
        )
        self._epg_teardown_patch = patch("apps.output.epg._epg_export_teardown")
        self._log_event_patch = patch("apps.output.views.log_system_event")
        self._epg_log_event_patch = patch("apps.output.epg.log_system_event")
        self._close_db_patch = patch("django.db.close_old_connections")
        self._epg_cache_patch = patch(
            "apps.output.epg.stream_cached_response",
            side_effect=self._epg_response_without_redis,
        )
        # Offset changes in these tests must not enqueue a live reschedule.
        self._reschedule_patcher = patch(
            "apps.channels.tasks.reschedule_upcoming_recordings_for_offset_change"
        )
        self.reschedule_task = self._reschedule_patcher.start()
        self._network_patch.start()
        self._epg_teardown_patch.start()
        self._log_event_patch.start()
        self._epg_log_event_patch.start()
        self._close_db_patch.start()
        self._epg_cache_patch.start()

    def tearDown(self):
        from django.core.cache import cache

        cache.clear()
        self._epg_cache_patch.stop()
        self._close_db_patch.stop()
        self._epg_log_event_patch.stop()
        self._log_event_patch.stop()
        self._epg_teardown_patch.stop()
        self._network_patch.stop()
        self._reschedule_patcher.stop()
        super().tearDown()

    def _epg_data(self, tvg_id):
        return EPGData.objects.create(
            name=tvg_id, tvg_id=tvg_id, epg_source=self.epg_source
        )

    def _channel(self, *, number, name, tvg_id=None, epg_data=None,
                 offset=None, stationid=None):
        channel = Channel.objects.create(
            channel_group=self.group,
            channel_number=number,
            name=name,
            tvg_id=tvg_id,
            tvc_guide_stationid=stationid,
            epg_data=epg_data,
            epg_time_offset_minutes=offset,
        )
        ChannelProfileMembership.objects.create(
            channel_profile=self.profile,
            channel=channel,
            enabled=True,
        )
        return channel

    def _programme(self, epg_data, start, end, title):
        return ProgramData.objects.create(
            epg=epg_data,
            start_time=start,
            end_time=end,
            title=title,
            tvg_id=epg_data.tvg_id,
        )

    def _epg_content(self, query):
        url = reverse("output:epg_endpoint", kwargs={"profile_name": self.profile.name})
        response = self.client.get(f"{url}?{query}")
        self.assertEqual(response.status_code, 200)
        return _response_text(response)

    def _epg_parsed(self, query):
        return _parse_xmltv(self._epg_content(query))

    def _m3u_tvg_ids(self, query):
        from django.core.cache import cache

        cache.clear()
        url = reverse("output:m3u_endpoint", kwargs={"profile_name": self.profile.name})
        response = self.client.get(f"{url}?{query}")
        self.assertEqual(response.status_code, 200)
        return TVP_ID_RE.findall(_response_text(response))


class ScheduleVariantIdHelperTests(SimpleTestCase):
    """The shared schedule-variant ID helper (no DB)."""

    class _Stub:
        def __init__(self, id, number, offset):
            self.id = id
            self.effective_channel_number = number
            self.epg_time_offset_minutes = offset

    def test_none_and_zero_offsets_use_canonical_base_id(self):
        for offset in (None, 0):
            with self.subTest(offset=offset):
                channel = self._Stub(1, 10.0, offset)
                id_map, _ = derive_schedule_variant_ids(
                    [(channel, "BBCOne")], "tvg_id"
                )
                self.assertEqual(id_map[1], "BBCOne")
                self.assertNotIn("__0m", id_map[1])

    def test_nonzero_offset_derives_suffixed_id(self):
        for offset, expected in ((180, "BBCOne__180m"), (-45, "BBCOne__-45m")):
            with self.subTest(offset=offset):
                channel = self._Stub(1, 10.0, offset)
                id_map, _ = derive_schedule_variant_ids(
                    [(channel, "BBCOne")], "tvg_id"
                )
                self.assertEqual(id_map[1], expected)

    def test_gracenote_source_uses_same_derivation(self):
        channel = self._Stub(1, 10.0, 90)
        id_map, _ = derive_schedule_variant_ids([(channel, "GN01")], "gracenote")
        self.assertEqual(id_map[1], "GN01__90m")

    def test_channel_number_source_retains_base_id(self):
        for offset in (None, 0, 180, -45):
            with self.subTest(offset=offset):
                channel = self._Stub(1, 10.0, offset)
                id_map, _ = derive_schedule_variant_ids([(channel, "10")], "channel_number")
                self.assertEqual(id_map[1], "10")

    def test_clearing_offset_returns_to_canonical_id(self):
        channel = self._Stub(1, 10.0, 180)
        shifted, _ = derive_schedule_variant_ids([(channel, "BBCOne")], "tvg_id")
        self.assertEqual(shifted[1], "BBCOne__180m")
        channel.epg_time_offset_minutes = None
        cleared, _ = derive_schedule_variant_ids([(channel, "BBCOne")], "tvg_id")
        self.assertEqual(cleared[1], "BBCOne")

    def test_same_source_same_offset_shares_one_id(self):
        a = self._Stub(1, 10.0, 180)
        b = self._Stub(2, 11.0, 180)
        id_map, reps = derive_schedule_variant_ids(
            [(a, "ShareTV"), (b, "ShareTV")], "tvg_id"
        )
        self.assertEqual(id_map[1], "ShareTV__180m")
        self.assertEqual(id_map[1], id_map[2])
        self.assertIs(reps["ShareTV__180m"], a)

    def test_same_source_different_offsets_distinct(self):
        a = self._Stub(1, 10.0, None)
        b = self._Stub(2, 11.0, 180)
        c = self._Stub(3, 12.0, -45)
        id_map, _ = derive_schedule_variant_ids(
            [(a, "TriTV"), (b, "TriTV"), (c, "TriTV")], "tvg_id"
        )
        self.assertEqual(
            {id_map[1], id_map[2], id_map[3]},
            {"TriTV", "TriTV__180m", "TriTV__-45m"},
        )

    def test_derived_id_colliding_with_reserved_canonical_id(self):
        canonical = self._Stub(1, 10.0, None)  # base is literally "A__180m"
        derived = self._Stub(2, 11.0, 180)  # base "A" -> "A__180m" -> taken
        id_map, _ = derive_schedule_variant_ids(
            [(derived, "A"), (canonical, "A__180m")], "tvg_id"
        )
        self.assertEqual(id_map[1], "A__180m")
        self.assertEqual(id_map[2], "A__180m_2")

    def test_derived_id_collision_skips_reserved_suffixes(self):
        c1 = self._Stub(1, 10.0, None)  # canonical "X__180m"
        c2 = self._Stub(2, 11.0, None)  # canonical "X__180m_2"
        c3 = self._Stub(3, 12.0, 180)  # "X" -> X__180m, _2 both taken
        id_map, _ = derive_schedule_variant_ids(
            [(c1, "X__180m"), (c2, "X__180m_2"), (c3, "X")], "tvg_id"
        )
        self.assertEqual(id_map[1], "X__180m")
        self.assertEqual(id_map[2], "X__180m_2")
        self.assertEqual(id_map[3], "X__180m_3")

    def test_mapping_is_deterministic_regardless_of_input_order(self):
        a = self._Stub(1, 10.0, None)
        b = self._Stub(2, 11.0, 180)
        c = self._Stub(3, 12.0, 180)
        pairs = [(a, "A"), (b, "A"), (c, "A")]
        first, _ = derive_schedule_variant_ids(pairs, "tvg_id")
        second, _ = derive_schedule_variant_ids(list(reversed(pairs)), "tvg_id")
        self.assertEqual(first, second)
        # b and c share (source, offset); a stays canonical.
        self.assertEqual(first[2], first[3])
        self.assertEqual(first[1], "A")
        self.assertEqual(first[2], "A__180m")


class XmltvOffsetExportTests(EpgOffsetOutputMixin, TestCase):
    """XMLTV export identity and programme time shifts."""

    def test_canonical_unshifted_id_and_unchanged_times(self):
        for offset, tvg_id in ((None, "CanonNull"), (0, "CanonZero")):
            with self.subTest(offset=offset):
                epg = self._epg_data(tvg_id)
                self._channel(
                    number=10.0, name=f"C{offset}", tvg_id=tvg_id,
                    epg_data=epg, offset=offset,
                )
                now = timezone.now().replace(second=0, microsecond=0)
                start = now + timedelta(hours=1)
                end = start + timedelta(minutes=90)
                self._programme(epg, start, end, f"Prog{offset}")

                _root, channels, programmes = self._epg_parsed(
                    "tvg_id_source=tvg_id&days=7"
                )
                self.assertIn(tvg_id, _channel_ids(channels))
                self.assertNotIn(f"{tvg_id}__0m", _channel_ids(channels))
                progs = _programmes_for(programmes, tvg_id)
                self.assertEqual(len(progs), 1)
                self.assertEqual(
                    (progs[0].get("start"), progs[0].get("stop")),
                    (_fmt(start), _fmt(end)),
                )

    def test_positive_offset_shifts_programme_times_later(self):
        epg = self._epg_data("PosTV")
        self._channel(number=11.0, name="Pos", tvg_id="PosTV", epg_data=epg, offset=180)
        now = timezone.now().replace(second=0, microsecond=0)
        start = now + timedelta(hours=1)
        end = start + timedelta(minutes=45)
        self._programme(epg, start, end, "PosProg")

        _root, channels, programmes = self._epg_parsed(
            "tvg_id_source=tvg_id&days=7"
        )
        self.assertIn("PosTV__180m", _channel_ids(channels))
        self.assertEqual(
            _times(_programmes_for(programmes, "PosTV__180m")),
            {(_fmt(start + timedelta(minutes=180)), _fmt(end + timedelta(minutes=180)))},
        )

    def test_negative_offset_shifts_programme_times_earlier(self):
        epg = self._epg_data("NegTV")
        self._channel(number=12.0, name="Neg", tvg_id="NegTV", epg_data=epg, offset=-45)
        now = timezone.now().replace(second=0, microsecond=0)
        start = now + timedelta(hours=2)
        end = start + timedelta(minutes=60)
        self._programme(epg, start, end, "NegProg")

        _root, channels, programmes = self._epg_parsed(
            "tvg_id_source=tvg_id&days=7"
        )
        self.assertIn("NegTV__-45m", _channel_ids(channels))
        self.assertEqual(
            _times(_programmes_for(programmes, "NegTV__-45m")),
            {(_fmt(start - timedelta(minutes=45)), _fmt(end - timedelta(minutes=45)))},
        )

    def test_same_source_same_offset_one_channel_block_one_programme_set(self):
        epg = self._epg_data("ShareTV")
        self._channel(number=20.0, name="Alpha", tvg_id="ShareTV", epg_data=epg, offset=180)
        self._channel(number=21.0, name="Beta", tvg_id="ShareTV", epg_data=epg, offset=180)
        start = timezone.now() + timedelta(hours=2)
        end = start + timedelta(minutes=30)
        self._programme(epg, start, end, "SharedProg")

        _root, channels, programmes = self._epg_parsed(
            "tvg_id_source=tvg_id&days=7"
        )
        shared_blocks = [ch for ch in channels if ch.get("id") == "ShareTV__180m"]
        self.assertEqual(len(shared_blocks), 1)
        # First physical channel in export order supplies display metadata.
        self.assertEqual(shared_blocks[0].find("display-name").text, "Alpha")
        shared_progs = _programmes_for(programmes, "ShareTV__180m")
        self.assertEqual(len(shared_progs), 1)
        self.assertEqual(shared_progs[0].findtext("title"), "SharedProg")
        self.assertEqual(
            (shared_progs[0].get("start"), shared_progs[0].get("stop")),
            (_fmt(start + timedelta(minutes=180)), _fmt(end + timedelta(minutes=180))),
        )

    def test_same_source_different_offsets_distinct_ids_and_times(self):
        epg = self._epg_data("TriTV")
        self._channel(number=30.0, name="Live", tvg_id="TriTV", epg_data=epg, offset=None)
        self._channel(number=31.0, name="Delay", tvg_id="TriTV", epg_data=epg, offset=180)
        self._channel(number=32.0, name="Early", tvg_id="TriTV", epg_data=epg, offset=-45)
        start = timezone.now() + timedelta(hours=3)
        end = start + timedelta(minutes=60)
        self._programme(epg, start, end, "TriProg")

        _root, channels, programmes = self._epg_parsed(
            "tvg_id_source=tvg_id&days=7"
        )
        self.assertEqual(
            _channel_ids(channels),
            {"TriTV", "TriTV__180m", "TriTV__-45m"},
        )
        for channel_id, offset in (
            ("TriTV", 0),
            ("TriTV__180m", 180),
            ("TriTV__-45m", -45),
        ):
            with self.subTest(channel_id=channel_id):
                progs = _programmes_for(programmes, channel_id)
                self.assertEqual(len(progs), 1)
                self.assertEqual(
                    (progs[0].get("start"), progs[0].get("stop")),
                    (_fmt(start + timedelta(minutes=offset)),
                     _fmt(end + timedelta(minutes=offset))),
                )

    def test_channel_number_mode_retains_ids_and_still_shifts_times(self):
        epg = self._epg_data("NumTV")
        self._channel(number=40.0, name="Num", tvg_id="NumTV", epg_data=epg, offset=180)
        start = timezone.now() + timedelta(hours=2)
        end = start + timedelta(minutes=30)
        self._programme(epg, start, end, "NumProg")

        _root, channels, programmes = self._epg_parsed(
            "tvg_id_source=channel_number&days=7"
        )
        self.assertEqual(_channel_ids(channels), {"40"})
        self.assertEqual(
            _times(_programmes_for(programmes, "40")),
            {(_fmt(start + timedelta(minutes=180)), _fmt(end + timedelta(minutes=180)))},
        )

    def test_clearing_offset_returns_to_canonical_id(self):
        epg = self._epg_data("ClearTV")
        channel = self._channel(
            number=45.0, name="Clear", tvg_id="ClearTV", epg_data=epg, offset=180
        )
        _root, channels, _programmes = self._epg_parsed(
            "tvg_id_source=tvg_id&days=7"
        )
        self.assertEqual(_channel_ids(channels), {"ClearTV__180m"})

        channel.epg_time_offset_minutes = None
        channel.save()

        _root, channels, _programmes = self._epg_parsed(
            "tvg_id_source=tvg_id&days=7"
        )
        self.assertEqual(_channel_ids(channels), {"ClearTV"})

    def test_derived_id_collision_with_existing_canonical_id(self):
        epg_a = self._epg_data("A")
        epg_c = self._epg_data("A__180m")
        self._channel(
            number=50.0, name="Canon", tvg_id="A__180m", epg_data=epg_c, offset=None
        )
        self._channel(number=51.0, name="Shft", tvg_id="A", epg_data=epg_a, offset=180)
        now = timezone.now().replace(second=0, microsecond=0)
        start = now + timedelta(hours=1)
        end = start + timedelta(minutes=30)
        self._programme(epg_a, start, end, "DerivedProg")
        self._programme(epg_c, start, end, "CanonicalProg")

        _root, channels, programmes = self._epg_parsed(
            "tvg_id_source=tvg_id&days=7"
        )
        self.assertEqual(_channel_ids(channels), {"A__180m", "A__180m_2"})
        canonical_progs = _programmes_for(programmes, "A__180m")
        derived_progs = _programmes_for(programmes, "A__180m_2")
        self.assertEqual([p.findtext("title") for p in canonical_progs], ["CanonicalProg"])
        self.assertEqual([p.findtext("title") for p in derived_progs], ["DerivedProg"])
        self.assertEqual(
            (derived_progs[0].get("start"), derived_progs[0].get("stop")),
            (_fmt(start + timedelta(minutes=180)), _fmt(end + timedelta(minutes=180))),
        )

    def test_positive_offset_widens_window_start_backwards(self):
        epg = self._epg_data("WinPos")
        self._channel(number=70.0, name="WP", tvg_id="WinPos", epg_data=epg, offset=180)
        lookback = timezone.now() - timedelta(days=1)
        # Source programme ends 60 minutes before the lookback cutoff: the
        # unwidened query (end_time >= lookback) excludes it, but shifted by
        # +180m it is still airing 120 minutes past the window start.
        end = lookback - timedelta(minutes=60)
        start = end - timedelta(hours=1)
        self._programme(epg, start, end, "WidePos")

        _root, _channels, programmes = self._epg_parsed(
            "tvg_id_source=tvg_id&days=7&prev_days=1"
        )
        progs = _programmes_for(programmes, "WinPos__180m")
        self.assertEqual(len(progs), 1)
        self.assertEqual(
            (progs[0].get("start"), progs[0].get("stop")),
            (_fmt(start + timedelta(minutes=180)), _fmt(end + timedelta(minutes=180))),
        )

    def test_negative_offset_widens_window_end_forwards(self):
        epg = self._epg_data("WinNeg")
        self._channel(number=71.0, name="WN", tvg_id="WinNeg", epg_data=epg, offset=-45)
        cutoff = timezone.now() + timedelta(days=7)
        # Source programme starts 30 minutes after the cutoff: the unwidened
        # query (start_time < cutoff) excludes it, but shifted by -45m it
        # starts 15 minutes before the window end.
        start = cutoff + timedelta(minutes=30)
        end = start + timedelta(minutes=30)
        self._programme(epg, start, end, "WideNeg")

        _root, _channels, programmes = self._epg_parsed(
            "tvg_id_source=tvg_id&days=7"
        )
        progs = _programmes_for(programmes, "WinNeg__-45m")
        self.assertEqual(len(progs), 1)
        self.assertEqual(
            (progs[0].get("start"), progs[0].get("stop")),
            (_fmt(start - timedelta(minutes=45)), _fmt(end - timedelta(minutes=45))),
        )


class M3uOffsetExportTests(EpgOffsetOutputMixin, TestCase):
    """M3U tvg-id uses the exact same mapping as XMLTV."""

    def test_shared_variant_streams_share_one_tvg_id(self):
        epg = self._epg_data("M3uShare")
        self._channel(number=60.0, name="Alpha", tvg_id="M3uShare", epg_data=epg, offset=180)
        self._channel(number=61.0, name="Beta", tvg_id="M3uShare", epg_data=epg, offset=180)

        tvg_ids = self._m3u_tvg_ids("tvg_id_source=tvg_id&days=7")
        self.assertEqual(tvg_ids, ["M3uShare__180m", "M3uShare__180m"])

    def test_different_offsets_get_distinct_tvg_ids(self):
        epg = self._epg_data("M3uTri")
        self._channel(number=62.0, name="Live", tvg_id="M3uTri", epg_data=epg, offset=None)
        self._channel(number=63.0, name="Delay", tvg_id="M3uTri", epg_data=epg, offset=180)
        self._channel(number=64.0, name="Early", tvg_id="M3uTri", epg_data=epg, offset=-45)

        tvg_ids = self._m3u_tvg_ids("tvg_id_source=tvg_id&days=7")
        self.assertEqual(tvg_ids, ["M3uTri", "M3uTri__180m", "M3uTri__-45m"])

    def test_channel_number_mode_tvg_id_unchanged(self):
        epg = self._epg_data("M3uNum")
        self._channel(number=65.0, name="Num", tvg_id="M3uNum", epg_data=epg, offset=180)

        tvg_ids = self._m3u_tvg_ids("tvg_id_source=channel_number&days=7")
        self.assertEqual(tvg_ids, ["65"])

    def test_tvg_id_matches_xmltv_channel_and_programme_ids(self):
        epg = self._epg_data("ConsTV")
        self._channel(number=66.0, name="Cons", tvg_id="ConsTV", epg_data=epg, offset=-45)
        start = timezone.now() + timedelta(hours=2)
        self._programme(epg, start, start + timedelta(minutes=30), "ConsProg")

        _root, channels, programmes = self._epg_parsed(
            "tvg_id_source=tvg_id&days=7"
        )
        tvg_ids = self._m3u_tvg_ids("tvg_id_source=tvg_id&days=7")
        self.assertEqual(tvg_ids, ["ConsTV__-45m"])
        self.assertIn("ConsTV__-45m", _channel_ids(channels))
        progs = _programmes_for(programmes, "ConsTV__-45m")
        self.assertEqual(len(progs), 1)
        self.assertEqual(progs[0].get("channel"), tvg_ids[0])


class XcXmltvOffsetExportTests(EpgOffsetOutputMixin, TestCase):
    """XC XMLTV inherits the XMLTV variant-ID behaviour through generate_epg."""

    def test_xc_xmltv_uses_variant_ids(self):
        xc_user = User.objects.create_user(
            username=f"xcx-{uuid4().hex[:8]}",
            password="pass",
            custom_properties={"xc_password": "xcpass"},
        )
        epg = self._epg_data("XcXml")
        self._channel(number=80.0, name="XcX", tvg_id="XcXml", epg_data=epg, offset=180)

        response = self.client.get(
            "/xmltv.php",
            {
                "username": xc_user.username,
                "password": "xcpass",
                "tvg_id_source": "tvg_id",
                "days": 7,
            },
        )
        self.assertEqual(response.status_code, 200)
        _root, channels, _programmes = _parse_xmltv(_response_text(response))
        self.assertIn("XcXml__180m", _channel_ids(channels))


class XcJsonOffsetTests(TestCase):
    """XC JSON schedule shifts times; numeric identity stays unchanged."""

    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            username=f"xcj-{uuid4().hex[:8]}",
            password="pass",
            custom_properties={"xc_password": "xcpass"},
        )
        self.user.user_level = 10
        self.user.save()
        self.group = ChannelGroup.objects.create(name=f"XcJson {uuid4().hex[:8]}")
        self.epg_source = EPGSource.objects.create(
            name=f"xcsrc {uuid4().hex[:8]}", source_type="xmltv"
        )
        self.epg_data = EPGData.objects.create(
            name="XcTV", tvg_id="XcTV", epg_source=self.epg_source
        )

    def _channel(self, offset):
        return Channel.objects.create(
            channel_group=self.group,
            channel_number=100.0,
            name="XcCh",
            tvg_id="XcTV",
            epg_data=self.epg_data,
            epg_time_offset_minutes=offset,
        )

    def _request(self, channel, **params):
        params.setdefault("stream_id", channel.id)
        return self.factory.get("/player_api.php", params)

    def _listing(self, result, index=0):
        listings = result["epg_listings"]
        self.assertEqual(len(listings), 1)
        return listings[index]

    def test_positive_offset_shifts_times_keeps_numeric_identity(self):
        channel = self._channel(180)
        start = timezone.now() + timedelta(hours=1)
        end = start + timedelta(minutes=60)
        program = ProgramData.objects.create(
            epg=self.epg_data, start_time=start, end_time=end,
            title="XcProg", tvg_id="XcTV",
        )

        result = xc_get_epg(self._request(channel, limit=10, days=7), self.user, short=False)
        listing = self._listing(result)
        self.assertEqual(
            listing["start"], (start + timedelta(minutes=180)).strftime("%Y-%m-%d %H:%M:%S")
        )
        self.assertEqual(
            listing["end"], (end + timedelta(minutes=180)).strftime("%Y-%m-%d %H:%M:%S")
        )
        self.assertEqual(listing["stream_id"], str(channel.id))
        self.assertEqual(listing["channel_id"], "100")
        self.assertEqual(listing["id"], str(program.id))

    def test_negative_offset_shifts_times_keeps_numeric_identity(self):
        channel = self._channel(-45)
        start = timezone.now() + timedelta(hours=1)
        end = start + timedelta(minutes=60)
        program = ProgramData.objects.create(
            epg=self.epg_data, start_time=start, end_time=end,
            title="XcProg", tvg_id="XcTV",
        )

        result = xc_get_epg(self._request(channel, limit=10, days=7), self.user, short=False)
        listing = self._listing(result)
        self.assertEqual(
            listing["start"], (start - timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:%S")
        )
        self.assertEqual(
            listing["end"], (end - timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:%S")
        )
        self.assertEqual(listing["stream_id"], str(channel.id))
        self.assertEqual(listing["channel_id"], "100")
        self.assertEqual(listing["id"], str(program.id))

    def test_unshifted_channel_returns_source_times(self):
        channel = self._channel(None)
        start = timezone.now() + timedelta(hours=1)
        end = start + timedelta(minutes=60)
        ProgramData.objects.create(
            epg=self.epg_data, start_time=start, end_time=end,
            title="XcProg", tvg_id="XcTV",
        )

        result = xc_get_epg(self._request(channel, limit=10, days=7), self.user, short=False)
        listing = self._listing(result)
        self.assertEqual(listing["start"], start.strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual(listing["end"], end.strftime("%Y-%m-%d %H:%M:%S"))

    def test_short_epg_includes_shifted_currently_airing_programme(self):
        # Source programme aired 4h..1h before now; with a +180m offset it
        # airs now-1h..now+2h, i.e. is currently airing. The unwidened short
        # query (end_time > now) would miss it.
        channel = self._channel(180)
        now = timezone.now()
        ProgramData.objects.create(
            epg=self.epg_data,
            start_time=now - timedelta(hours=4),
            end_time=now - timedelta(hours=1),
            title="AirNow",
            tvg_id="XcTV",
        )

        result = xc_get_epg(self._request(channel, limit=10), self.user, short=True)
        listing = self._listing(result)
        self.assertEqual(listing["stream_id"], str(channel.id))
        self.assertEqual(
            listing["end"], (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        )


class OffsetXmltvCacheInvalidationTests(EpgOffsetOutputMixin, TestCase):
    """End-to-end: a real offset change drops the warm XMLTV chunk cache."""

    def test_offset_change_drops_warm_chunk_cache_and_rebuilds(self):
        epg = self._epg_data("CacheTV")
        channel = self._channel(
            number=90.0, name="CacheCh", tvg_id="CacheTV", epg_data=epg, offset=None
        )
        start = timezone.now() + timedelta(hours=1)
        self._programme(epg, start, start + timedelta(minutes=30), "CacheProg")

        from django_redis import get_redis_connection

        def _has_ready_epg_cache():
            return any(
                key.endswith(b":ready")
                for key in redis.scan_iter(match=b"epg_content:*", count=200)
            )

        url = reverse("output:epg_endpoint", kwargs={"profile_name": self.profile.name})
        query = "tvg_id_source=tvg_id&days=7"
        redis = get_redis_connection("default")

        # Run the real Redis chunk-cache path for this one test.
        self._epg_cache_patch.stop()
        try:
            first = _response_text(self.client.get(f"{url}?{query}"))
            self.assertIn('id="CacheTV"', first)
            self.assertTrue(_has_ready_epg_cache())

            channel.epg_time_offset_minutes = 180
            channel.save()

            self.assertFalse(_has_ready_epg_cache())

            second = _response_text(self.client.get(f"{url}?{query}"))
            self.assertIn('id="CacheTV__180m"', second)
        finally:
            self._epg_cache_patch.start()
