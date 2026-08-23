"""Tests for series rule evaluation deduplication.

Unit tests verify the dedup logic in evaluate_series_rules_impl.
Integration tests exercise the full path: EPG refresh → series rule
evaluation → Recording creation → post_save signal chain.
"""
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.test import TestCase
from django.utils import timezone

from apps.channels.models import Channel, Recording
from apps.epg.models import EPGSource, EPGData, ProgramData
from core.models import CoreSettings


def _set_series_rules(rules):
    """Helper to store series rules in CoreSettings."""
    CoreSettings.set_dvr_series_rules(rules)


def _set_dvr_offsets(pre_min=0, post_min=0):
    """Helper to store DVR pre/post offsets."""
    CoreSettings._update_group("dvr_settings", "DVR Settings", {
        "pre_offset_minutes": pre_min,
        "post_offset_minutes": post_min,
    })


class SeriesRuleDedupBaseTestCase(TestCase):
    """Shared setup for series rule dedup tests."""

    def setUp(self):
        self.now = timezone.now()
        self.epg_source = EPGSource.objects.create(
            name="Test EPG", source_type="xmltv"
        )
        self.epg = EPGData.objects.create(
            tvg_id="test.channel.1",
            name="Test Channel EPG",
            epg_source=self.epg_source,
        )
        self.channel = Channel.objects.create(
            channel_number=1, name="Test Channel", epg_data=self.epg
        )

        _set_series_rules([{
            "tvg_id": "test.channel.1",
            "mode": "all",
            "title": "Test Show",
        }])
        _set_dvr_offsets(pre_min=0, post_min=0)

    def _create_program(self, hours_from_now=1, title="Test Show",
                        sub_title="Episode 1", tvg_id="test.channel.1"):
        """Create a ProgramData at the given offset."""
        start = self.now + timedelta(hours=hours_from_now)
        end = start + timedelta(hours=1)
        return ProgramData.objects.create(
            epg=self.epg,
            tvg_id=tvg_id,
            start_time=start,
            end_time=end,
            title=title,
            sub_title=sub_title,
        )

    def _simulate_epg_refresh(self, programs_data):
        """Delete all ProgramData and recreate with new IDs (simulates EPG refresh)."""
        ProgramData.objects.filter(epg=self.epg).delete()
        new_programs = []
        for data in programs_data:
            prog = ProgramData.objects.create(epg=self.epg, **data)
            new_programs.append(prog)
        return new_programs

    def _program_data_for_refresh(self, prog):
        """Build the dict needed by _simulate_epg_refresh from a ProgramData."""
        return {
            "tvg_id": prog.tvg_id,
            "start_time": prog.start_time,
            "end_time": prog.end_time,
            "title": prog.title,
            "sub_title": prog.sub_title,
        }


# ---------------------------------------------------------------------------
# Unit tests: dedup logic in evaluate_series_rules_impl
# ---------------------------------------------------------------------------

@patch("apps.channels.tasks.prefetch_recording_artwork")
@patch("apps.channels.signals.schedule_recording_task", return_value="mock-task-id")
class ProgramIdStabilityTests(SeriesRuleDedupBaseTestCase):
    """Verify dedup works after EPG refresh changes ProgramData IDs."""

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_no_duplicate_after_epg_refresh(self, mock_release, mock_lock,
                                            mock_schedule, mock_artwork):
        """Same program should not be recorded twice after EPG refresh."""
        from apps.channels.tasks import evaluate_series_rules_impl

        prog = self._create_program(hours_from_now=2)
        old_id = prog.id
        result1 = evaluate_series_rules_impl()
        self.assertEqual(result1["scheduled"], 1)
        self.assertEqual(Recording.objects.count(), 1)

        new_programs = self._simulate_epg_refresh(
            [self._program_data_for_refresh(prog)]
        )
        self.assertNotEqual(old_id, new_programs[0].id)

        result2 = evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 1)
        self.assertEqual(result2["scheduled"], 0)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_no_duplicate_with_offsets_after_refresh(self, mock_release, mock_lock,
                                                     mock_schedule, mock_artwork):
        """Dedup works when DVR offsets shift Recording times away from program times."""
        from apps.channels.tasks import evaluate_series_rules_impl

        _set_dvr_offsets(pre_min=5, post_min=5)
        prog = self._create_program(hours_from_now=2)
        result1 = evaluate_series_rules_impl()
        self.assertEqual(result1["scheduled"], 1)

        rec = Recording.objects.first()
        self.assertEqual(rec.start_time, prog.start_time - timedelta(minutes=5))
        self.assertEqual(rec.end_time, prog.end_time + timedelta(minutes=5))

        self._simulate_epg_refresh([self._program_data_for_refresh(prog)])
        result2 = evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 1)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_different_episodes_still_recorded(self, mock_release, mock_lock,
                                               mock_schedule, mock_artwork):
        """Different episodes on the same channel should each get a recording."""
        from apps.channels.tasks import evaluate_series_rules_impl

        self._create_program(hours_from_now=2, sub_title="Episode 1")
        self._create_program(hours_from_now=4, sub_title="Episode 2")
        result = evaluate_series_rules_impl()
        self.assertEqual(result["scheduled"], 2)
        self.assertEqual(Recording.objects.count(), 2)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_new_episode_after_refresh_is_recorded(self, mock_release, mock_lock,
                                                   mock_schedule, mock_artwork):
        """A genuinely new episode appearing after EPG refresh should be recorded."""
        from apps.channels.tasks import evaluate_series_rules_impl

        prog = self._create_program(hours_from_now=2, sub_title="Episode 1")
        evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 1)

        self._simulate_epg_refresh([
            self._program_data_for_refresh(prog),
            {
                "tvg_id": "test.channel.1",
                "start_time": prog.end_time,
                "end_time": prog.end_time + timedelta(hours=1),
                "title": "Test Show",
                "sub_title": "Episode 2",
            },
        ])

        result2 = evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 2)
        self.assertEqual(result2["scheduled"], 1)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_multiple_epg_refreshes_no_duplicates(self, mock_release, mock_lock,
                                                   mock_schedule, mock_artwork):
        """Multiple consecutive EPG refreshes should not accumulate duplicates."""
        from apps.channels.tasks import evaluate_series_rules_impl

        prog = self._create_program(hours_from_now=2)
        evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 1)

        for _ in range(5):
            self._simulate_epg_refresh([self._program_data_for_refresh(prog)])
            evaluate_series_rules_impl()

        self.assertEqual(Recording.objects.count(), 1)


