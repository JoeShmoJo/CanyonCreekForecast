"""Offline regression tests for full-year scheduling and resume integrity."""
import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from build_water_year_dataset import scheduled_decisions, checkpoint_valid, sha, exclusion_code


class WaterYearTests(unittest.TestCase):
    def test_schedule_obeys_local_year_boundaries(self):
        times=scheduled_decisions(2025)
        self.assertEqual(len(times),365)
        self.assertEqual(times[0],pd.Timestamp('2024-10-02T06:00Z'))
        self.assertEqual(times[-1],pd.Timestamp('2025-10-01T06:00Z'))
        local=times.tz_convert('America/Los_Angeles')
        self.assertTrue((local>=pd.Timestamp('2024-10-01',tz='America/Los_Angeles')).all())
        self.assertTrue((local<pd.Timestamp('2025-10-01',tz='America/Los_Angeles')).all())
        self.assertTrue((times.hour==6).all())
        self.assertEqual(len(scheduled_decisions(2024)),366)

    def test_resume_rejects_changed_source_policy_or_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'source';path.write_bytes(b'original')
            record={'status':'passed','fingerprint':'policy','provenance':{
                'source_local_path':str(path),'source_key':'key','source_sha256':sha(path)}}
            self.assertTrue(checkpoint_valid(record,'policy','key'))
            self.assertFalse(checkpoint_valid(record,'changed','key'))
            self.assertFalse(checkpoint_valid(record,'policy','other'))
            path.write_bytes(b'changed')
            self.assertFalse(checkpoint_valid(record,'policy','key'))

    def test_unexpected_failures_cannot_be_silently_excluded(self):
        self.assertEqual(exclusion_code(ValueError('Incomplete quarter-hour window')),'incomplete_observations')
        with self.assertRaisesRegex(ValueError,'unexpected'):
            exclusion_code(ValueError('unexpected decoding error'))


if __name__=='__main__':unittest.main()
