"""Original source names must survive every export entry point."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import unittest

import webhost  # register Qt schemes before QApplication
from PySide6.QtWidgets import QApplication
import bookformats
import convert_dialog as C
import latexexport as L

ROOT = Path(__file__).resolve().parents[1]
APP = None


def setUpModule():
    global APP
    APP = QApplication.instance() or QApplication([])


class ExportFilenames(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='br-names-')
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def source(self, fixture, name):
        source = self.folder / name
        shutil.copyfile(ROOT / 'tests' / fixture, source)
        return source

    def test_source_name_is_not_truncated_or_normalized(self):
        stem = 'Mathematical  Finance_' + 'long_author_name_' * 5 + 'Björk_第三版.v2'
        self.assertGreater(len(stem), 80)
        self.assertEqual(L.source_stem(str(self.folder / (stem + '.epub'))), stem)

    def test_export_uses_filename_and_retains_metadata_title(self):
        stem = 'Arbitrage  Thy in Ctus Time_Björk_第二版.v2'
        source = self.source('fixtures/epub2_ncx.epub', stem + '.epub')
        with bookformats.open_book(source) as book:
            title = book.metadata['title']
            self.assertNotEqual(title, stem)
            out = L.export_book(book, str(self.folder / 'out'), L.ExportOptions(compile_pdf=False))
            again = L.export_book(book, str(self.folder / 'out'), L.ExportOptions(compile_pdf=False))
        self.assertEqual(Path(out.folder).name, stem)
        self.assertEqual(Path(out.tex_path).name, stem + '.tex')
        self.assertEqual(Path(again.folder).name, stem + ' (2)')
        self.assertEqual(Path(again.tex_path).name, stem + '.tex')
        self.assertIn(title, Path(out.tex_path).read_text(encoding='utf-8'))

    def test_dialog_preview_agrees_with_worker_for_epub(self):
        source = self.source('fixtures/epub2_ncx.epub', 'My_Entire_Book_Name_作者.epub')
        dest = str(self.folder / 'out')
        dlg = C.ConvertDialog(None, title='Different metadata title', source_format='epub',
                              destination=dest, source_path=str(source), engine=None)
        self.addCleanup(dlg.close)
        preview = dlg.target_path()
        job = C.ConvertJob(path=str(source), title='Different metadata title', source_format='epub',
                           destination=dest, options=L.ExportOptions(compile_pdf=False))
        errors = []
        job.failed.connect(errors.append)
        job._run()
        self.assertEqual(errors, [])
        self.assertEqual(job.result['folder'], preview)
        self.assertEqual(Path(job.result['tex']).name, source.stem + '.tex')

    def test_mobi_export_uses_original_not_cache_or_metadata_name(self):
        source = self.source('samples/gb11.mobi', 'Alice_Original_Author_Name.mobi')
        with bookformats.open_book(source, cache_root=str(self.folder / 'cache')) as book:
            out = L.export_book(book, str(self.folder / 'out'), L.ExportOptions(compile_pdf=False))
        self.assertEqual(Path(out.tex_path).name, source.stem + '.tex')

    def test_explicit_long_stem_is_not_cut_at_eighty_characters(self):
        source = self.source('fixtures/epub2_ncx.epub', 'short.epub')
        stem = 'Book_' + 'Full_Name_' * 9 + '作者'
        with bookformats.open_book(source) as book:
            out = L.export_book(book, str(self.folder / 'out'), L.ExportOptions(compile_pdf=False), stem=stem)
        self.assertEqual(Path(out.tex_path).name, stem + '.tex')

    def test_djvu_preview_and_actual_pdf_keep_original_name(self):
        source = self.source('samples/ia_jstor_20637537.djvu', 'Scan_Title_Full_Author.djvu')
        dest = str(self.folder / 'out')
        Path(dest).mkdir()  # The accepted destination dialog creates this folder.
        dlg = C.ConvertDialog(None, title='Different title', source_format='djvu',
                              destination=dest, source_path=str(source), engine=None)
        self.addCleanup(dlg.close)
        job = C.ConvertJob(path=str(source), title='Different title', source_format='djvu', destination=dest)
        errors = []
        job.failed.connect(errors.append)
        job._run()
        self.assertEqual(errors, [])
        self.assertEqual(job.result['pdf'], os.path.join(dest, source.stem + '.pdf'))
        self.assertGreater(job.result['pages'], 0)
        self.assertEqual(Path(job.result['pdf']).read_bytes()[:5], b'%PDF-')


if __name__ == '__main__':
    unittest.main()
