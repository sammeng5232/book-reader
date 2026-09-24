"""JNI worker failures must be returned to the Android conversion UI."""
from pathlib import Path
import importlib.util
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class AndroidTypesetterTests(unittest.TestCase):
    def setUp(self):
        path = ROOT / "app/src/main/python/bookreader_android.py"
        spec = importlib.util.spec_from_file_location("android_bridge_test", path)
        self.bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bridge)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pdf = Path(self.temp.name) / "document.pdf"
        self.tex = Path(self.temp.name) / "document.tex"
        self.compiler = SimpleNamespace(CompileResult=lambda **kw: SimpleNamespace(**kw),
                                        ExportCancelled=RuntimeError)

    def compile(self, engine):
        self.bridge._TEX = engine
        with patch.dict(sys.modules, {"latexexport": self.compiler}):
            return self.bridge._EngineTypesetter()(str(self.tex), str(self.pdf))

    def test_success_requires_new_engine_result_and_pdf(self):
        def typeset(*args):
            self.pdf.write_bytes(b"%PDF-1.4\n/Type /Pages /Count 2")
            return ""
        result = self.compile(SimpleNamespace(typeset=typeset))
        self.assertTrue(result.ok)
        self.assertEqual(result.pages, 2)

    def test_engine_error_is_preserved(self):
        result = self.compile(SimpleNamespace(typeset=lambda *args: "missing cmmi12.pfb"))
        self.assertFalse(result.ok)
        self.assertEqual(result.problems, ["missing cmmi12.pfb"])

    def test_jni_exception_cannot_accept_stale_pdf(self):
        self.pdf.write_bytes(b"%PDF-1.4\n/Type /Pages /Count 9")
        def typeset(*args):
            raise RuntimeError("JNI failed to load font")
        result = self.compile(SimpleNamespace(typeset=typeset))
        self.assertFalse(result.ok)
        self.assertIn("JNI failed to load font", result.problems[0])
        self.assertIsNone(result.pdf_path)


if __name__ == "__main__":
    unittest.main()
