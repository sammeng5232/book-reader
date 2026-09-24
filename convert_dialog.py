# -*- coding: utf-8 -*-
"""转换: converting a book to LaTeX + PDF (or, for DjVu, straight to PDF).

One entry point, :func:`start_conversion`, used by the reading screen's "…" menu
and the library's context menu:

1. :class:`ConvertDialog` asks where to save and, for text books, the page size,
   text size, cover and contents options (remembered in ``convert.*`` settings).
2. The conversion runs on a worker thread (:class:`ConvertJob`), opening its own
   copy of the book, so reading can go on; a progress window can cancel it.
3. The result says where the files are and offers to open the PDF or show the
   folder.  XeLaTeX problems are listed under "details" (the PDF may still be fine).

The book's own file is only ever read.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from PySide6.QtCore import QObject, QStandardPaths, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QProgressDialog, QPushButton, QVBoxLayout, QWidget,
)

import latexexport
from strings import S, plural

__all__ = ["ConvertDialog", "ConvertJob", "start_conversion", "default_destination", "PAPER_CHOICES"]

_log = logging.getLogger(__name__)

PAPER_CHOICES = ("a5", "a4", "b5", "letter", "6x9")
_running: list["ConvertJob"] = []          # keeps jobs (and their signals) alive while they run


def _is_djvu(source_format: str | None) -> bool:
    return (source_format or "").lower() in ("djvu", "djv")


def default_destination(book_path: str, store: Any) -> str:
    """The last folder used, else the book's own folder when writable, else Documents."""
    last = str(store.get("convert.last_dir", "") or "") if store is not None else ""
    if last and os.path.isdir(last):
        return last
    folder = os.path.dirname(os.path.abspath(book_path)) if book_path else ""
    if folder and os.path.isdir(folder) and os.access(folder, os.W_OK):
        return folder
    return QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)


