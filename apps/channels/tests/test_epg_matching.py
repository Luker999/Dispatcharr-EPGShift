"""Tests for the _match_epg_program_by_timeslot() helper in tasks.py.

Covers:
  - Exact time-slot match returns program dict
  - 80% overlap threshold: at boundary, above, and below
  - Multiple overlapping programs: dominant vs. evenly split
  - Edge cases: None inputs, zero-duration recording, no EPG data
  - Returned dict structure (id, title, sub_title, description)
"""
import os
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.channels.models import Channel, Recording
from apps.epg.models import EPGSource, EPGData, ProgramData
from apps.channels.tasks import _match_epg_program_by_timeslot


class EpgMatchingSetupMixin:
    """Shared setup for EPG matching tests."""

    def setUp(self):
        self.source = EPGSource.objects.create(name="Test Source")
        self.epg = EPGData.objects.create(
            tvg_id="test.channel", name="Test Channel EPG", epg_source=self.source,
        )
        self.channel = Channel.objects.create(
            channel_number=50, name="EPG Match Channel", epg_data=self.epg,
        )
        self.base = timezone.now().replace(second=0, microsecond=0)

    def _prog(self, offset_min, duration_min, title="Test Show", **kwargs):
        """Create a ProgramData starting offset_min from self.base."""
        start = self.base + timedelta(minutes=offset_min)
        end = start + timedelta(minutes=duration_min)
        return ProgramData.objects.create(
            epg=self.epg, start_time=start, end_time=end, title=title, **kwargs,
        )


class ExactMatchTests(EpgMatchingSetupMixin, TestCase):
    """Recording window exactly matches an EPG program."""

    def test_exact_match_returns_program_dict(self):
        prog = self._prog(0, 60, title="News at 9", sub_title="Top Stories",
                          description="Evening news broadcast")
        result = _match_epg_program_by_timeslot(
            self.epg, prog.start_time, prog.end_time,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], prog.id)
        self.assertEqual(result["title"], "News at 9")
        self.assertEqual(result["sub_title"], "Top Stories")
        self.assertEqual(result["description"], "Evening news broadcast")

    def test_missing_optional_fields_returned_as_empty_strings(self):
        prog = self._prog(0, 30, title="Minimal Show")
        result = _match_epg_program_by_timeslot(
            self.epg, prog.start_time, prog.end_time,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["sub_title"], "")
        self.assertEqual(result["description"], "")


class OverlapThresholdTests(EpgMatchingSetupMixin, TestCase):
    """80% overlap threshold boundary tests."""

    def test_exactly_80_percent_overlap_returns_match(self):
        """Program covers exactly 80% of the recording window."""
        # Program: 0-60min, Recording: 0-75min → overlap = 60/75 = 80%
        prog = self._prog(0, 60, title="Borderline Show")
        rec_start = self.base
        rec_end = self.base + timedelta(minutes=75)
        result = _match_epg_program_by_timeslot(self.epg, rec_start, rec_end)
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Borderline Show")

    def test_below_80_percent_returns_none(self):
        """Program covers 79% of the recording — below threshold."""
        # Program: 0-60min, Recording: 0-76min → overlap = 60/76 ≈ 78.9%
        prog = self._prog(0, 60, title="Too Short")
        rec_start = self.base
        rec_end = self.base + timedelta(minutes=76)
        result = _match_epg_program_by_timeslot(self.epg, rec_start, rec_end)
        self.assertIsNone(result)

    def test_above_80_percent_returns_match(self):
        """Program covers 90% of the recording."""
        # Program: 0-60min, Recording: 0-66min → overlap = 60/66 ≈ 90.9%
        prog = self._prog(0, 60, title="Good Match")
        rec_start = self.base
        rec_end = self.base + timedelta(minutes=66)
        result = _match_epg_program_by_timeslot(self.epg, rec_start, rec_end)
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Good Match")


