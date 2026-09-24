"""Offline font resources must cover both metric maps and TU font declarations."""
from pathlib import Path
import importlib.util
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verify_bundle", ROOT / "native/verify-bundle.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class OfflineBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = Path(self.temp.name)
        (self.bundle / "pdftex.map").write_text("", encoding="utf-8")

    def test_metric_without_its_font_program_is_rejected(self):
        (self.bundle / "pdftex.map").write_text("cmex7 CMEX7 <cmex7.pfb\n")
        (self.bundle / "cmex7.tfm").write_bytes(b"metric")
        self.assertEqual(validator.missing_fonts(self.bundle), [("cmex7.tfm", "cmex7.pfb")])
        (self.bundle / "cmex7.pfb").write_bytes(b"font")
        self.assertEqual(validator.missing_fonts(self.bundle), [])

    def test_optical_size_declaration_requires_its_opentype_font(self):
        (self.bundle / "tulmr.fd").write_text(
            r"\UnicodeFontFile{lmroman7-regular}{\UnicodeFontTeXLigatures}" + "\n" +
            r"\UnicodeFontFile{lmroman10-regular}{\UnicodeFontTeXLigatures}", encoding="utf-8")
        (self.bundle / "lmroman10-regular.otf").write_bytes(b"body font")
        self.assertEqual(validator.missing_fonts(self.bundle), [("tulmr.fd", "lmroman7-regular.otf")])
        (self.bundle / "lmroman7-regular.otf").write_bytes(b"superscript font")
        self.assertEqual(validator.missing_fonts(self.bundle), [])

    def test_packaged_bundle_has_no_missing_font_dependencies(self):
        self.assertEqual(validator.missing_fonts(ROOT / "app/src/main/assets/texbundle"), [])


if __name__ == "__main__":
    unittest.main()
