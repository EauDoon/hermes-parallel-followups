"""The optional transcript workflow must not create or alter user databases."""
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


SCANNER = Path(__file__).with_name("test_classifier.py")


def scan(path):
    return subprocess.run([sys.executable, str(SCANNER), "--db", str(path)],
                          capture_output=True, text=True, check=False)


class TranscriptScanTests(unittest.TestCase):
    def test_streamed_counts_preserve_source_and_do_not_print_messages(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "transcript #100%.db"
            with sqlite3.connect(target) as db:
                db.execute("CREATE TABLE messages(role, content)")
                db.executemany("INSERT INTO messages VALUES (?, ?)", [
                    ("user", "What are the principal benefits of solar energy?"),
                    ("user", "also secret-transcript-marker"), ("assistant", "ignore"),
                    ("user", None), ("user", " "), ("user", 42)])
            db.close()
            before = target.read_bytes()
            result = scan(target)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("YOUR TRANSCRIPT: n=2", result.stdout)
            self.assertIn("background (independent): 1 (50.0%)", result.stdout)
            self.assertNotIn("secret-transcript-marker", result.stdout + result.stderr)
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(list(Path(td).iterdir()), [target])

    def test_empty_transcript_reports_zero_queued(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "empty.db"
            with sqlite3.connect(target) as db:
                db.execute("CREATE TABLE messages(role, content)")
            db.close()
            result = scan(target)
            self.assertEqual(result.returncode, 0)
            self.assertIn("stay queued (dependent) : 0 (0.0%)", result.stdout)

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "missing.db"
            self.assertEqual(scan(target).returncode, 2)
            self.assertFalse(target.exists())

    def test_missing_db_argument_has_usage_error_without_traceback(self):
        result = subprocess.run([sys.executable, str(SCANNER), "--db"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
