"""Verify Qt's ICU ABI after freezing on Windows.

Windows provides the unsuffixed ICU API used by Qt. Some build hosts also have
Conda/Poppler ICU on their DLL search path; those libraries use version-suffixed
exports and must not shadow Windows' ICU in the resulting application.
"""
from pathlib import Path
import argparse
import os
import pefile


def exports(path):
    pe = pefile.PE(str(path), fast_load=True)
    try:
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXPORT']])
        return {s.name for s in pe.DIRECTORY_ENTRY_EXPORT.symbols if s.name}
    finally:
        pe.close()


def imports(path, library):
    pe = pefile.PE(str(path), fast_load=True)
    try:
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT']])
        return {s.name for entry in getattr(pe, 'DIRECTORY_ENTRY_IMPORT', [])
                if entry.dll.lower() == library for s in entry.imports if s.name}
    finally:
        pe.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    root = parser.parse_args().bundle.resolve() / '_internal'
    bundled = root / 'icuuc.dll'
    if not bundled.exists():
        print('Qt uses Windows ICU; no bundled shadow library.')
        return
    required = imports(root / 'PySide6' / 'Qt6Core.dll', b'icuuc.dll')
    if not required or required <= exports(bundled):
        print('Bundled ICU provides the API required by Qt.')
        return
    system = Path(os.environ['SystemRoot']) / 'System32' / 'icuuc.dll'
    provided = exports(system)
    incompatible = []
    consumers = 0
    for path in root.rglob('*'):
        if path.is_file() and path.suffix.lower() in ('.dll', '.pyd'):
            needed = imports(path, b'icuuc.dll')
            if needed:
                consumers += 1
                if needed - provided:
                    incompatible.append(str(path.relative_to(root)))
    if incompatible:
        raise RuntimeError('Conflicting ICU consumers require separate dependency isolation: ' + ', '.join(incompatible))
    if not required <= provided:
        raise RuntimeError('Windows ICU does not provide the API required by this Qt build.')
    bundled.unlink()
    print(f'Removed incompatible bundled ICU; Windows exports satisfy all {consumers} consumers.')


if __name__ == '__main__':
    main()
