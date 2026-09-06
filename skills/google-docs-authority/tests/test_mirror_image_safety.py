"""Image preservation, pointer-aware sizing and refused-render rollback."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

from google_docs_authority import mirror as sgd


@pytest.fixture(autouse=True)
def isolated_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(sgd, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(sgd, "ALLOW_IMAGE_SHRINK", False)
    monkeypatch.setattr(sgd, "IMAGE_SHRINK_FLOOR", 0.7)


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + b"IHDR" + b"\x00" * 64
POINTER = (
    b"version https://git-lfs.github.com/spec/v1\noid sha256:"
    + b"b" * 64
    + b"\nsize 987654\n"
)


class PointerAwareness(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)
        (self.dir / "attachments").mkdir()
        self.real = self.dir / "attachments" / "real.png"
        self.ptr = self.dir / "attachments" / "ptr.png"
        self.real.write_bytes(PNG)
        self.ptr.write_bytes(POINTER)

    def tearDown(self):
        self.td.cleanup()

    def test_pointer_detected(self):
        self.assertTrue(sgd.is_lfs_pointer(self.ptr))
        self.assertFalse(sgd.is_lfs_pointer(self.real))

    def test_pointer_is_not_image_bytes(self):
        self.assertFalse(sgd.real_image_bytes(self.ptr))
        self.assertTrue(sgd.real_image_bytes(self.real))

    def test_size_readable_through_pointer(self):
        self.assertEqual(sgd.attachment_bytes(self.ptr), 987654)
        self.assertEqual(sgd.attachment_bytes(self.real), len(PNG))

    def test_extract_rewrites_a_pointer_with_real_bytes(self):
        import base64
        import hashlib

        data = PNG + b"payload"
        sha1 = hashlib.sha1(data).hexdigest()
        slug = "doc--deadbeef"
        adir = sgd.OUTPUT_DIR / slug / "attachments"
        try:
            adir.mkdir(parents=True, exist_ok=True)
            stale = adir / f"{sha1}.png"
            stale.write_bytes(POINTER)
            md = f"![x](data:image/png;base64,{base64.b64encode(data).decode()})"
            sgd.extract_md_images(md, slug)
            self.assertTrue(
                sgd.real_image_bytes(stale),
                "a pointer standing in for an image must be overwritten with the decoded bytes",
            )
            self.assertEqual(stale.read_bytes(), data)
        finally:
            for p in adir.glob("*"):
                p.unlink()
            for d in (adir, adir.parent):
                if d.exists():
                    d.rmdir()


class DowngradeGate(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)
        (self.dir / "attachments").mkdir()

    def tearDown(self):
        self.td.cleanup()

    def _mk(self, n, size, offset=0):
        imgs = [{"sha1": f"{i + offset:040x}", "ext": "png"} for i in range(n)]
        for i in imgs:
            (self.dir / "attachments" / f"{i['sha1']}.png").write_bytes(b"\x00" * size)
        return imgs

    def test_same_count_shrink_is_refused(self):
        big = self._mk(4, 300000)
        small = self._mk(4, 80000, offset=500)
        self.assertIsNotNone(sgd.image_downgrade_verdict(big, small, self.dir))

    def test_growth_is_allowed(self):
        small = self._mk(4, 80000)
        big = self._mk(4, 300000, offset=500)
        self.assertIsNone(sgd.image_downgrade_verdict(small, big, self.dir))

    def test_content_edit_changing_image_count_is_not_a_downgrade(self):
        old = self._mk(10, 300000)
        new = self._mk(4, 300000, offset=500)
        self.assertIsNone(sgd.image_downgrade_verdict(old, new, self.dir))

    def test_gate_measures_through_pointers(self):
        old = [{"sha1": f"{i:040x}", "ext": "png"} for i in range(3)]
        for i in old:
            (self.dir / "attachments" / f"{i['sha1']}.png").write_bytes(POINTER)
        new = self._mk(3, 50000, offset=900)
        self.assertIsNotNone(
            sgd.image_downgrade_verdict(old, new, self.dir),
            "sizes must be read from the pointer, else skip-smudge hides the downgrade",
        )


class RefusalLeavesNoResidue(unittest.TestCase):
    def test_prune_to_previous_set_removes_this_renders_images(self):
        slug = "residue-doc--cafebabe"
        adir = sgd.OUTPUT_DIR / slug / "attachments"
        try:
            adir.mkdir(parents=True, exist_ok=True)
            keep = [{"sha1": f"{i:040x}", "ext": "png"} for i in range(2)]
            for i in keep:
                (adir / f"{i['sha1']}.png").write_bytes(b"old")
            for i in range(3):
                (adir / f"{i + 900:040x}.png").write_bytes(b"new")
            removed = sgd.prune_unreferenced_images(slug, keep)
            self.assertEqual(removed, 3)
            self.assertEqual(
                sorted((p.name for p in adir.glob("*"))),
                sorted((f"{i['sha1']}.png" for i in keep)),
            )
        finally:
            for p in adir.glob("*"):
                p.unlink()
            for d in (adir, adir.parent):
                if d.exists():
                    d.rmdir()

    def test_first_ever_render_rolls_back_to_empty(self):
        slug = "residue-first--cafed00d"
        adir = sgd.OUTPUT_DIR / slug / "attachments"
        try:
            adir.mkdir(parents=True, exist_ok=True)
            for i in range(4):
                (adir / f"{i:040x}.png").write_bytes(b"new")
            self.assertEqual(sgd.prune_unreferenced_images(slug, []), 4)
            self.assertEqual(list(adir.glob("*")), [])
        finally:
            for p in adir.glob("*"):
                p.unlink()
            for d in (adir, adir.parent):
                if d.exists():
                    d.rmdir()


class ChurnGate(unittest.TestCase):
    def test_image_reencode_churn_matches(self):
        old = "# 标题\n\n正文没变。\n\n![](attachments/aaaa.png)\n"
        new = "# 标题\n\n正文没变。\n\n![](attachments/bbbb.png)\n"
        self.assertTrue(sgd.render_matches_previous(old, new))

    def test_html_engine_img_and_style_churn_matches(self):
        old = '<img src="attachments/6bd8.png" style="width:624px">\n\n| where (Message has "Request started")\n'
        new = '<img src="attachments/94f1.png" style="width:311px">\n\n| where (Message has "Request started")\n'
        self.assertTrue(sgd.render_matches_previous(old, new))

    def test_word_change_does_not_match(self):
        self.assertFalse(
            sgd.render_matches_previous("结论:方案 A 更好。\n", "结论:方案 B 更好。\n")
        )

    def test_first_render_never_matches(self):
        self.assertFalse(sgd.render_matches_previous(None, "anything\n"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SelfTestIsWiredIn(unittest.TestCase):
    def test_self_test_flag_passes(self):
        script = Path(__file__).resolve().parents[1] / "scripts/sync"
        result = subprocess.run(
            [str(script), "--self-test"],
            capture_output=True,
            text=True,
            env=dict(os.environ, GOOGLE_DOCS_AUTHORITY_PYTHON=sys.executable),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_disabled_guard_fails_self_test(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from google_docs_authority import mirror;m=mirror;m.image_downgrade_verdict=lambda *args:None;raise SystemExit(m.selftest())",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