class MultipleProgramTests(EpgMatchingSetupMixin, TestCase):
    """Recording spans multiple EPG programs."""

    def test_dominant_program_returned(self):
        """Recording spans 2 programs; one covers 85%, the other 15%."""
        # Show A: 0-60min, Show B: 60-120min
        # Recording: 9-69min → A overlap=51/60=85%, B overlap=9/60=15%
        self._prog(0, 60, title="Show A")
        self._prog(60, 60, title="Show B")
        rec_start = self.base + timedelta(minutes=9)
        rec_end = self.base + timedelta(minutes=69)
        result = _match_epg_program_by_timeslot(self.epg, rec_start, rec_end)
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Show A")

    def test_evenly_split_returns_none(self):
        """Recording spans 2 equal programs — neither reaches 80%."""
        # Show A: 0-60min, Show B: 60-120min
        # Recording: 30-90min → each covers 50%
        self._prog(0, 60, title="Show A")
        self._prog(60, 60, title="Show B")
        rec_start = self.base + timedelta(minutes=30)
        rec_end = self.base + timedelta(minutes=90)
        result = _match_epg_program_by_timeslot(self.epg, rec_start, rec_end)
        self.assertIsNone(result)

    def test_three_programs_one_dominant(self):
        """Recording spans 3 programs; middle one is dominant."""
        # A: 0-30min, B: 30-90min, C: 90-120min
        # Recording: 25-95min (70min window) → B overlap=60/70≈85.7%
        self._prog(0, 30, title="Show A")
        self._prog(30, 60, title="Show B")
        self._prog(90, 30, title="Show C")
        rec_start = self.base + timedelta(minutes=25)
        rec_end = self.base + timedelta(minutes=95)
        result = _match_epg_program_by_timeslot(self.epg, rec_start, rec_end)
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Show B")


class EdgeCaseTests(EpgMatchingSetupMixin, TestCase):
    """Edge cases and error handling."""

    def test_none_epg_data_returns_none(self):
        result = _match_epg_program_by_timeslot(None, self.base, self.base + timedelta(hours=1))
        self.assertIsNone(result)

    def test_none_start_time_returns_none(self):
        result = _match_epg_program_by_timeslot(self.epg, None, self.base + timedelta(hours=1))
        self.assertIsNone(result)

    def test_none_end_time_returns_none(self):
        result = _match_epg_program_by_timeslot(self.epg, self.base, None)
        self.assertIsNone(result)

    def test_zero_duration_returns_none(self):
        """Recording with start == end should return None."""
        result = _match_epg_program_by_timeslot(self.epg, self.base, self.base)
        self.assertIsNone(result)

    def test_negative_duration_returns_none(self):
        """Recording with end before start should return None."""
        result = _match_epg_program_by_timeslot(
            self.epg, self.base + timedelta(hours=1), self.base,
        )
        self.assertIsNone(result)

    def test_no_overlapping_programs_returns_none(self):
        """No EPG programs in the recording window."""
        self._prog(0, 60, title="Earlier Show")
        rec_start = self.base + timedelta(hours=5)
        rec_end = rec_start + timedelta(hours=1)
        result = _match_epg_program_by_timeslot(self.epg, rec_start, rec_end)
        self.assertIsNone(result)

    def test_empty_epg_no_programs_returns_none(self):
        """EPGData exists but has no programs."""
        result = _match_epg_program_by_timeslot(
            self.epg, self.base, self.base + timedelta(hours=1),
        )
        self.assertIsNone(result)


class OffsetedTimeslotMatchingTests(EpgMatchingSetupMixin, TestCase):
    """The offset_minutes parameter shifts the source EPG lookup.

    The recording window passed in is the real airtime on the channel;
    with a positive offset O the channel airs source programmes later, so
    the source EPG is looked up at recording_time - O (and vice versa for
    negative offsets).
    """

    def test_positive_offset_selects_program_actually_airing(self):
        """O=+120: the real window aligns with the later source programme."""
        # Source: "Show A" 0-60min, "Show B" 60-120min.
        a = self._prog(0, 60, title="Show A")
        b = self._prog(60, 60, title="Show B")
        # B is airing on the channel during the real window 180-240min.
        rec_start = self.base + timedelta(minutes=180)
        rec_end = self.base + timedelta(minutes=240)
        result = _match_epg_program_by_timeslot(
            self.epg, rec_start, rec_end, offset_minutes=120,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], b.id)
        self.assertEqual(result["title"], "Show B")
        # An unshifted lookup of the same real window finds nothing.
        self.assertIsNone(_match_epg_program_by_timeslot(self.epg, rec_start, rec_end))

    def test_negative_offset_selects_program_actually_airing(self):
        """O=-120: the real window aligns with the later source programme,
        while an unshifted lookup would pick the earlier one."""
        # Source: "Show A" 0-60min (real -120..-60), "Show B" 120-180min
        # (real 0-60).
        a = self._prog(0, 60, title="Show A")
        b = self._prog(120, 60, title="Show B")
        rec_start = self.base
        rec_end = self.base + timedelta(minutes=60)
        result = _match_epg_program_by_timeslot(
            self.epg, rec_start, rec_end, offset_minutes=-120,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], b.id)
        self.assertEqual(result["title"], "Show B")
        # Unshifted, the same window would match Show A.
        unshifted = _match_epg_program_by_timeslot(self.epg, rec_start, rec_end)
        self.assertIsNotNone(unshifted)
        self.assertEqual(unshifted["id"], a.id)

    def test_null_and_zero_offsets_preserve_unshifted_lookup(self):
        """offset_minutes=None (unset channel offset) and 0 are no-ops."""
        prog = self._prog(0, 60, title="Plain Show")
        for offset in (None, 0):
            with self.subTest(offset=offset):
                result = _match_epg_program_by_timeslot(
                    self.epg, prog.start_time, prog.end_time, offset_minutes=offset,
                )
                self.assertIsNotNone(result)
                self.assertEqual(result["id"], prog.id)

    def test_positive_offset_exact_boundary_match(self):
        """A real window exactly equal to source programme + O still matches."""
        prog = self._prog(0, 60, title="Edge Show")
        rec_start = prog.start_time + timedelta(minutes=90)
        rec_end = prog.end_time + timedelta(minutes=90)
        result = _match_epg_program_by_timeslot(
            self.epg, rec_start, rec_end, offset_minutes=90,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], prog.id)


