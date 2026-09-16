"""Bounded Windows DNS-cache recovery during one new Quick Tunnel launch.

This clears the system-wide transient DNS resolver cache, including negative
entries; it is not a per-host refresh. It changes neither configured DNS
servers nor the HOSTS file. The tunnel controller owns a bounded retry schedule during a new launch
and may call this only after a genuine public DNS lookup failure.
"""

from __future__ import annotations

import ctypes
import math
import os
from pathlib import Path
import subprocess


_IS_WINDOWS = os.name == "nt"
_LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800
_MAX_TIMEOUT = 5.0


def _system_ipconfig() -> Path | None:
    # Use Windows' own directory API, not PATH, SystemRoot, a shell or a profile.
    library = ctypes.WinDLL("kernel32.dll", winmode=_LOAD_LIBRARY_SEARCH_SYSTEM32)
    library.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    library.GetSystemDirectoryW.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    length = library.GetSystemDirectoryW(buffer, len(buffer))
    if length == 0 or length >= len(buffer) or length != len(buffer.value):
        return None
    directory = Path(buffer.value)
    executable = directory / "ipconfig.exe"
    if (not directory.is_absolute() or str(directory).startswith("\\\\")
            or directory.name.casefold() != "system32"
            or executable.resolve(strict=True) != executable
            or not executable.is_file()):
        return None
    return executable


def clear_startup_dns_cache(*, timeout: float = 2.0) -> bool:
    """Clear transient system-wide DNS cache; True means the command succeeded.

    This is not a connectivity test. The caller must preserve normal DNS, TLS,
    Host and application health checks and enforce a bounded startup-only retry
    schedule after actual DNS failures. Status/lease checks must not call this. No administrative elevation or configuration change is made.

    Run the native system executable directly, avoiding shell/Python redirector
    children. A timeout kills and waits for that exact process. Output is never
    captured or logged. Unsupported platforms and invalid timeouts are no-ops.
    """
    if not _IS_WINDOWS or isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return False
    try:
        seconds = float(timeout)
    except (OverflowError, ValueError):
        return False
    if not math.isfinite(seconds) or seconds <= 0:
        return False
    try:
        executable = _system_ipconfig()
        if executable is None:
            return False
        # Reconstruct minimal environment from the trusted API result. No
        # service secrets, proxy variables, PATH or user profile are inherited.
        windows = str(executable.parent.parent)
        completed = subprocess.run(
            [str(executable), "/flushdns"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, shell=False, close_fds=True,
            cwd=str(executable.parent),
            env={"SystemRoot": windows, "WINDIR": windows},
            timeout=min(seconds, _MAX_TIMEOUT),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0
