"""Report only permission test outcomes; never reads service data or secrets."""
from pathlib import Path
import ctypes
from ctypes import wintypes
import json
import os
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    if os.name != 'nt':
        raise SystemExit('Native Windows is required')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.IsTokenRestricted.argtypes = [wintypes.HANDLE]
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        restricted = bool(security.IsTokenRestricted(token))
    finally:
        kernel.CloseHandle(token)
    report = {'restricted_windows_token': restricted, 'python': sys.version.split()[0]}
    from server.platform_files import ensure_private_directory, atomic_write_private, open_file, validate_private_path
    try:
        with tempfile.TemporaryDirectory(prefix='yeobaek-access-probe-') as name:
            root = Path(name) / 'private'
            ensure_private_directory(root)
            sample = root / 'synthetic.txt'
            atomic_write_private(sample, b'synthetic-permission-probe')
            validate_private_path(sample)
            descriptor = open_file(sample, os.O_RDONLY, private=True)
            try:
                if os.read(descriptor, 128) != b'synthetic-permission-probe':
                    raise ValueError('Synthetic content check failed')
            finally:
                os.close(descriptor)
        report['private_create_reopen_delete'] = 'passed'
    except (OSError, ValueError) as exc:
        report['private_create_reopen_delete'] = 'failed'
        report['error_type'] = type(exc).__name__
        report['winerror'] = getattr(exc, 'winerror', None)
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report['private_create_reopen_delete'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