@patch("apps.channels.signals.schedule_recording_task", return_value="mock-task-id")
@patch("apps.channels.signals.prefetch_recording_artwork")
class ManualEnrichmentCallPathTests(EpgMatchingSetupMixin, TestCase):
    """Both manual-recording enrichment call paths (run_recording and
    prefetch_recording_artwork) must shift the EPG lookup by the channel's
    epg_time_offset_minutes so title/description/artwork enrichment selects
    the programme actually airing on the delayed/advanced channel.
    """

    RATING_CP = {"rating": "PG-13", "rating_system": "MPAA"}

    def setUp(self):
        super().setUp()
        # Channel offset saves in these tests must not enqueue a live
        # reschedule (the post_save signal dispatches on offset changes).
        self._reschedule_patcher = patch(
            "apps.channels.tasks.reschedule_upcoming_recordings_for_offset_change"
        )
        self._reschedule_patcher.start()

    def tearDown(self):
        self._reschedule_patcher.stop()
        super().tearDown()

    def _set_channel_offset(self, minutes):
        self.channel.epg_time_offset_minutes = minutes
        self.channel.save()

    def _prog_at(self, start, end, title="Test Show", **kwargs):
        return ProgramData.objects.create(
            epg=self.epg, start_time=start, end_time=end, title=title, **kwargs,
        )

    def _run_prefetch(self, rec):
        from apps.channels.tasks import prefetch_recording_artwork
        # The eager .apply() fires celery's task_prerun/task_postrun hooks,
        # which call close_old_connections() and would drop the test's DB
        # connection (see dispatcharr/celery.py).
        with patch("django.db.close_old_connections"), \
             patch("apps.channels.tasks.requests.get", side_effect=ConnectionError("offline")), \
             patch("apps.channels.tasks._validate_url", return_value=False), \
             patch("core.utils.send_websocket_update"):
            prefetch_recording_artwork.apply(args=[rec.id])
        rec.refresh_from_db()
        return rec

    def _run_recording_task(self, rec):
        from apps.channels.tasks import run_recording
        tmpdir = tempfile.mkdtemp(prefix="dvr-enrich-test-")
        with patch("django.db.close_old_connections"), \
             patch("apps.channels.tasks.async_to_sync", return_value=lambda *a, **k: None), \
             patch(
                 "apps.channels.tasks._build_output_paths",
                 return_value=(
                     os.path.join(tmpdir, "out.mkv"),
                     os.path.join(tmpdir, "hls"),
                     "out.mkv",
                 ),
             ), \
             patch("apps.channels.tasks._resolve_poster_for_program", return_value=(None, None)), \
             patch("core.utils.send_websocket_update"):
            run_recording.apply(
                args=[rec.id, rec.channel_id, rec.start_time.isoformat(), rec.end_time.isoformat()],
            )
        rec.refresh_from_db()
        return rec

    def _saved_program(self, rec):
        return (rec.custom_properties or {}).get("program") or {}

    def test_prefetch_positive_offset_enriches_from_shifted_lookup(
        self, mock_prefetch_signal, mock_schedule,
    ):
        """prefetch_recording_artwork with O=+120 matches the source
        programme airing in the real window, not the raw-time one."""
        self._set_channel_offset(120)
        # Source: "Show A" 0-60min, "Show B" 60-120min (B's real airing: 180-240).
        self._prog(0, 60, title="Show A")
        b = self._prog(60, 60, title="Show B", custom_properties=self.RATING_CP)
        rec = Recording.objects.create(
            channel=self.channel,
            start_time=self.base + timedelta(minutes=180),
            end_time=self.base + timedelta(minutes=240),
            custom_properties={"program": {}},
        )

        rec = self._run_prefetch(rec)

        prog = self._saved_program(rec)
        self.assertEqual(prog.get("id"), b.id)
        self.assertEqual(prog.get("title"), "Show B")
        # Rating enrichment is keyed on the matched program id — proves the
        # shifted lookup selected the right source programme.
        self.assertEqual((rec.custom_properties or {}).get("rating"), "PG-13")

    def test_prefetch_negative_offset_enriches_from_shifted_lookup(
        self, mock_prefetch_signal, mock_schedule,
    ):
        """prefetch_recording_artwork with O=-120 matches the source
        programme airing in the real window (an unshifted lookup would pick
        the earlier source programme)."""
        self._set_channel_offset(-120)
        a = self._prog(0, 60, title="Show A")  # real airing: -120..-60min
        b = self._prog(120, 60, title="Show B", custom_properties=self.RATING_CP)
        rec = Recording.objects.create(
            channel=self.channel,
            start_time=self.base,
            end_time=self.base + timedelta(minutes=60),
            custom_properties={"program": {}},
        )

        rec = self._run_prefetch(rec)

        prog = self._saved_program(rec)
        self.assertEqual(prog.get("id"), b.id)
        self.assertEqual(prog.get("title"), "Show B")
        self.assertIsNotNone(a)  # the unshifted decoy must not have been picked
        self.assertNotEqual(prog.get("title"), a.title)

    def test_prefetch_null_offset_preserves_unshifted_enrichment(
        self, mock_prefetch_signal, mock_schedule,
    ):
        """Offset null: the lookup window is used as-is."""
        self.assertIsNone(self.channel.epg_time_offset_minutes)
        # Rating forces the enriched program to be persisted (the prefetch
        # task only saves when some enrichment field changed).
        prog = self._prog(0, 60, title="Plain Show", custom_properties=self.RATING_CP)
        rec = Recording.objects.create(
            channel=self.channel,
            start_time=prog.start_time,
            end_time=prog.end_time,
            custom_properties={"program": {}},
        )

        rec = self._run_prefetch(rec)

        saved = self._saved_program(rec)
        self.assertEqual(saved.get("id"), prog.id)
        self.assertEqual(saved.get("title"), "Plain Show")
        self.assertEqual((rec.custom_properties or {}).get("rating"), "PG-13")

    def test_run_recording_positive_offset_enriches_saved_program(
        self, mock_prefetch_signal, mock_schedule,
    ):
        """run_recording with O=+120 enriches the persisted program from the
        source programme actually airing in the (past) real window."""
        self._set_channel_offset(120)
        # Past real window so the task exits before starting the stream.
        real_start = timezone.now() - timedelta(hours=5)
        real_end = real_start + timedelta(hours=1)
        # B is the programme airing during the real window (source = real - 120).
        b = self._prog_at(
            real_start - timedelta(minutes=120), real_end - timedelta(minutes=120),
            title="Delayed Show",
        )
        # A sits at the raw real-window times: an unshifted lookup would pick it.
        self._prog_at(real_start, real_end, title="Wrong Show")
        rec = Recording.objects.create(
            channel=self.channel,
            start_time=real_start,
            end_time=real_end,
            custom_properties={"program": {}},
        )

        rec = self._run_recording_task(rec)

        prog = self._saved_program(rec)
        self.assertEqual(prog.get("id"), b.id)
        self.assertEqual(prog.get("title"), "Delayed Show")

    def test_run_recording_negative_offset_enriches_saved_program(
        self, mock_prefetch_signal, mock_schedule,
    ):
        """run_recording with O=-120 enriches the persisted program from the
        source programme actually airing in the (past) real window."""
        self._set_channel_offset(-120)
        real_start = timezone.now() - timedelta(hours=5)
        real_end = real_start + timedelta(hours=1)
        # B is the programme airing during the real window (source = real + 120).
        b = self._prog_at(
            real_start + timedelta(minutes=120), real_end + timedelta(minutes=120),
            title="Advanced Show",
        )
        # A sits at the raw real-window times: an unshifted lookup would pick it.
        self._prog_at(real_start, real_end, title="Wrong Show")
        rec = Recording.objects.create(
            channel=self.channel,
            start_time=real_start,
            end_time=real_end,
            custom_properties={"program": {}},
        )

        rec = self._run_recording_task(rec)

        prog = self._saved_program(rec)
        self.assertEqual(prog.get("id"), b.id)
        self.assertEqual(prog.get("title"), "Advanced Show")
