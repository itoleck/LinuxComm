"""Keeps the version number and the What's New history in step.

Run with:  python3 -m unittest discover -s tests -v
"""

import re
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linuxcomm import __version__, changelog  # noqa: E402


def parse(version):
    return tuple(int(part) for part in version.split("."))


class VersionTests(unittest.TestCase):
    def test_version_format(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")

    def test_newest_release_is_the_current_version(self):
        self.assertEqual(changelog.RELEASES[0][0], __version__,
                         "add an entry for the new version at the top of changelog.RELEASES")

    def test_history_is_newest_first_and_well_formed(self):
        versions = [parse(v) for v, _d, _c in changelog.RELEASES]
        self.assertEqual(versions, sorted(versions, reverse=True))
        self.assertEqual(len(versions), len(set(versions)), "duplicate version")
        for version, day, changes in changelog.RELEASES:
            date.fromisoformat(day)
            self.assertTrue(changes, f"{version} lists no changes")

    def test_release_notes_markup(self):
        notes = changelog.release_notes(__version__)
        self.assertTrue(notes.startswith("<ul><li>") and notes.endswith("</li></ul>"))
        self.assertEqual(changelog.release_notes("9.9.9"), "")
        self.assertNotRegex(re.sub(r"</?(ul|li)>", "", notes), r"[<>]", "change text must be escaped")


if __name__ == "__main__":
    unittest.main()
