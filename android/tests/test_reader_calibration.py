"""Read-only mobile calibration: same glyph scale as PDF, cache, sparse icons."""
from pathlib import Path
import importlib.util
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
# For a staged development checkout, the shared modules still live in the repo.
if not (ROOT / 'latexexport.py').exists():
    ROOT = Path(r'C:\Users\mengz\epub-reader')
sys.path.insert(0, str(ROOT))
import store
from epublib import EpubBook
from tests.test_formula_sizing import make_book, png
spec = importlib.util.spec_from_file_location('mobile_reader_bridge',
    Path(__file__).resolve().parents[1] / 'app/src/main/python/reader_android.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

class ReaderCalibration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.cache = patch.object(store, 'cache_dir', return_value=str(self.folder/'cache'))
        self.cache.start()
    def tearDown(self):
        self.cache.stop()
        self.tmp.cleanup()
    def book(self, count, prefix='math-'):
        pictures = {f'{prefix}{i}.png': png(140,35,True) for i in range(count)}
        body = ''.join(f'<p>Formula <img src="{prefix}{i}.png"/> text.</p>' for i in range(count))
        return make_book(self.folder/'sample.epub',body,pictures=pictures)
    def test_formula_scale_preserves_original_book_and_cache(self):
        path = self.book(10)
        before = Path(path).read_bytes()
        with EpubBook.open(path) as book:
            a = bridge.formula_scales(book,str(path))
            self.assertAlmostEqual(a['math-#.png'],.70/20)
            with patch('latexexport._glyph_heights',side_effect=AssertionError('cache miss')):
                self.assertEqual(bridge.formula_scales(book,str(path)),a)
        self.assertEqual(Path(path).read_bytes(),before)
    def test_sparse_unsized_icons_are_not_calibrated(self):
        with EpubBook.open(self.book(5,'icon-')) as book:
            self.assertEqual(bridge.formula_scales(book,str(self.folder/'sample.epub')), {})
    def test_untagged_dense_legacy_formula_series(self):
        with EpubBook.open(self.book(30,'Image')) as book:
            self.assertAlmostEqual(bridge.formula_scales(book,str(self.folder/'sample.epub'))['image#.png'],.70/20)

if __name__ == '__main__':
    unittest.main()
