"""Windows memory counters, without process command lines or private paths."""
import ctypes
from ctypes import wintypes as w

class MEMORYSTATUSEX(ctypes.Structure):
    _fields_=[('length',w.DWORD),('load',w.DWORD)]+[(name,ctypes.c_ulonglong) for name in ('total','available','page_total','page_available','virtual_total','virtual_available','extended')]

class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_=[('cb',w.DWORD),('faults',w.DWORD)]+[(name,ctypes.c_size_t) for name in ('peak_working','working','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile')]

def memory_resources():
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    psapi=ctypes.WinDLL('psapi',use_last_error=True)
    kernel.GetCurrentProcess.restype=w.HANDLE
    psapi.GetProcessMemoryInfo.argtypes=[w.HANDLE,ctypes.POINTER(PROCESS_MEMORY_COUNTERS),w.DWORD]
    memory=MEMORYSTATUSEX(); memory.length=ctypes.sizeof(memory)
    process=PROCESS_MEMORY_COUNTERS(); process.cb=ctypes.sizeof(process)
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(memory)) or not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(process),process.cb):
        return None
    return {'total_bytes':memory.total,'available_bytes':memory.available,'used_bytes':memory.total-memory.available,'process_rss_bytes':process.working}
