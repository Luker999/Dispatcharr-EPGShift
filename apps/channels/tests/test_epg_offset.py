from django.test import SimpleTestCase

from apps.channels.epg_offset import (
    EPG_TIME_OFFSET_MAX_MINUTES,
    EPG_TIME_OFFSET_MIN_MINUTES,
    validate_epg_time_offset_minutes,
)


class ValidateEpgTimeOffsetMinutesTests(SimpleTestCase):
    """The shared ±1440 rule applied by every write surface."""

    def test_range_constants(self):
        self.assertEqual(EPG_TIME_OFFSET_MIN_MINUTES, -1440)
        self.assertEqual(EPG_TIME_OFFSET_MAX_MINUTES, 1440)

    def test_none_means_no_shift(self):
        self.assertIsNone(validate_epg_time_offset_minutes(None))

    def test_blank_strings_mean_no_shift(self):
        for value in ("", "   ", "\t"):
            with self.subTest(value=value):
                self.assertIsNone(validate_epg_time_offset_minutes(value))

    def test_accepts_integers_within_range(self):
        for value in (0, 1, -1, 90, -45, 1440, -1440):
            with self.subTest(value=value):
                self.assertEqual(validate_epg_time_offset_minutes(value), value)

    def test_accepts_integer_strings(self):
        self.assertEqual(validate_epg_time_offset_minutes("90"), 90)
        self.assertEqual(validate_epg_time_offset_minutes(" -45 "), -45)
        self.assertEqual(validate_epg_time_offset_minutes("1440"), 1440)
        self.assertEqual(validate_epg_time_offset_minutes("-1440"), -1440)

    def test_rejects_booleans(self):
        for value in (True, False):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_epg_time_offset_minutes(value)

    def test_rejects_floats(self):
        for value in (1.0, 1.5, -1440.0, 1440.0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_epg_time_offset_minutes(value)

    def test_rejects_decimal_and_malformed_strings(self):
        for value in ("90.5", "1,440", "1e3", "0x10", "abc", "9 0", "+1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_epg_time_offset_minutes(value)

    def test_rejects_out_of_range_values(self):
        for value in (1441, -1441, 10**6, -10**6, "1441", "-1441"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_epg_time_offset_minutes(value)

    def test_rejects_unexpected_types(self):
        with self.assertRaises(ValueError):
            validate_epg_time_offset_minutes(object())
