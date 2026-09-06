"""Subjects are clipped to the byte limit before transport publication."""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "agent_bus_v3", ROOT / "scripts" / "agent-bus-v3.py")
bus = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bus)

class ClipSubjectBytesTests(unittest.TestCase):
    """The CLI-level clip: bytes, UTF-8 boundary, exactly-at-limit untouched."""

    def test_cjk_exactly_at_limit_is_untouched(self):
        subject = "跨" * 53 + "x"          # 53*3 + 1 = 160 bytes exactly
        self.assertEqual(len(subject.encode()), 160)
        self.assertEqual(bus.clip_subject(subject), subject)

    def test_cjk_over_limit_clips_on_utf8_boundary(self):
        subject = "跨" * 54                # 162 bytes: over at 54 CHARS
        out = bus.clip_subject(subject)
        raw = out.encode("utf-8")
        self.assertLessEqual(len(raw), 160)
        self.assertTrue(out.endswith("..."))
        raw.decode("utf-8")                # boundary-safe: decodes cleanly
        self.assertNotEqual(out, subject)

    def test_dead_letter_instance_shape_now_survives(self):
        subject = "d" * 167                # ASCII boundary case
        out = bus.clip_subject(subject)
        self.assertEqual(len(out.encode()), 160)
        self.assertTrue(out.endswith("..."))

    def test_ascii_under_limit_is_identity(self):
        self.assertEqual(bus.clip_subject("short"), "short")
if __name__ == "__main__":
    unittest.main()