@patch("apps.channels.tasks.prefetch_recording_artwork")
@patch("apps.channels.signals.schedule_recording_task", return_value="mock-task-id")
class ConcurrencyGuardTests(SeriesRuleDedupBaseTestCase):
    """Verify the task lock prevents concurrent evaluation."""

    def test_lock_acquired_and_released(self, mock_schedule, mock_artwork):
        """evaluate_series_rules_impl acquires and releases the task lock."""
        from apps.channels.tasks import evaluate_series_rules_impl

        self._create_program(hours_from_now=2)

        with patch("apps.channels.tasks.acquire_task_lock", return_value=True) as mock_lock, \
             patch("apps.channels.tasks.release_task_lock") as mock_release:
            evaluate_series_rules_impl()
            mock_lock.assert_called_once_with('evaluate_series_rules', 'all')
            mock_release.assert_called_once_with('evaluate_series_rules', 'all')

    def test_skips_when_lock_held(self, mock_schedule, mock_artwork):
        """Returns early with skip reason when lock is already held."""
        from apps.channels.tasks import evaluate_series_rules_impl

        self._create_program(hours_from_now=2)

        with patch("apps.channels.tasks.acquire_task_lock", return_value=False):
            result = evaluate_series_rules_impl()
            self.assertEqual(result["scheduled"], 0)
            self.assertTrue(
                any(d.get("reason") == "concurrent evaluation in progress"
                    for d in result["details"]),
            )
            self.assertEqual(Recording.objects.count(), 0)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_lock_released_on_exception(self, mock_release, mock_lock,
                                        mock_schedule, mock_artwork):
        """Lock is released even if the inner implementation raises."""
        from apps.channels.tasks import evaluate_series_rules_impl

        with patch("apps.channels.tasks._evaluate_series_rules_locked",
                   side_effect=RuntimeError("test error")):
            with self.assertRaises(RuntimeError):
                evaluate_series_rules_impl()
            mock_release.assert_called_once_with('evaluate_series_rules', 'all')


@patch("apps.channels.tasks.prefetch_recording_artwork")
@patch("apps.channels.signals.schedule_recording_task", return_value="mock-task-id")
class SecondaryGuardTests(SeriesRuleDedupBaseTestCase):
    """Verify the secondary DB guard uses stable program attributes."""

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_secondary_guard_catches_duplicate_with_offsets(self, mock_release, mock_lock,
                                                            mock_schedule, mock_artwork):
        """Secondary guard works with stale program IDs and DVR offsets."""
        from apps.channels.tasks import evaluate_series_rules_impl

        _set_dvr_offsets(pre_min=10, post_min=10)
        prog = self._create_program(hours_from_now=2)

        # Pre-existing recording with a stale program ID (from previous EPG refresh)
        Recording.objects.create(
            channel=self.channel,
            start_time=prog.start_time - timedelta(minutes=10),
            end_time=prog.end_time + timedelta(minutes=10),
            custom_properties={
                "program": {
                    "id": 99999,
                    "tvg_id": prog.tvg_id,
                    "title": prog.title,
                    "start_time": prog.start_time.isoformat(),
                    "end_time": prog.end_time.isoformat(),
                }
            },
        )

        result = evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 1)
        self.assertEqual(result["scheduled"], 0)


# ---------------------------------------------------------------------------
# Integration tests: full path from EPG refresh through recording creation
# ---------------------------------------------------------------------------

