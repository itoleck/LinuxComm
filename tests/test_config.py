"""Tests for the settings file.

Run with:  python3 -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linuxcomm.config import Config  # noqa: E402


class ConfigTests(unittest.TestCase):
    def config_with(self, stored: dict) -> Config:
        path = Path(tempfile.mkdtemp()) / "config.json"
        path.write_text(json.dumps(stored), encoding="utf-8")
        return Config(path)

    def test_old_data_folder_default_is_migrated(self):
        # Version 0.0.8 saved "/data" as the default; it now means ~/linuxcomm/data ("").
        self.assertEqual(self.config_with({"transcript_folder": "/data"}).get("transcript_folder"), "")

    def test_a_chosen_folder_is_kept(self):
        self.assertEqual(self.config_with({"transcript_folder": "/media/usb/transcripts"}).get("transcript_folder"),
                         "/media/usb/transcripts")

    def test_defaults_and_unknown_keys(self):
        config = self.config_with({"station_name": "Kitchen", "no_such_setting": 1})
        self.assertEqual(config.station_name, "Kitchen")
        self.assertEqual(config.get("transcript_folder"), "")
        with self.assertRaises(KeyError):
            config.get("no_such_setting")


if __name__ == "__main__":
    unittest.main()