class ConvertDialog(QDialog):
    """Where to save, and how to lay out the pages."""

    def __init__(self, parent: QWidget | None, *, title: str, source_format: str, destination: str,
                 store: Any = None, engine: str | None = None,
                 source_path: str | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ConvertDialog")
        self._store = store
        self._title = title
        self._stem = latexexport.source_stem(source_path) if source_path else latexexport.safe_stem(title)
        self._djvu = _is_djvu(source_format)
        self._engine = engine
        self.setWindowTitle(S("convert.title"))
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(10)
        heading = QLabel(S("convert.heading", title=title), self)
        heading.setProperty("erRole", "title")
        heading.setWordWrap(True)
        lay.addWidget(heading)
        body = QLabel(S("convert.djvu.body" if self._djvu else "convert.latex.body"), self)
        body.setProperty("erRole", "secondary")
        body.setWordWrap(True)
        lay.addWidget(body)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)
        row = QHBoxLayout()
        self.dest_edit = QLineEdit(destination, self)
        self.dest_edit.setObjectName("convert-dest")
        self.dest_edit.textChanged.connect(self._update_preview)
        browse = QPushButton(S("convert.browse"), self)
        browse.setObjectName("convert-browse")
        browse.clicked.connect(self._browse)
        row.addWidget(self.dest_edit, 1)
        row.addWidget(browse)
        form.addRow(S("convert.dest"), row)

        get = (lambda k, d: store.get(k, d)) if store is not None else (lambda k, d: d)
        self.paper = QComboBox(self)
        self.paper.setObjectName("convert-paper")
        for p in PAPER_CHOICES:
            self.paper.addItem(S(f"convert.paper.{p}"), p)
        i = self.paper.findData(str(get("convert.paper", "a5")))
        self.paper.setCurrentIndex(max(0, i))
        self.size = QComboBox(self)
        self.size.setObjectName("convert-size")
        for n in (10, 11, 12):
            self.size.addItem(S("convert.fontsize.value", n=n), n)
        i = self.size.findData(int(get("convert.font_size", 11) or 11))
        self.size.setCurrentIndex(max(0, i))
        self.cover = QCheckBox(S("convert.cover"), self)
        self.cover.setChecked(bool(get("convert.cover", True)))
        self.contents = QCheckBox(S("convert.contents"), self)
        self.contents.setChecked(bool(get("convert.contents", True)))
        self.compile = QCheckBox(S("convert.compile"), self)
        self.compile.setObjectName("convert-compile")
        self.compile.setChecked(bool(get("convert.compile_pdf", True)) and engine is not None)
        self.compile.setEnabled(engine is not None)
        if not self._djvu:
            form.addRow(S("convert.paper"), self.paper)
            form.addRow(S("convert.fontsize"), self.size)
            form.addRow("", self.cover)
            form.addRow("", self.contents)
            form.addRow("", self.compile)
        else:
            for w in (self.paper, self.size, self.cover, self.contents, self.compile):
                w.hide()
        lay.addLayout(form)
        if not self._djvu and engine is None:
            note = QLabel(S("convert.no_tex"), self)
            note.setObjectName("convert-no-tex")
            note.setProperty("erRole", "secondary")
            note.setWordWrap(True)
            lay.addWidget(note)
        self.preview = QLabel(self)
        self.preview.setObjectName("convert-preview")
        self.preview.setProperty("erRole", "secondary")
        self.preview.setWordWrap(True)
        self.preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.preview)
        buttons = QDialogButtonBox(self)
        self.ok_button = buttons.addButton(S("convert.start"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.ok_button.setObjectName("convert-start")
        buttons.addButton(S("common.cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._update_preview()

    # ---------------------------------------------------------------- values
    def destination(self) -> str:
        return os.path.normpath(self.dest_edit.text().strip() or ".")

    def target_path(self) -> str:
        """What will be created: a folder (LaTeX) or a PDF file (DjVu)."""
        stem = self._stem
        dest = self.destination()
        if self._djvu:
            return _unique_file(dest, stem, ".pdf")
        return _unique_path(dest, stem)

    def options(self) -> latexexport.ExportOptions:
        return latexexport.ExportOptions(
            paper=str(self.paper.currentData() or "a5"), font_size=int(self.size.currentData() or 11),
            cover=self.cover.isChecked(), contents=self.contents.isChecked(),
            compile_pdf=self.compile.isChecked() and self._engine is not None)

    # ---------------------------------------------------------------- slots
    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, S("convert.pick_folder"), self.destination())
        if folder:
            self.dest_edit.setText(os.path.normpath(folder))

    def _update_preview(self) -> None:
        target = self.target_path()
        shown = target if self._djvu else target + os.sep
        self.preview.setText(S("convert.creates", path=shown))

    def _accept(self) -> None:
        dest = self.destination()
        try:
            os.makedirs(dest, exist_ok=True)
            ok = os.access(dest, os.W_OK)
        except OSError:
            ok = False
        if not ok:
            QMessageBox.warning(self, S("convert.failed.title"), S("convert.failed.write") + "\n" + dest)
            return
        if self._store is not None:
            self._store.set("convert.last_dir", dest)
            if not self._djvu:
                o = self.options()
                self._store.set("convert.paper", o.paper)
                self._store.set("convert.font_size", o.font_size)
                self._store.set("convert.cover", o.cover)
                self._store.set("convert.contents", o.contents)
                if self._engine is not None:
                    self._store.set("convert.compile_pdf", self.compile.isChecked())
        self.accept()


def _unique_path(parent: str, stem: str) -> str:
    path = os.path.join(parent, stem)
    n = 2
    while os.path.exists(path):
        path = os.path.join(parent, f"{stem} ({n})")
        n += 1
    return path


def _unique_file(parent: str, stem: str, ext: str) -> str:
    path = os.path.join(parent, stem + ext)
    n = 2
    while os.path.exists(path):
        path = os.path.join(parent, f"{stem} ({n}){ext}")
        n += 1
    return path


class ConvertJob(QObject):
    """One conversion on a worker thread.  Signals arrive on the GUI thread."""

    progressed = Signal(str, int, int)
    succeeded = Signal(dict)
    failed = Signal(str)          # technical detail
    cancelled = Signal()

    def __init__(self, *, path: str, source_format: str, destination: str, title: str,
                 options: latexexport.ExportOptions | None = None, cache_root: str | None = None,
                 content_key: str | None = None, engine: str | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.path = path
        self.djvu = _is_djvu(source_format)
        self.destination = destination
        self.title = title
        self.options = options or latexexport.ExportOptions()
        self.cache_root = cache_root
        self.content_key = content_key
        self.engine = engine
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.result: dict | None = None

    def start(self) -> None:
        _running.append(self)
        # released on the GUI thread once the job reports (the worker has no Qt event loop)
        for sig in (self.succeeded, self.failed, self.cancelled):
            sig.connect(self._forget)
        self._thread = threading.Thread(target=self._run, name="convert", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._stop.set()

    def is_cancelled(self) -> bool:
        return self._stop.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    def _progress(self, stage: str, done: int, total: int) -> None:
        self.progressed.emit(stage, int(done), int(total))

    def _run(self) -> None:
        import djvupdf
        try:
            if self.djvu:
                out = _unique_file(self.destination, latexexport.source_stem(self.path), ".pdf")
                stats = djvupdf.convert_djvu_to_pdf(
                    self.path, out, title=self.title, cancelled=self._stop.is_set,
                    progress=lambda d, t: self._progress("pages", d, t))
                self.result = {"kind": "djvu", "pdf": out, "pages": int(stats.get("pages") or 0),
                               "failed_pages": list(stats.get("failed_pages") or [])}
            else:
                import bookformats
                book = bookformats.open_book(self.path, cache_root=self.cache_root, content_key=self.content_key)
                try:
                    res = latexexport.export_book(book, self.destination, self.options, progress=self._progress,
                                                  cancelled=self._stop.is_set, engine=self.engine,
                                                  stem=latexexport.source_stem(self.path))
                finally:
                    book.close()
                self.result = {"kind": "latex", "folder": res.folder, "tex": res.tex_path, "pdf": res.pdf_path,
                               "pages": res.pages, "compiled": res.compiled, "engine": res.engine,
                               "problems": list(res.problems), "warnings": list(res.warnings),
                               "chapters": res.chapters, "images": res.images}
        except (latexexport.ExportCancelled, djvupdf.ExportCancelled):
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001 - reported to the user with its details
            _log.exception("conversion failed: %s", self.path)
            detail = getattr(exc, "detail", "") or ""
            self.failed.emit(f"{type(exc).__name__}: {exc}" + (f"\n{detail}" if detail else ""))
        else:
            self.succeeded.emit(self.result)

    def _forget(self, *_args: Any) -> None:
        if self in _running:
            _running.remove(self)


# progress bar: where each stage sits between 0 and 1000
_STAGES_COMPILED = {"read": (0, 60), "convert": (60, 250), "typeset1": (250, 700),
                    "typeset2": (700, 970), "typeset3": (970, 1000)}
_STAGES_TEX_ONLY = {"read": (0, 250), "convert": (250, 1000)}


def _stage_text(stage: str, done: int, total: int) -> str:
    if stage == "read":
        return S("convert.stage.read", done=done, total=total)
    if stage == "convert":
        return S("convert.stage.convert", done=done, total=total)
    if stage.startswith("typeset"):
        return S("convert.stage.typeset", n=stage[7:] or "1", page=done)
    return S("convert.stage.pages", done=done, total=total)


class ConvertProgress(QProgressDialog):
    def __init__(self, job: ConvertJob, parent: QWidget | None) -> None:
        super().__init__(S("convert.stage.read", done=0, total=0), S("common.cancel"), 0, 1000, parent)
        self.setObjectName("ConvertProgress")
        self.setWindowTitle(S("convert.progress.title"))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumDuration(0)
        self.setAutoClose(False)
        self.setAutoReset(False)
        self.setMinimumWidth(460)
        self.setValue(0)
        self._job = job
        self._stages = (_STAGES_COMPILED if job.options.compile_pdf else _STAGES_TEX_ONLY)
        job.progressed.connect(self.on_progress)
        self.canceled.connect(job.cancel)

    def on_progress(self, stage: str, done: int, total: int) -> None:
        self.setLabelText(_stage_text(stage, done, total))
        if self._job.djvu:
            self.setValue(int(1000 * done / total) if total else 0)
            return
        lo, hi = self._stages.get(stage, (0, 1000))
        frac = min(1.0, done / total) if total else 0.0
        self.setValue(max(self.value(), int(lo + (hi - lo) * frac)))


def start_conversion(parent: QWidget, *, path: str, title: str, source_format: str | None,
                     store: Any = None, content_key: str | None = None) -> ConvertJob | None:
    """Ask, convert in the background, report.  Returns the running job (None when cancelled)."""
    if not path or not os.path.isfile(path):
        QMessageBox.warning(parent, S("convert.failed.title"), S("convert.failed.missing"))
        return None
    djvu = _is_djvu(source_format)
    engine = None if djvu else latexexport.find_xelatex()
    dlg = ConvertDialog(parent, title=title, source_format=source_format or "", engine=engine,
                        destination=default_destination(path, store), store=store, source_path=path)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    job = ConvertJob(path=path, source_format=source_format or "", destination=dlg.destination(), title=title,
                     options=dlg.options(), cache_root=getattr(store, "cache_root", None),
                     content_key=content_key, engine=engine)
    progress = ConvertProgress(job, parent)

    def done(result: dict) -> None:
        progress.close()
        progress.deleteLater()
        show_result(parent, result)

    def failed(detail: str) -> None:
        progress.close()
        progress.deleteLater()
        box = QMessageBox(QMessageBox.Icon.Warning, S("convert.failed.title"), S("convert.failed.body"),
                          QMessageBox.StandardButton.Close, parent)
        box.setDetailedText(detail)
        box.exec()

    def cancelled() -> None:
        progress.close()
        progress.deleteLater()

    job.succeeded.connect(done)
    job.failed.connect(failed)
    job.cancelled.connect(cancelled)
    job.start()
    progress.show()
    return job


def result_text(result: dict) -> tuple[str, str, str]:
    """(main text, secondary text, details) describing a finished conversion."""
    if result.get("kind") == "djvu":
        main = plural("convert.done.pdf", int(result.get("pages") or 0)) + "\n" + str(result.get("pdf"))
        failed = result.get("failed_pages") or []
        extra = plural("convert.done.failed_pages", len(failed)) if failed else ""
        return main, extra, ""
    folder = str(result.get("folder"))
    problems = list(result.get("problems") or [])
    details = "\n".join(problems + list(result.get("warnings") or [])[:50])
    if result.get("pdf"):
        main = plural("convert.done.both", int(result.get("pages") or 0)) + "\n" + folder
        extra = plural("convert.done.problems", len(problems)) if problems else ""
    else:
        main = S("convert.done.tex_only") + "\n" + folder
        if not result.get("compiled"):
            extra = S("convert.done.no_tex") if result.get("engine") is None else ""
        else:
            extra = S("convert.done.tex_failed")
    return main, extra, details


def show_result(parent: QWidget | None, result: dict) -> QMessageBox:
    main, extra, details = result_text(result)
    box = QMessageBox(QMessageBox.Icon.Information, S("convert.done.title"), main,
                      QMessageBox.StandardButton.NoButton, parent)
    box.setObjectName("ConvertResult")
    if extra:
        box.setInformativeText(extra)
    if details:
        box.setDetailedText(details)
    box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    pdf = result.get("pdf")
    open_btn = box.addButton(S("convert.open_pdf"), QMessageBox.ButtonRole.AcceptRole) if pdf else None
    folder_btn = box.addButton(S("lib.ctx.reveal"), QMessageBox.ButtonRole.ActionRole)
    box.addButton(S("common.close"), QMessageBox.ButtonRole.RejectRole)
    box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

    def clicked(button: Any) -> None:
        if open_btn is not None and button is open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(pdf)))
        elif button is folder_btn:
            target = str(pdf or result.get("tex") or result.get("folder") or "")
            try:
                import library_page
                library_page._reveal_path(target)
            except Exception:  # noqa: BLE001 - showing the folder is a convenience
                QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(target)))
    box.buttonClicked.connect(clicked)
    box.open()
    return box