@patch("apps.channels.tasks.prefetch_recording_artwork")
@patch("apps.channels.signals.schedule_recording_task", return_value="mock-task-id")
class IntegrationEPGRefreshTests(SeriesRuleDedupBaseTestCase):
    """End-to-end tests simulating the EPG refresh → evaluate → record flow.

    These exercise the full signal chain: evaluate_series_rules_impl creates
    a Recording, the post_save signal fires schedule_recording_task, and
    subsequent evaluations (after EPG refresh) must not create duplicates.
    """

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_full_flow_single_episode_no_duplicates(self, mock_release, mock_lock,
                                                     mock_schedule, mock_artwork):
        """Simulate: create rule → evaluate → EPG refresh → re-evaluate.

        The full recording lifecycle must result in exactly 1 recording.
        """
        from apps.channels.tasks import evaluate_series_rules_impl

        # Initial EPG data
        prog = self._create_program(hours_from_now=2, sub_title="Pilot")

        # First evaluation creates the recording
        result1 = evaluate_series_rules_impl()
        self.assertEqual(result1["scheduled"], 1)
        self.assertEqual(Recording.objects.count(), 1)

        # Verify the recording was created with correct program metadata
        rec = Recording.objects.first()
        self.assertEqual(rec.custom_properties["program"]["tvg_id"], "test.channel.1")
        self.assertEqual(rec.custom_properties["program"]["title"], "Test Show")
        self.assertEqual(
            rec.custom_properties["program"]["start_time"],
            prog.start_time.isoformat()
        )

        # Verify the post_save signal scheduled a task
        mock_schedule.assert_called()
        initial_schedule_count = mock_schedule.call_count

        # Simulate EPG refresh (programs get new DB IDs)
        self._simulate_epg_refresh([self._program_data_for_refresh(prog)])

        # Re-evaluate after refresh (this is what EPG refresh triggers)
        result2 = evaluate_series_rules_impl()
        self.assertEqual(result2["scheduled"], 0)
        self.assertEqual(Recording.objects.count(), 1)

        # No additional task scheduling should have occurred
        self.assertEqual(mock_schedule.call_count, initial_schedule_count)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_full_flow_with_offsets_no_duplicates(self, mock_release, mock_lock,
                                                   mock_schedule, mock_artwork):
        """Full flow with DVR offsets: recording times differ from program times."""
        from apps.channels.tasks import evaluate_series_rules_impl

        _set_dvr_offsets(pre_min=5, post_min=10)
        prog = self._create_program(hours_from_now=3, sub_title="Episode 1")

        result1 = evaluate_series_rules_impl()
        self.assertEqual(result1["scheduled"], 1)

        rec = Recording.objects.first()
        # Verify offset-adjusted recording times
        self.assertEqual(rec.start_time, prog.start_time - timedelta(minutes=5))
        self.assertEqual(rec.end_time, prog.end_time + timedelta(minutes=10))
        # Verify original (unadjusted) program times in custom_properties
        self.assertEqual(
            rec.custom_properties["program"]["start_time"],
            prog.start_time.isoformat()
        )
        self.assertEqual(
            rec.custom_properties["program"]["end_time"],
            prog.end_time.isoformat()
        )

        # EPG refresh + re-evaluate
        self._simulate_epg_refresh([self._program_data_for_refresh(prog)])
        result2 = evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 1)
        self.assertEqual(result2["scheduled"], 0)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_full_flow_multiple_episodes_across_refreshes(self, mock_release, mock_lock,
                                                           mock_schedule, mock_artwork):
        """New episodes appear across multiple EPG refreshes; each recorded once."""
        from apps.channels.tasks import evaluate_series_rules_impl

        ep1 = self._create_program(hours_from_now=2, sub_title="Episode 1")
        evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 1)

        # EPG refresh adds episode 2 alongside episode 1
        ep1_data = self._program_data_for_refresh(ep1)
        ep2_start = ep1.end_time
        ep2_data = {
            "tvg_id": "test.channel.1",
            "start_time": ep2_start,
            "end_time": ep2_start + timedelta(hours=1),
            "title": "Test Show",
            "sub_title": "Episode 2",
        }
        self._simulate_epg_refresh([ep1_data, ep2_data])
        evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 2)

        # Another EPG refresh adds episode 3
        ep3_start = ep2_start + timedelta(hours=1)
        ep3_data = {
            "tvg_id": "test.channel.1",
            "start_time": ep3_start,
            "end_time": ep3_start + timedelta(hours=1),
            "title": "Test Show",
            "sub_title": "Episode 3",
        }
        self._simulate_epg_refresh([ep1_data, ep2_data, ep3_data])
        evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 3)

        # Final EPG refresh with no new episodes — count must stay at 3
        self._simulate_epg_refresh([ep1_data, ep2_data, ep3_data])
        evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 3)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_full_flow_multiple_series_rules(self, mock_release, mock_lock,
                                              mock_schedule, mock_artwork):
        """Multiple series rules on different channels, each evaluated correctly."""
        from apps.channels.tasks import evaluate_series_rules_impl

        # Second channel with its own EPG
        epg2 = EPGData.objects.create(
            tvg_id="test.channel.2",
            name="Channel 2 EPG",
            epg_source=self.epg_source,
        )
        channel2 = Channel.objects.create(
            channel_number=2, name="Test Channel 2", epg_data=epg2
        )

        _set_series_rules([
            {"tvg_id": "test.channel.1", "mode": "all", "title": "Show A"},
            {"tvg_id": "test.channel.2", "mode": "all", "title": "Show B"},
        ])

        # Programs on both channels
        start1 = self.now + timedelta(hours=2)
        prog1 = ProgramData.objects.create(
            epg=self.epg, tvg_id="test.channel.1",
            start_time=start1, end_time=start1 + timedelta(hours=1),
            title="Show A", sub_title="Episode 1",
        )
        start2 = self.now + timedelta(hours=3)
        prog2 = ProgramData.objects.create(
            epg=epg2, tvg_id="test.channel.2",
            start_time=start2, end_time=start2 + timedelta(hours=1),
            title="Show B", sub_title="Episode 1",
        )

        evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 2)
        self.assertEqual(Recording.objects.filter(channel=self.channel).count(), 1)
        self.assertEqual(Recording.objects.filter(channel=channel2).count(), 1)

        # EPG refresh for both channels
        ProgramData.objects.filter(epg=self.epg).delete()
        ProgramData.objects.filter(epg=epg2).delete()
        ProgramData.objects.create(
            epg=self.epg, tvg_id="test.channel.1",
            start_time=start1, end_time=start1 + timedelta(hours=1),
            title="Show A", sub_title="Episode 1",
        )
        ProgramData.objects.create(
            epg=epg2, tvg_id="test.channel.2",
            start_time=start2, end_time=start2 + timedelta(hours=1),
            title="Show B", sub_title="Episode 1",
        )

        evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 2,
                         "No duplicates across multiple series rules after EPG refresh")

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_full_flow_rapid_epg_refreshes_simulate_user_report(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        """Reproduce the user-reported scenario: series rule + multiple EPG refreshes
        causing count to balloon from 6 to 25 and 5 simultaneous recordings.

        Simulates 6 episodes with 5 EPG refreshes (each assigning new ProgramData IDs).
        """
        from apps.channels.tasks import evaluate_series_rules_impl

        # Create 6 episodes (the user had "next of 6")
        episodes = []
        for i in range(6):
            start = self.now + timedelta(hours=2 + i * 2)
            episodes.append({
                "tvg_id": "test.channel.1",
                "start_time": start,
                "end_time": start + timedelta(hours=1),
                "title": "Test Show",
                "sub_title": f"Episode {i + 1}",
            })

        # Create initial ProgramData
        for ep in episodes:
            ProgramData.objects.create(epg=self.epg, **ep)

        # First evaluation: should create exactly 6 recordings
        evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 6)

        # Simulate 5 EPG refreshes (the user saw count balloon to 25)
        for refresh_num in range(5):
            self._simulate_epg_refresh(episodes)
            result = evaluate_series_rules_impl()
            self.assertEqual(
                Recording.objects.count(), 6,
                f"After EPG refresh #{refresh_num + 1}, expected 6 recordings "
                f"but got {Recording.objects.count()}"
            )
            self.assertEqual(result["scheduled"], 0)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_full_flow_recording_survives_program_removal_and_readd(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        """Program temporarily disappears from EPG then reappears — no duplicate."""
        from apps.channels.tasks import evaluate_series_rules_impl

        prog = self._create_program(hours_from_now=2, sub_title="Episode 1")
        evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 1)

        # EPG refresh removes the program entirely
        self._simulate_epg_refresh([])
        evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 1,
                         "Existing recording preserved when program disappears from EPG")

        # EPG refresh adds the program back (new ID)
        self._simulate_epg_refresh([self._program_data_for_refresh(prog)])
        evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 1,
                         "No duplicate when program reappears with new ID")

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_full_flow_celery_task_wrapper_calls_impl(self, mock_release, mock_lock,
                                                       mock_schedule, mock_artwork):
        """The @shared_task evaluate_series_rules delegates to _impl correctly."""
        from apps.channels.tasks import evaluate_series_rules

        self._create_program(hours_from_now=2)
        result = evaluate_series_rules()
        self.assertEqual(result["scheduled"], 1)
        self.assertEqual(Recording.objects.count(), 1)

        # Call again (simulating a second EPG refresh trigger)
        result2 = evaluate_series_rules()
        self.assertEqual(result2["scheduled"], 0)
        self.assertEqual(Recording.objects.count(), 1)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_full_flow_tvg_id_scoped_evaluation(self, mock_release, mock_lock,
                                                 mock_schedule, mock_artwork):
        """Scoped evaluation (tvg_id parameter) still prevents duplicates."""
        from apps.channels.tasks import evaluate_series_rules_impl

        prog = self._create_program(hours_from_now=2)
        result1 = evaluate_series_rules_impl(tvg_id="test.channel.1")
        self.assertEqual(result1["scheduled"], 1)

        self._simulate_epg_refresh([self._program_data_for_refresh(prog)])
        result2 = evaluate_series_rules_impl(tvg_id="test.channel.1")
        self.assertEqual(result2["scheduled"], 0)
        self.assertEqual(Recording.objects.count(), 1)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_full_flow_offset_change_between_refreshes(self, mock_release, mock_lock,
                                                        mock_schedule, mock_artwork):
        """Changing DVR offsets between EPG refreshes doesn't create duplicates.

        Even though Recording.start_time/end_time change when offsets change,
        the dedup key uses the original program times from custom_properties.
        """
        from apps.channels.tasks import evaluate_series_rules_impl

        _set_dvr_offsets(pre_min=5, post_min=5)
        prog = self._create_program(hours_from_now=2)
        evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 1)

        rec = Recording.objects.first()
        original_start = rec.start_time
        original_end = rec.end_time

        # Change offsets
        _set_dvr_offsets(pre_min=10, post_min=15)

        # EPG refresh
        self._simulate_epg_refresh([self._program_data_for_refresh(prog)])
        result = evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 1,
                         "Changing offsets between refreshes should not create duplicates")
        self.assertEqual(result["scheduled"], 0)


