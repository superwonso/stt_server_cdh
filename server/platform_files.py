"""Private files using POSIX permissions or Windows kernel ACLs and locks.

Windows objects receive an owner-only DACL before writing any secret. Existing
objects are checked without silently changing their permissions. Reparse points
and multiply-linked files fail closed. No optional security dependency is used.
"""
from __future__ import annotations

import os
import secrets
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    P = ctypes.c_void_p
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [P]
    kernel.LocalFree.restype = P
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, P, w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CreateDirectoryW.argtypes = [w.LPCWSTR, P]
    kernel.MoveFileExW.argtypes = [w.LPCWSTR, w.LPCWSTR, w.DWORD]
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, P, w.DWORD, ctypes.POINTER(w.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [P, ctypes.POINTER(w.LPWSTR)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(P), P]
    advapi.GetSecurityInfo.argtypes = [w.HANDLE, ctypes.c_int, w.DWORD, ctypes.POINTER(P), P, ctypes.POINTER(P), P, ctypes.POINTER(P)]
    advapi.GetAce.argtypes = [P, w.DWORD, ctypes.POINTER(P)]

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", P), ("bInheritHandle", w.BOOL)]

    class ACL(ctypes.Structure):
        _fields_ = [("revision", w.BYTE), ("reserved", w.BYTE), ("size", w.WORD), ("count", w.WORD), ("reserved2", w.WORD)]

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]

    kernel.LockFileEx.argtypes = [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, w.DWORD, ctypes.POINTER(OVERLAPPED)]
    kernel.UnlockFileEx.argtypes = [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, ctypes.POINTER(OVERLAPPED)]


def _win_error() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


def _sid_string(sid) -> str:
    result = w.LPWSTR()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(result)):
        raise _win_error()
    try:
        return result.value
    finally:
        kernel.LocalFree(result)


def current_user_sid() -> str:
    if not IS_WINDOWS:
        raise OSError("Windows security identity is unavailable")
    token = w.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise _win_error()
    try:
        size = w.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        data = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, data, size, ctypes.byref(size)):
            raise _win_error()
        return _sid_string(P.from_buffer(data).value)
    finally:
        kernel.CloseHandle(token)


@contextmanager
def _security_attributes():
    descriptor = P()
    sid = current_user_sid()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        f"O:{sid}D:P(A;OICI;FA;;;{sid})", 1, ctypes.byref(descriptor), None
    ):
        raise _win_error()
    attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False)
    try:
        yield ctypes.byref(attributes)
    finally:
        kernel.LocalFree(descriptor)


def _validate_handle_acl(handle) -> None:
    owner, dacl, security = P(), P(), P()
    result = advapi.GetSecurityInfo(handle, 1, 0x00000005, ctypes.byref(owner), None,
                                  ctypes.byref(dacl), None, ctypes.byref(security))
    if result:
        raise ctypes.WinError(result)
    try:
        sid = current_user_sid()
        if not owner or _sid_string(owner) != sid or not dacl:
            raise PermissionError("Private object has an unsafe owner or DACL")
        acl = ctypes.cast(dacl, ctypes.POINTER(ACL)).contents
        has_owner_access = False
        for index in range(acl.count):
            ace = P()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise _win_error()
            kind = ctypes.c_ubyte.from_address(ace.value).value
            flags = ctypes.c_ubyte.from_address(ace.value + 1).value
            # Object/callback ACEs need a richer parser: fail closed.
            if kind not in (0, 1):
                raise PermissionError("Private object has an unsupported ACE")
            if kind == 1:
                continue
            principal = _sid_string(ace.value + 8)
            if principal not in {sid, "S-1-5-18", "S-1-5-32-544"}:
                raise PermissionError("Private object grants another principal access")
            if principal == sid and not flags & 0x08:
                mask = w.DWORD.from_address(ace.value + 4).value
                has_owner_access |= bool(mask & 0x0001)
        if not has_owner_access:
            raise PermissionError("Private object does not grant owner access")
    finally:
        kernel.LocalFree(security)


def reject_links(path: Path) -> None:
    path = Path(path).absolute()
    for current in (*reversed(path.parents), path):
        try:
            details = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400:
            raise PermissionError("Reparse points and symbolic links are not allowed")


@contextmanager
def _pinned_parents(path: Path):
    """Deny ancestor rename/deletion while resolving a Windows leaf."""
    handles = []
    try:
        for parent in reversed(Path(path).absolute().parents):
            handle = kernel.CreateFileW(str(parent), 0x00020080, 0x3, None, 3, 0x02200000, None)
            if handle == P(-1).value:
                raise _win_error()
            handles.append(handle)
            if getattr(parent.lstat(), "st_file_attributes", 0) & 0x400:
                raise PermissionError("Reparse parent is not allowed")
        yield
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


def validate_private_path(path: Path, *, directory: bool = False) -> None:
    path = Path(path)
    reject_links(path)
    if not IS_WINDOWS:
        details = path.lstat()
        if ((not stat.S_ISDIR(details.st_mode) if directory else not stat.S_ISREG(details.st_mode))
                or details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != (0o700 if directory else 0o600)
                or (not directory and details.st_nlink != 1)):
            raise PermissionError("Private object permissions are unsafe")
        return
    with _pinned_parents(path):
        handle = kernel.CreateFileW(str(path.absolute()), 0x00020080, 0x3, None, 3, 0x02200000, None)
        if handle == P(-1).value:
            raise _win_error()
        try:
            details = path.lstat()
            if (bool(details.st_file_attributes & 0x400)
                    or (not stat.S_ISDIR(details.st_mode) if directory else not stat.S_ISREG(details.st_mode))
                    or (not directory and details.st_nlink != 1)):
                raise PermissionError("Private object type is unsafe")
            _validate_handle_acl(handle)
        finally:
            kernel.CloseHandle(handle)


