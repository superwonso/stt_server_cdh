"""Own every child during native startup, including Python's venv redirector.

Launch suspended, assign a private job, and resume the one initial thread. If
startup fails, closing the armed job kills only this newly created process tree.
After verified child registration, disarm and close it for independent lifetime.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import os
import subprocess
import time

from .model_process import ModelProcessError

if os.name == "nt":
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.SetInformationJobObject.restype = w.BOOL
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.AssignProcessToJobObject.restype = w.BOOL
    kernel.IsProcessInJob.argtypes = [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)]
    kernel.IsProcessInJob.restype = w.BOOL
    kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
    kernel.TerminateJobObject.restype = w.BOOL
    kernel.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
    kernel.QueryInformationJobObject.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenThread.restype = w.HANDLE
    kernel.GetProcessIdOfThread.argtypes = [w.HANDLE]
    kernel.GetProcessIdOfThread.restype = w.DWORD
    kernel.ResumeThread.argtypes = [w.HANDLE]
    kernel.ResumeThread.restype = w.DWORD
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.WaitForSingleObject.restype = w.DWORD

    class BASIC_LIMITS(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64), ("flags", w.DWORD),
                    ("min_working_set", ctypes.c_size_t), ("max_working_set", ctypes.c_size_t),
                    ("active_process_limit", w.DWORD), ("affinity", ctypes.c_size_t),
                    ("priority", w.DWORD), ("scheduling", w.DWORD)]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

    class EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [("basic", BASIC_LIMITS), ("io", IO_COUNTERS), ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

    class THREAD_ENTRY(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("tid", w.DWORD), ("pid", w.DWORD),
                    ("base_priority", w.LONG), ("delta_priority", w.LONG), ("flags", w.DWORD)]

    class ACCOUNTING(ctypes.Structure):
        _fields_ = [(name, ctypes.c_int64) for name in ("user_time", "kernel_time", "period_user_time", "period_kernel_time")]
        _fields_ += [(name, w.DWORD) for name in ("page_faults", "total_processes", "active_processes", "terminated_processes")]

    kernel.Thread32First.argtypes = [w.HANDLE, ctypes.POINTER(THREAD_ENTRY)]
    kernel.Thread32First.restype = w.BOOL
    kernel.Thread32Next.argtypes = [w.HANDLE, ctypes.POINTER(THREAD_ENTRY)]
    kernel.Thread32Next.restype = w.BOOL


def _resume_initial_thread(pid, process_handle):
    snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ModelProcessError("새 프로세스의 초기 스레드를 확인하지 못했습니다.")
    try:
        entry = THREAD_ENTRY()
        entry.size = ctypes.sizeof(entry)
        identifiers = []
        available = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while available:
            if entry.pid == pid:
                identifiers.append(entry.tid)
            entry.size = ctypes.sizeof(entry)
            available = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        if len(identifiers) != 1:
            raise ModelProcessError("새 프로세스의 초기 스레드가 예상과 달라 시작하지 않았습니다.")
        thread = kernel.OpenThread(0x0802, False, identifiers[0])
        if not thread:
            raise ModelProcessError("새 프로세스의 초기 스레드를 열지 못했습니다.")
        try:
            if (kernel.WaitForSingleObject(process_handle, 0) != 258
                    or kernel.GetProcessIdOfThread(thread) != pid or kernel.ResumeThread(thread) != 1):
                raise ModelProcessError("새 프로세스를 안전하게 재개하지 못했습니다.")
        finally:
            kernel.CloseHandle(thread)
    finally:
        kernel.CloseHandle(snapshot)


class OwnedLaunch:
    def __init__(self, command, *, cwd, env, stdout):
        self.command, self.cwd, self.env, self.stdout = command, cwd, env, stdout
        self.job = None
        self.process = None
        self.committed = False

    def _limits(self, armed):
        limits = EXTENDED_LIMITS()
        limits.basic.flags = 0x00002000 if armed else 0
        if not kernel.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ModelProcessError("새 프로세스의 시작 보호를 설정하지 못했습니다.")

    def __enter__(self):
        if os.name != "nt":
            raise ModelProcessError("Windows 시작 보호가 필요합니다.")
        self.job = kernel.CreateJobObjectW(None, None)
        if not self.job:
            raise ModelProcessError("새 프로세스의 전용 시작 보호를 만들지 못했습니다.")
        try:
            self._limits(True)
            self.process = subprocess.Popen(self.command, cwd=self.cwd, env=self.env,
                stdin=subprocess.DEVNULL, stdout=self.stdout, stderr=self.stdout, close_fds=True,
                creationflags=0x00000004 | subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
            if not kernel.AssignProcessToJobObject(self.job, int(self.process._handle)):
                raise ModelProcessError("새 프로세스를 전용 시작 보호에 연결하지 못했습니다.")
            _resume_initial_thread(self.process.pid, int(self.process._handle))
            return self
        except BaseException:
            self._rollback()
            raise

    def commit(self, record):
        from .win_model_process import ProcessHandle
        with ProcessHandle(record["pid"]) as child:
            actual = child.identity()
            member = w.BOOL()
            if (actual is None or any(actual[key] != record[key] for key in actual)
                    or not kernel.IsProcessInJob(child.handle, self.job, ctypes.byref(member)) or not member.value):
                raise ModelProcessError("등록한 자식이 이 시작 작업의 프로세스인지 확인하지 못했습니다.")
        self._limits(False)
        kernel.CloseHandle(self.job)
        self.job = None
        self.committed = True

    def _rollback(self):
        if self.job:
            members = []
            try:
                # ActiveProcesses may reach zero a little before each process
                # HANDLE becomes signaled. Retain handles to the current job
                # members so return/cleanup observes their actual exit.
                from .win_model_process import ProcessHandle
                for capacity in (16, 128, 1024, 4096):
                    listing = ctypes.create_string_buffer(8 + ctypes.sizeof(ctypes.c_size_t) * capacity)
                    if kernel.QueryInformationJobObject(self.job, 3, listing, len(listing), None):
                        count = w.DWORD.from_buffer(listing, 4).value
                        identifiers = (ctypes.c_size_t * count).from_buffer(listing, 8)
                        break
                    if ctypes.get_last_error() != 234:
                        raise ModelProcessError("시작 작업의 자식 목록을 확인하지 못했습니다.")
                else:
                    raise ModelProcessError("시작 작업의 자식 수가 안전한 종료 범위를 넘었습니다.")
                for pid in identifiers:
                    try:
                        member = ProcessHandle(int(pid))
                    except ProcessLookupError:
                        continue
                    belongs = w.BOOL()
                    if kernel.IsProcessInJob(member.handle, self.job, ctypes.byref(belongs)) and belongs.value:
                        members.append(member)
                    else:
                        member.close()
                if not kernel.TerminateJobObject(self.job, 1):
                    raise ModelProcessError("시작 실패 프로세스 묶음의 종료를 요청하지 못했습니다.")
                deadline = time.monotonic() + 5
                while True:
                    accounting = ACCOUNTING()
                    if not kernel.QueryInformationJobObject(self.job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                        raise ModelProcessError("시작 실패 자식 프로세스 종료를 확인하지 못했습니다.")
                    if accounting.active_processes == 0:
                        break
                    if time.monotonic() >= deadline:
                        raise ModelProcessError("시작 실패 자식 프로세스가 종료 대기 중입니다.")
                    time.sleep(.01)
                for member in members:
                    if not member.wait(max(0, deadline - time.monotonic())):
                        raise ModelProcessError("시작 실패 자식의 실제 종료를 확인하지 못했습니다.")
            finally:
                for member in members:
                    member.close()
                kernel.CloseHandle(self.job)
                self.job = None
        if self.process is not None:
            # Before job assignment only the suspended launcher can exist.
            # Popen.terminate uses its original Windows HANDLE, never a PID-only
            # signal, and cannot target a subsequently recycled PID.
            if self.process.poll() is None:
                self.process.terminate()
            self.process.wait(timeout=5)

    def __exit__(self, *args):
        if not self.committed:
            self._rollback()