# ---------------------------------------------------------------------------
# Edge case tests: Redis unavailability, non-series recordings, robustness
# ---------------------------------------------------------------------------

@patch("apps.channels.tasks.prefetch_recording_artwork")
@patch("apps.channels.signals.schedule_recording_task", return_value="mock-task-id")
class RedisUnavailabilityTests(SeriesRuleDedupBaseTestCase):
    """Verify evaluation works when Redis is unavailable (lock cannot be acquired)."""

    def test_proceeds_when_redis_down(self, mock_schedule, mock_artwork):
        """Evaluation succeeds (with dedup guards) when Redis raises on lock acquire."""
        from apps.channels.tasks import evaluate_series_rules_impl

        self._create_program(hours_from_now=2)

        with patch("apps.channels.tasks.acquire_task_lock",
                   side_effect=ConnectionError("Redis unavailable")):
            result = evaluate_series_rules_impl()
            self.assertEqual(result["scheduled"], 1)
            self.assertEqual(Recording.objects.count(), 1)

    def test_dedup_still_works_without_lock(self, mock_schedule, mock_artwork):
        """Dedup guards prevent duplicates even when the lock is unavailable."""
        from apps.channels.tasks import evaluate_series_rules_impl

        prog = self._create_program(hours_from_now=2)

        # First call: Redis down, proceeds without lock
        with patch("apps.channels.tasks.acquire_task_lock",
                   side_effect=ConnectionError("Redis unavailable")):
            evaluate_series_rules_impl()
            self.assertEqual(Recording.objects.count(), 1)

        # EPG refresh
        self._simulate_epg_refresh([self._program_data_for_refresh(prog)])

        # Second call: Redis still down
        with patch("apps.channels.tasks.acquire_task_lock",
                   side_effect=ConnectionError("Redis unavailable")):
            result = evaluate_series_rules_impl()
            self.assertEqual(Recording.objects.count(), 1,
                             "Dedup guards prevent duplicates even without lock")
            self.assertEqual(result["scheduled"], 0)

    def test_lock_not_released_when_not_acquired(self, mock_schedule, mock_artwork):
        """release_task_lock is not called if acquire raised an exception."""
        from apps.channels.tasks import evaluate_series_rules_impl

        self._create_program(hours_from_now=2)

        with patch("apps.channels.tasks.acquire_task_lock",
                   side_effect=ConnectionError("Redis unavailable")), \
             patch("apps.channels.tasks.release_task_lock") as mock_release:
            evaluate_series_rules_impl()
            mock_release.assert_not_called()


@patch("apps.channels.tasks.prefetch_recording_artwork")
@patch("apps.channels.signals.schedule_recording_task", return_value="mock-task-id")
class NonSeriesRecordingTests(SeriesRuleDedupBaseTestCase):
    """Verify non-series recordings don't interfere with series rule dedup."""

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_manual_recording_without_program_data_ignored(self, mock_release, mock_lock,
                                                            mock_schedule, mock_artwork):
        """Recordings without custom_properties.program are skipped by dedup key builder."""
        from apps.channels.tasks import evaluate_series_rules_impl

        # Manual recording with no program metadata
        Recording.objects.create(
            channel=self.channel,
            start_time=self.now + timedelta(hours=2),
            end_time=self.now + timedelta(hours=3),
            custom_properties={},
        )

        prog = self._create_program(hours_from_now=2)
        result = evaluate_series_rules_impl()
        self.assertEqual(result["scheduled"], 1)
        self.assertEqual(Recording.objects.count(), 2)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_recurring_rule_recording_does_not_interfere(self, mock_release, mock_lock,
                                                          mock_schedule, mock_artwork):
        """Recordings from recurring rules (custom_properties.rule) don't block series rules."""
        from apps.channels.tasks import evaluate_series_rules_impl

        Recording.objects.create(
            channel=self.channel,
            start_time=self.now + timedelta(hours=2),
            end_time=self.now + timedelta(hours=3),
            custom_properties={"rule": {"id": 1, "name": "Daily News"}},
        )

        prog = self._create_program(hours_from_now=2)
        result = evaluate_series_rules_impl()
        self.assertEqual(result["scheduled"], 1)
        self.assertEqual(Recording.objects.count(), 2)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_recording_with_null_custom_properties_ignored(self, mock_release, mock_lock,
                                                            mock_schedule, mock_artwork):
        """Recordings with None custom_properties don't crash the dedup key builder."""
        from apps.channels.tasks import evaluate_series_rules_impl

        Recording.objects.create(
            channel=self.channel,
            start_time=self.now + timedelta(hours=2),
            end_time=self.now + timedelta(hours=3),
            custom_properties=None,
        )

        prog = self._create_program(hours_from_now=2)
        result = evaluate_series_rules_impl()
        self.assertEqual(result["scheduled"], 1)


# ---------------------------------------------------------------------------
# Title-only rule tests: tvg_id is empty / omitted (cross-EPG matching)
# ---------------------------------------------------------------------------