def ensure_private_directory(path: Path, *, parents: bool = True) -> None:
    path = Path(path)
    reject_links(path)
    if not path.exists():
        if parents and not path.parent.exists():
            ensure_private_directory(path.parent)
        if IS_WINDOWS:
            with _pinned_parents(path), _security_attributes() as attributes:
                if not kernel.CreateDirectoryW(str(path.absolute()), attributes):
                    if ctypes.get_last_error() != 183:
                        raise _win_error()
        else:
            path.mkdir(mode=0o700)
    validate_private_path(path, directory=True)


def make_private_temporary_directory(*, prefix: str = "stt-", directory: Path | None = None) -> Path:
    """Create a unique scratch directory with private permissions from birth.

    Windows Python 3.12 tempfile directories can inherit additional principals;
    setting a Unix mode cannot turn that inherited DACL into private storage.
    The caller owns cleanup, including when retaining verified recovery files.
    """
    if not prefix or any(character in prefix for character in "/\\"):
        raise ValueError("Temporary directory prefix must be a file name")
    if not IS_WINDOWS:
        return Path(tempfile.mkdtemp(prefix=prefix, dir=directory))
    parent = Path(directory or tempfile.gettempdir()).absolute()
    reject_links(parent)
    for _attempt in range(10):
        target = parent / (prefix + secrets.token_hex(16))
        with _pinned_parents(target), _security_attributes() as attributes:
            if not kernel.CreateDirectoryW(str(target), attributes):
                if ctypes.get_last_error() == 183:
                    continue
                raise _win_error()
            validate_private_path(target, directory=True)
            return target
    raise FileExistsError("Unable to allocate a private temporary directory")


def open_file(path: Path, flags: int, mode: int = 0o600, *, private: bool = False) -> int:
    """Open a regular file; Windows permits descriptor-safe deletion."""
    path = Path(path)
    reject_links(path)
    if not IS_WINDOWS:
        fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), mode)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or (private and (info.st_uid != os.geteuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != mode)):
                raise PermissionError("File is not safe")
            return fd
        except BaseException:
            os.close(fd)
            raise
    with _pinned_parents(path):
        if private:
            validate_private_path(path.parent, directory=True)
        access = 0x00020080 | (0xC0000000 if flags & os.O_RDWR else 0x40000000 if flags & os.O_WRONLY else 0x80000000)
        # Validate an existing file before any requested truncation.
        creation = 1 if flags & os.O_EXCL and flags & os.O_CREAT else 4 if flags & os.O_CREAT else 3
        with _security_attributes() as attributes:
            handle = kernel.CreateFileW(str(path.absolute()), access, 0x7, attributes, creation, 0x00200000, None)
        if handle == P(-1).value:
            raise _win_error()
        fd = -1
        try:
            fd = msvcrt.open_osfhandle(handle, (flags & (os.O_RDWR | os.O_WRONLY | os.O_APPEND)) | os.O_BINARY)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & 0x400:
                raise PermissionError("File is not a safe regular file")
            if private:
                _validate_handle_acl(handle)
            if flags & os.O_TRUNC:
                os.ftruncate(fd, 0)
            return fd
        except BaseException:
            if fd >= 0:
                os.close(fd)
            else:
                kernel.CloseHandle(handle)
            raise


def set_private_file(descriptor: int) -> None:
    if IS_WINDOWS:
        _validate_handle_acl(msvcrt.get_osfhandle(descriptor))
    else:
        os.fchmod(descriptor, 0o600)


@contextmanager
def file_lock(descriptor: int, *, blocking: bool = True):
    if IS_WINDOWS:
        handle = msvcrt.get_osfhandle(descriptor)
        overlapped = OVERLAPPED()
        if not kernel.LockFileEx(handle, 2 | (0 if blocking else 1), 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(overlapped)):
            if ctypes.get_last_error() == 33:
                raise BlockingIOError("Private file lock is held")
            raise _win_error()
        try:
            yield
        finally:
            if not kernel.UnlockFileEx(handle, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(overlapped)):
                raise _win_error()
    else:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)


def sync_directory(path: Path) -> None:
    if IS_WINDOWS:
        # Durable renames use MOVEFILE_WRITE_THROUGH below. Windows rejects
        # FlushFileBuffers on ordinary read-only directory handles.
        validate_private_path(path, directory=True)
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_private(path: Path, content: bytes) -> None:
    path = Path(path)
    validate_private_path(path.parent, directory=True)
    if path.exists() or path.is_symlink():
        validate_private_path(path)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")
    try:
        fd = open_file(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, private=True)
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if IS_WINDOWS:
            with _pinned_parents(path):
                if not kernel.MoveFileExW(str(temporary.absolute()), str(path.absolute()), 0x1 | 0x8):
                    raise _win_error()
        else:
            os.replace(temporary, path)
            sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def read_at(descriptor: int, count: int, offset: int) -> bytes:
    """The caller serializes seek/read when Windows shares this descriptor."""
    if IS_WINDOWS:
        os.lseek(descriptor, offset, os.SEEK_SET)
        return os.read(descriptor, count)
    return os.pread(descriptor, count, offset)