@patch("apps.channels.tasks.prefetch_recording_artwork")
@patch("apps.channels.signals.schedule_recording_task", return_value="mock-task-id")
class TitleOnlyRuleTests(SeriesRuleDedupBaseTestCase):
    """Tests for series rules where tvg_id is omitted (searches all EPG channels)."""

    def setUp(self):
        super().setUp()
        _set_series_rules([{
            "tvg_id": "",
            "mode": "all",
            "title": "Test Show",
        }])

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_title_only_rule_schedules_recording(self, mock_release, mock_lock,
                                                  mock_schedule, mock_artwork):
        """A rule with no tvg_id matches programs on any EPG channel by title."""
        from apps.channels.tasks import evaluate_series_rules_impl

        self._create_program(hours_from_now=2)
        result = evaluate_series_rules_impl()
        self.assertEqual(result["scheduled"], 1)
        self.assertEqual(Recording.objects.count(), 1)
        self.assertEqual(Recording.objects.first().channel, self.channel)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_title_only_rule_matches_across_multiple_epg_channels(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        """Title-only rule creates a recording per matching EPG channel (distinct programs)."""
        from apps.channels.tasks import evaluate_series_rules_impl

        epg2 = EPGData.objects.create(
            tvg_id="test.channel.2", name="Channel 2 EPG",
            epg_source=self.epg_source,
        )
        Channel.objects.create(
            channel_number=2, name="Test Channel 2", epg_data=epg2
        )

        start1 = self.now + timedelta(hours=2)
        ProgramData.objects.create(
            epg=self.epg, tvg_id="test.channel.1",
            start_time=start1, end_time=start1 + timedelta(hours=1),
            title="Test Show", sub_title="Episode 1",
        )
        start2 = self.now + timedelta(hours=3)
        ProgramData.objects.create(
            epg=epg2, tvg_id="test.channel.2",
            start_time=start2, end_time=start2 + timedelta(hours=1),
            title="Test Show", sub_title="Episode 2",
        )

        result = evaluate_series_rules_impl()
        self.assertEqual(result["scheduled"], 2)
        self.assertEqual(Recording.objects.count(), 2)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_title_only_rule_dedup_after_epg_refresh(self, mock_release, mock_lock,
                                                      mock_schedule, mock_artwork):
        """Dedup works for title-only rules after EPG refresh reassigns program IDs."""
        from apps.channels.tasks import evaluate_series_rules_impl

        prog = self._create_program(hours_from_now=2)
        evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 1)

        self._simulate_epg_refresh([self._program_data_for_refresh(prog)])
        result = evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 1)
        self.assertEqual(result["scheduled"], 0)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_invalid_rule_no_title_no_description_skipped(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        """Rules with neither title nor description are skipped and flagged invalid."""
        from apps.channels.tasks import evaluate_series_rules_impl

        _set_series_rules([{"tvg_id": "", "mode": "all", "title": "", "description": ""}])
        self._create_program(hours_from_now=2)
        result = evaluate_series_rules_impl()
        self.assertEqual(result["scheduled"], 0)
        statuses = [d.get("status") for d in result["details"]]
        self.assertIn("invalid_rule", statuses)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_program_on_epg_with_no_channel_skipped(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        """Programs on an EPG source that has no Channel assigned are skipped gracefully."""
        from apps.channels.tasks import evaluate_series_rules_impl

        epg_orphan = EPGData.objects.create(
            tvg_id="orphan.channel", name="Orphan EPG",
            epg_source=self.epg_source,
        )
        start = self.now + timedelta(hours=2)
        ProgramData.objects.create(
            epg=epg_orphan, tvg_id="orphan.channel",
            start_time=start, end_time=start + timedelta(hours=1),
            title="Test Show", sub_title="Episode 1",
        )

        result = evaluate_series_rules_impl()
        self.assertEqual(result["scheduled"], 0)
        self.assertEqual(Recording.objects.count(), 0)


# ---------------------------------------------------------------------------
# Series rule EPG time offset: real-airtime filtering, dedup and storage
# ---------------------------------------------------------------------------

@patch("apps.channels.tasks.prefetch_recording_artwork")
@patch("apps.channels.signals.schedule_recording_task", return_value="mock-task-id")
class SeriesRuleOffsetTests(SeriesRuleDedupBaseTestCase):
    """Series rule evaluation honours Channel.epg_time_offset_minutes.

    A positive offset O means the channel airs the source programme later:
    real_start = source_start + O, real_end = source_end + O.  All fixtures
    are anchored to a frozen clock so now/horizon boundaries are exact.
    """

    def setUp(self):
        super().setUp()
        self.frozen_now = timezone.now().replace(microsecond=0)
        self.horizon = self.frozen_now + timedelta(days=7)
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

    def _prog(self, start, end, title="Test Show", sub_title="Episode 1",
              tvg_id="test.channel.1", epg=None):
        return ProgramData.objects.create(
            epg=epg or self.epg, tvg_id=tvg_id, start_time=start, end_time=end,
            title=title, sub_title=sub_title,
        )

    def _eval(self):
        from apps.channels.tasks import evaluate_series_rules_impl
        with patch("django.utils.timezone.now", return_value=self.frozen_now):
            return evaluate_series_rules_impl()

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_positive_offset_program_ended_recently_is_scheduled(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        """O=+120: a source programme that ended 60 minutes ago has its real
        end 60 minutes in the future — it must be scheduled, not dropped by
        a raw end_time > now fetch/filter."""
        self._set_channel_offset(120)
        self._prog(
            start=self.frozen_now - timedelta(minutes=180),
            end=self.frozen_now - timedelta(minutes=60),
        )

        result = self._eval()
        self.assertEqual(result["scheduled"], 1)
        rec = Recording.objects.get()
        self.assertEqual(rec.start_time, self.frozen_now - timedelta(minutes=60))
        self.assertEqual(rec.end_time, self.frozen_now + timedelta(minutes=60))

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_negative_offset_program_past_raw_horizon_is_scheduled(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        """O=-120: a source programme starting 60 minutes after the raw
        horizon has its real start 60 minutes before it — it must be
        scheduled, not dropped by a raw start_time <= horizon filter."""
        self._set_channel_offset(-120)
        self._prog(
            start=self.horizon + timedelta(minutes=60),
            end=self.horizon + timedelta(minutes=120),
        )

        result = self._eval()
        self.assertEqual(result["scheduled"], 1)
        rec = Recording.objects.get()
        self.assertEqual(rec.start_time, self.horizon - timedelta(minutes=60))
        self.assertEqual(rec.end_time, self.horizon)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_shifted_real_end_at_now_excluded(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        """A programme whose shifted real end is exactly at now is over:
        the filter is strictly real_end > now."""
        self._set_channel_offset(120)
        self._prog(
            start=self.frozen_now - timedelta(minutes=300),
            end=self.frozen_now - timedelta(minutes=120),  # real end == now
        )

        result = self._eval()
        self.assertEqual(result["scheduled"], 0)
        self.assertEqual(Recording.objects.count(), 0)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_shifted_real_end_before_now_excluded(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        """A programme whose shifted real end is before now is excluded."""
        self._set_channel_offset(120)
        self._prog(
            start=self.frozen_now - timedelta(minutes=301),
            end=self.frozen_now - timedelta(minutes=121),  # real end < now
        )

        result = self._eval()
        self.assertEqual(result["scheduled"], 0)
        self.assertEqual(Recording.objects.count(), 0)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_shifted_real_start_after_horizon_excluded(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        """O=+120: a programme whose source start is inside the widened fetch
        window but whose shifted real start is after the horizon is excluded
        by the exact filter (real_start <= horizon)."""
        self._set_channel_offset(120)
        self._prog(
            start=self.horizon - timedelta(minutes=119),  # real start == horizon + 1
            end=self.horizon - timedelta(minutes=59),     # real end == horizon + 61
        )

        result = self._eval()
        self.assertEqual(result["scheduled"], 0)
        self.assertEqual(Recording.objects.count(), 0)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_shifted_real_start_at_horizon_included(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        """The horizon filter is inclusive: real_start <= horizon."""
        self._set_channel_offset(-120)
        self._prog(
            start=self.horizon + timedelta(minutes=120),  # real start == horizon
            end=self.horizon + timedelta(minutes=180),
        )

        result = self._eval()
        self.assertEqual(result["scheduled"], 1)
        rec = Recording.objects.get()
        self.assertEqual(rec.start_time, self.horizon)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_null_offset_preserves_unshifted_behaviour(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        """Offset null: programs are scheduled and stored at raw source times."""
        self.assertIsNone(self.channel.epg_time_offset_minutes)
        self._prog(
            start=self.frozen_now + timedelta(hours=2),
            end=self.frozen_now + timedelta(hours=3),
        )

        result = self._eval()
        self.assertEqual(result["scheduled"], 1)
        rec = Recording.objects.get()
        self.assertEqual(rec.start_time, self.frozen_now + timedelta(hours=2))
        self.assertEqual(rec.end_time, self.frozen_now + timedelta(hours=3))
        prog = rec.custom_properties["program"]
        self.assertEqual(prog["start_time"], (self.frozen_now + timedelta(hours=2)).isoformat())
        self.assertEqual(prog["end_time"], (self.frozen_now + timedelta(hours=3)).isoformat())

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_zero_offset_preserves_unshifted_behaviour(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        """Offset 0: identical to no offset."""
        self._set_channel_offset(0)
        self._prog(
            start=self.frozen_now + timedelta(hours=2),
            end=self.frozen_now + timedelta(hours=3),
        )

        result = self._eval()
        self.assertEqual(result["scheduled"], 1)
        rec = Recording.objects.get()
        self.assertEqual(rec.start_time, self.frozen_now + timedelta(hours=2))
        self.assertEqual(rec.end_time, self.frozen_now + timedelta(hours=3))

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_program_past_horizon_excluded_with_null_offset(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        """Negative case without offset: a programme beyond the horizon is
        excluded (guards against the widened fetch over-scheduling)."""
        self._prog(
            start=self.horizon + timedelta(minutes=1),
            end=self.horizon + timedelta(hours=2),
        )

        result = self._eval()
        self.assertEqual(result["scheduled"], 0)
        self.assertEqual(Recording.objects.count(), 0)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_negative_offset_upcoming_program_scheduled_at_real_start(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        """O=-120 (channel airs earlier): a future source programme is
        recorded at its shifted real start."""
        self._set_channel_offset(-120)
        source_start = self.frozen_now + timedelta(hours=4)
        self._prog(start=source_start, end=source_start + timedelta(hours=1))

        result = self._eval()
        self.assertEqual(result["scheduled"], 1)
        rec = Recording.objects.get()
        self.assertEqual(rec.start_time, source_start - timedelta(minutes=120))
        self.assertEqual(rec.end_time, source_start + timedelta(hours=1) - timedelta(minutes=120))

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_stored_program_times_are_real_airtimes(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        """custom_properties.program stores shifted real airtimes while
        preserving the original source programme id and metadata."""
        self._set_channel_offset(120)
        source_start = self.frozen_now + timedelta(hours=2)
        source_end = source_start + timedelta(hours=1)
        prog = self._prog(
            start=source_start, end=source_end,
            title="Test Show", sub_title="Episode 7",
        )

        self._eval()
        rec = Recording.objects.get()
        self.assertEqual(rec.start_time, source_start + timedelta(minutes=120))
        self.assertEqual(rec.end_time, source_end + timedelta(minutes=120))

        p = rec.custom_properties["program"]
        self.assertEqual(p["id"], prog.id)
        self.assertEqual(p["tvg_id"], "test.channel.1")
        self.assertEqual(p["title"], "Test Show")
        self.assertEqual(p["sub_title"], "Episode 7")
        self.assertEqual(p["start_time"], (source_start + timedelta(minutes=120)).isoformat())
        self.assertEqual(p["end_time"], (source_end + timedelta(minutes=120)).isoformat())

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_padding_applied_to_real_airtimes(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        """DVR pre/post padding wraps the real airtimes, not the source ones:
        adj_start = source_start + O - pre, adj_end = source_end + O + post."""
        self._set_channel_offset(120)
        _set_dvr_offsets(pre_min=5, post_min=10)
        source_start = self.frozen_now + timedelta(hours=2)
        source_end = source_start + timedelta(hours=1)
        self._prog(start=source_start, end=source_end)

        result = self._eval()
        self.assertEqual(result["scheduled"], 1)
        rec = Recording.objects.get()
        real_start = source_start + timedelta(minutes=120)
        real_end = source_end + timedelta(minutes=120)
        self.assertEqual(rec.start_time, real_start - timedelta(minutes=5))
        self.assertEqual(rec.end_time, real_end + timedelta(minutes=10))

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_shifted_dedup_against_guide_created_recording(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        """A recording created from the shifted guide stores real airtimes in
        custom_properties.program; re-evaluating the series rule must not
        create a duplicate for the same airing."""
        self._set_channel_offset(120)
        source_start = self.frozen_now + timedelta(hours=2)
        source_end = source_start + timedelta(hours=1)
        self._prog(start=source_start, end=source_end)

        real_start = source_start + timedelta(minutes=120)
        real_end = source_end + timedelta(minutes=120)
        Recording.objects.create(
            channel=self.channel,
            start_time=real_start,
            end_time=real_end,
            custom_properties={
                "program": {
                    "id": 999999,  # guide program id; may differ from current
                    "tvg_id": "test.channel.1",
                    "title": "Test Show",
                    "sub_title": "Episode 1",
                    "start_time": real_start.isoformat(),
                    "end_time": real_end.isoformat(),
                }
            },
        )

        result = self._eval()
        self.assertEqual(result["scheduled"], 0)
        self.assertEqual(Recording.objects.count(), 1)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_dedup_after_epg_refresh_with_offset(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        """Dedup still works across EPG refreshes when the channel offset is
        set (real airtimes are stable, ProgramData.id is not)."""
        self._set_channel_offset(120)
        source_start = self.frozen_now + timedelta(hours=2)
        self._prog(start=source_start, end=source_start + timedelta(hours=1))

        from apps.channels.tasks import evaluate_series_rules_impl
        with patch("django.utils.timezone.now", return_value=self.frozen_now):
            evaluate_series_rules_impl()
        self.assertEqual(Recording.objects.count(), 1)

        # EPG refresh: same source airtimes, new program IDs.
        ProgramData.objects.filter(epg=self.epg).delete()
        self._prog(start=source_start, end=source_start + timedelta(hours=1))

        result = self._eval()
        self.assertEqual(result["scheduled"], 0)
        self.assertEqual(Recording.objects.count(), 1)

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_earliest_airing_uses_shifted_real_start_times(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        """Same episode airing on two channels with different offsets: the
        earliest airing is decided by shifted real start times, not raw
        source start times."""
        _set_series_rules([{
            "tvg_id": "",
            "mode": "all",
            "title": "Shifted Show",
        }])
        self._set_channel_offset(120)  # base channel chA (epg 1) airs late
        epg2 = EPGData.objects.create(
            tvg_id="series.show", name="Series EPG 2", epg_source=self.epg_source,
        )
        chB = Channel.objects.create(
            channel_number=2, name="Test Channel 2",
            epg_data=epg2, epg_time_offset_minutes=0,
        )

        # chA airs the episode 2h early in source time (+120 -> real 4h),
        # chB at source 3h (== real 3h).  Raw-earliest is chA; the real
        # earliest is chB, so chB must be recorded.
        self._prog(
            start=self.frozen_now + timedelta(hours=2),
            end=self.frozen_now + timedelta(hours=3),
            title="Shifted Show", sub_title="Episode 1", tvg_id="series.show",
        )
        self._prog(
            start=self.frozen_now + timedelta(hours=3),
            end=self.frozen_now + timedelta(hours=4),
            title="Shifted Show", sub_title="Episode 1", tvg_id="series.show",
            epg=epg2,
        )

        result = self._eval()
        self.assertEqual(result["scheduled"], 1)
        rec = Recording.objects.get()
        self.assertEqual(rec.channel, chB)
        self.assertEqual(rec.start_time, self.frozen_now + timedelta(hours=3))
        self.assertEqual(rec.end_time, self.frozen_now + timedelta(hours=4))


# ---------------------------------------------------------------------------
# Offset-change rescheduling must not double-apply the channel EPG offset
# ---------------------------------------------------------------------------

@patch("apps.channels.tasks.prefetch_recording_artwork")
@patch("apps.channels.signals.schedule_recording_task", return_value="mock-task-id")
class OffsetRescheduleRegressionTests(SeriesRuleDedupBaseTestCase):
    """reschedule_upcoming_recordings_for_offset_change derives the schedule
    from the source ProgramData row plus the channel's current EPG offset
    and applies only the DVR pre/post padding — the channel EPG offset must
    not be applied a second time."""

    def setUp(self):
        super().setUp()
        self._reschedule_patcher = patch(
            "apps.channels.tasks.reschedule_upcoming_recordings_for_offset_change"
        )
        self._reschedule_patcher.start()

    def tearDown(self):
        self._reschedule_patcher.stop()
        super().tearDown()

    @patch("apps.channels.tasks.acquire_task_lock", return_value=True)
    @patch("apps.channels.tasks.release_task_lock")
    def test_reschedule_after_dvr_offset_change_applies_channel_offset_once(
        self, mock_release, mock_lock, mock_schedule, mock_artwork
    ):
        from apps.channels.tasks import (
            evaluate_series_rules_impl,
            reschedule_upcoming_recordings_for_offset_change_impl,
        )

        self.channel.epg_time_offset_minutes = 120
        self.channel.save()
        _set_dvr_offsets(pre_min=5, post_min=5)

        source_start = self.now + timedelta(hours=2)
        source_end = source_start + timedelta(hours=1)
        ProgramData.objects.create(
            epg=self.epg, tvg_id="test.channel.1",
            start_time=source_start, end_time=source_end,
            title="Test Show", sub_title="Episode 1",
        )
        evaluate_series_rules_impl()
        rec = Recording.objects.get()
        real_start = source_start + timedelta(minutes=120)
        real_end = source_end + timedelta(minutes=120)
        self.assertEqual(rec.start_time, real_start - timedelta(minutes=5))
        self.assertEqual(rec.end_time, real_end + timedelta(minutes=5))

        # Change the DVR pre/post offsets and run the reschedule path.
        _set_dvr_offsets(pre_min=10, post_min=15)
        rescheduled = reschedule_upcoming_recordings_for_offset_change_impl()
        rec.refresh_from_db()
        # Offset applied exactly once: real airtimes + new padding only.
        self.assertEqual(rec.start_time, real_start - timedelta(minutes=10))
        self.assertEqual(rec.end_time, real_end + timedelta(minutes=15))
        self.assertEqual(Recording.objects.count(), 1)
        self.assertGreaterEqual(rescheduled["changed"], 1)

        # A second reschedule run is a no-op (no drift / double application).
        reschedule_upcoming_recordings_for_offset_change_impl()
        rec.refresh_from_db()
        self.assertEqual(rec.start_time, real_start - timedelta(minutes=10))
        self.assertEqual(rec.end_time, real_end + timedelta(minutes=15))


# ---------------------------------------------------------------------------
# Channel EPG offset changes: reschedule from the source programme, never
# from the already-shifted stored times
# ---------------------------------------------------------------------------

@patch("apps.channels.tasks.prefetch_recording_artwork")
@patch("apps.channels.signals.schedule_recording_task", return_value="mock-task-id")
@patch("apps.channels.tasks.reschedule_upcoming_recordings_for_offset_change")
class OffsetChangeRescheduleTests(SeriesRuleDedupBaseTestCase):
    """When Channel.epg_time_offset_minutes changes, future not-yet-started
    recordings must move to the source programme's new real airtime:
    new_real = source + current offset, with DVR padding applied after.
    The stored (already shifted) times must never be used as the shift base.
    """

    def _make_source_and_rec(self, source_start, source_end, offset, status=""):
        """Create a source programme plus an EPG-based recording exactly as
        the series-rule engine (or the guide) stores it under `offset`:
        recording at the real airtimes, program dict carrying the source id
        and the real (shifted) airtimes."""
        prog = ProgramData.objects.create(
            epg=self.epg, tvg_id="test.channel.1",
            start_time=source_start, end_time=source_end,
            title="Test Show", sub_title="Episode 1",
        )
        real_start = source_start + timedelta(minutes=offset)
        real_end = source_end + timedelta(minutes=offset)
        cp = {
            "program": {
                "id": prog.id,
                "tvg_id": "test.channel.1",
                "title": "Test Show",
                "sub_title": "Episode 1",
                "start_time": real_start.isoformat(),
                "end_time": real_end.isoformat(),
            }
        }
        if status:
            cp["status"] = status
        rec = Recording.objects.create(
            channel=self.channel,
            start_time=real_start,
            end_time=real_end,
            custom_properties=cp,
        )
        return prog, rec

    def _set_channel_offset(self, minutes):
        self.channel.epg_time_offset_minutes = minutes
        self.channel.save()

    def _reschedule(self):
        from apps.channels.tasks import (
            reschedule_upcoming_recordings_for_offset_change_impl,
        )
        return reschedule_upcoming_recordings_for_offset_change_impl()

    def test_offset_increase_moves_recording_by_delta_only(
        self, mock_reschedule, mock_schedule, mock_artwork
    ):
        """+120 -> +180 moves the recording by exactly +60 minutes, to
        source + 180."""
        source_start = self.now + timedelta(hours=2)
        source_end = source_start + timedelta(hours=1)
        prog, rec = self._make_source_and_rec(source_start, source_end, 120)
        old_start, old_end = rec.start_time, rec.end_time

        self._set_channel_offset(180)
        result = self._reschedule()
        rec.refresh_from_db()

        new_real_start = source_start + timedelta(minutes=180)
        new_real_end = source_end + timedelta(minutes=180)
        self.assertEqual(result["changed"], 1)
        self.assertEqual(rec.start_time, new_real_start)
        self.assertEqual(rec.end_time, new_real_end)
        self.assertEqual(rec.start_time, old_start + timedelta(minutes=60))
        self.assertEqual(rec.end_time, old_end + timedelta(minutes=60))
        # Stored program carries the new real airtimes, original id kept.
        p = rec.custom_properties["program"]
        self.assertEqual(p["id"], prog.id)
        self.assertEqual(p["tvg_id"], "test.channel.1")
        self.assertEqual(p["title"], "Test Show")
        self.assertEqual(p["sub_title"], "Episode 1")
        self.assertEqual(p["start_time"], new_real_start.isoformat())
        self.assertEqual(p["end_time"], new_real_end.isoformat())

    def test_offset_to_zero_and_null_restores_source_airtime(
        self, mock_reschedule, mock_schedule, mock_artwork
    ):
        """+120 -> 0 and +120 -> None both restore the source airtime."""
        for target in (0, None):
            with self.subTest(target=target):
                source_start = self.now + timedelta(hours=2)
                source_end = source_start + timedelta(hours=1)
                prog, rec = self._make_source_and_rec(source_start, source_end, 120)

                self._set_channel_offset(target)
                result = self._reschedule()
                rec.refresh_from_db()

                self.assertEqual(result["changed"], 1)
                self.assertEqual(rec.start_time, source_start)
                self.assertEqual(rec.end_time, source_end)
                p = rec.custom_properties["program"]
                self.assertEqual(p["start_time"], source_start.isoformat())
                self.assertEqual(p["end_time"], source_end.isoformat())

    def test_negative_to_positive_offset_calculates_from_source_time(
        self, mock_reschedule, mock_schedule, mock_artwork
    ):
        """-120 -> +180 is computed from the source time; adding the new
        offset to the stored (already shifted) times would give a different,
        wrong result."""
        # Source starts 4h out so the -120 real start stays comfortably in
        # the future (the reschedule only touches recordings whose start is
        # after now).
        source_start = self.now + timedelta(hours=4)
        source_end = source_start + timedelta(hours=1)
        prog, rec = self._make_source_and_rec(source_start, source_end, -120)
        self.assertEqual(rec.start_time, source_start - timedelta(minutes=120))

        self._set_channel_offset(180)
        result = self._reschedule()
        rec.refresh_from_db()

        self.assertEqual(result["changed"], 1)
        self.assertEqual(rec.start_time, source_start + timedelta(minutes=180))
        self.assertEqual(rec.end_time, source_end + timedelta(minutes=180))
        # The double-shifted value (old real start + new offset) must not
        # have been produced.
        self.assertNotEqual(
            rec.start_time, source_start - timedelta(minutes=120) + timedelta(minutes=180)
        )

    def test_repeated_reschedule_is_idempotent(
        self, mock_reschedule, mock_schedule, mock_artwork
    ):
        """Running the reschedule twice with the same current offset is a
        no-op the second time."""
        source_start = self.now + timedelta(hours=2)
        source_end = source_start + timedelta(hours=1)
        prog, rec = self._make_source_and_rec(source_start, source_end, 120)

        self._set_channel_offset(180)
        first = self._reschedule()
        rec.refresh_from_db()
        start_after, end_after = rec.start_time, rec.end_time
        cp_after = dict(rec.custom_properties["program"])
        self.assertEqual(first["changed"], 1)

        second = self._reschedule()
        rec.refresh_from_db()
        self.assertEqual(second["changed"], 0)
        self.assertEqual(rec.start_time, start_after)
        self.assertEqual(rec.end_time, end_after)
        self.assertEqual(rec.custom_properties["program"], cp_after)

    def test_missing_source_program_preserved_and_reported(
        self, mock_reschedule, mock_schedule, mock_artwork
    ):
        """If the source ProgramData row is gone (EPG refresh), the
        recording is preserved untouched and the fallback is reported."""
        source_start = self.now + timedelta(hours=2)
        source_end = source_start + timedelta(hours=1)
        prog, rec = self._make_source_and_rec(source_start, source_end, 120)
        start_before, end_before = rec.start_time, rec.end_time

        prog.delete()  # simulate EPG refresh replacing the row
        self._set_channel_offset(180)
        result = self._reschedule()
        rec.refresh_from_db()

        self.assertEqual(result["missing_programs"], 1)
        self.assertEqual(result["changed"], 0)
        self.assertEqual(rec.start_time, start_before)
        self.assertEqual(rec.end_time, end_before)
        self.assertEqual(
            rec.custom_properties["program"]["start_time"], start_before.isoformat()
        )
        self.assertEqual(
            rec.custom_properties["program"]["end_time"], end_before.isoformat()
        )

    def test_active_and_terminal_recordings_excluded(
        self, mock_reschedule, mock_schedule, mock_artwork
    ):
        """Active (recording) and terminal (completed/interrupted) recordings
        are never modified; only the pending recording moves."""
        source_start = self.now + timedelta(hours=2)
        source_end = source_start + timedelta(hours=1)
        prog, pending = self._make_source_and_rec(source_start, source_end, 120)

        protected = {}
        for i, status in enumerate(("recording", "completed", "interrupted")):
            start = self.now + timedelta(hours=4 + i)
            _, rec = self._make_source_and_rec(
                start, start + timedelta(hours=1), 120, status=status
            )
            protected[status] = (rec, start)

        self._set_channel_offset(180)
        result = self._reschedule()
        pending.refresh_from_db()

        self.assertEqual(result["changed"], 1)
        self.assertEqual(pending.start_time, source_start + timedelta(minutes=180))
        self.assertEqual(pending.end_time, source_end + timedelta(minutes=180))
        for status, (rec, start) in protected.items():
            rec.refresh_from_db()
            with self.subTest(status=status):
                self.assertEqual(rec.start_time, start + timedelta(minutes=120))
                self.assertEqual(
                    rec.end_time, start + timedelta(hours=1) + timedelta(minutes=120)
                )
                self.assertEqual(
                    rec.custom_properties["program"]["start_time"],
                    (start + timedelta(minutes=120)).isoformat(),
                )

    def test_padding_applied_after_new_real_airtime(
        self, mock_reschedule, mock_schedule, mock_artwork
    ):
        """DVR pre/post padding wraps the new real airtimes, and the stored
        program times are the un-padded real airtimes."""
        _set_dvr_offsets(pre_min=5, post_min=10)
        source_start = self.now + timedelta(hours=2)
        source_end = source_start + timedelta(hours=1)
        prog, rec = self._make_source_and_rec(source_start, source_end, 120)

        self._set_channel_offset(180)
        result = self._reschedule()
        rec.refresh_from_db()

        real_start = source_start + timedelta(minutes=180)
        real_end = source_end + timedelta(minutes=180)
        self.assertEqual(result["changed"], 1)
        self.assertEqual(rec.start_time, real_start - timedelta(minutes=5))
        self.assertEqual(rec.end_time, real_end + timedelta(minutes=10))
        p = rec.custom_properties["program"]
        self.assertEqual(p["start_time"], real_start.isoformat())
        self.assertEqual(p["end_time"], real_end.isoformat())

    def test_offset_change_dispatches_reschedule_task(
        self, mock_reschedule, mock_schedule, mock_artwork
    ):
        """Saving a channel with a changed epg_time_offset_minutes dispatches
        the reschedule task (post_save signal, .save() write path)."""
        self._set_channel_offset(120)
        mock_reschedule.delay.assert_called_once()

    def test_offset_save_without_change_does_not_dispatch(
        self, mock_reschedule, mock_schedule, mock_artwork
    ):
        """A channel save that does not change the offset dispatches nothing."""
        self.channel.name = "Renamed Channel"
        self.channel.save()
        mock_reschedule.delay.assert_not_called()

    def test_none_to_zero_toggle_does_not_dispatch(
        self, mock_reschedule, mock_schedule, mock_artwork
    ):
        """None and 0 are equivalent (no shift), so toggling between them
        does not reschedule."""
        self._set_channel_offset(0)
        self._set_channel_offset(None)
        mock_reschedule.delay.assert_not_called()
