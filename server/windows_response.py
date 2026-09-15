"""Serve a validated open handle without reopening a replaceable Windows path."""
from __future__ import annotations

import os
import secrets
import threading
import weakref
from starlette.responses import FileResponse
from starlette.datastructures import MutableHeaders
from starlette.concurrency import run_in_threadpool
from .platform_files import read_at


class DescriptorFileResponse(FileResponse):
    def __init__(self, descriptor: int, **kwargs):
        self.descriptor = descriptor
        self._io_lock = threading.Lock()
        self._reading = False
        self._closing = False
        kwargs.setdefault('stat_result', os.fstat(descriptor))
        super().__init__('validated-open-handle.wav', **kwargs)
        # A cancelled sync endpoint can discard its returned response before
        # ASGI calls it. The finalizer also owns that otherwise orphaned fd.
        # An active bound _read worker retains self until it has finished.
        self._finalizer = weakref.finalize(self, os.close, descriptor)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.close()

    def close(self):
        # asyncio task cancellation can abandon a threadpool await while its
        # read still runs. Keep that fd alive until the worker exits, otherwise
        # the integer can be reused for a different request's private file.
        with self._io_lock:
            self._closing = True
            if not self._reading and self.descriptor >= 0:
                descriptor, self.descriptor = self.descriptor, -1
                self._finalizer()

    def _read(self, count, offset):
        with self._io_lock:
            if self._closing or self.descriptor < 0 or self._reading:
                raise OSError('Validated download is closed or already reading')
            self._reading = True
            descriptor = self.descriptor
        try:
            return read_at(descriptor, count, offset)
        finally:
            with self._io_lock:
                self._reading = False
                if self._closing and self.descriptor >= 0:
                    descriptor, self.descriptor = self.descriptor, -1
                    self._finalizer()

    async def _bytes(self, send, start, end):
        while start < end:
            chunk = await run_in_threadpool(self._read, min(self.chunk_size, end-start), start)
            if not chunk:
                raise OSError('Validated download was truncated')
            start += len(chunk)
            await send({'type':'http.response.body', 'body':chunk, 'more_body':True})

    async def _handle_simple(self, send, send_header_only, send_pathsend):
        # ASGI pathsend cannot preserve the validated handle's identity.
        await send({'type':'http.response.start','status':self.status_code,'headers':self.raw_headers})
        if not send_header_only:
            await self._bytes(send,0,self.stat_result.st_size)
        await send({'type':'http.response.body','body':b'', 'more_body':False})

    async def _handle_single_range(self, send, start, end, file_size, send_header_only):
        headers=MutableHeaders(raw=list(self.raw_headers))
        headers['content-range']=f'bytes {start}-{end-1}/{file_size}'
        headers['content-length']=str(end-start)
        await send({'type':'http.response.start','status':206,'headers':headers.raw})
        if not send_header_only:
            await self._bytes(send,start,end)
        await send({'type':'http.response.body','body':b'', 'more_body':False})

    async def _handle_multiple_ranges(self, send, ranges, file_size, send_header_only):
        boundary=secrets.token_hex(13)
        length,header=self.generate_multipart(ranges,boundary,file_size,self.headers['content-type'])
        headers=MutableHeaders(raw=list(self.raw_headers))
        headers['content-type']=f'multipart/byteranges; boundary={boundary}'
        headers['content-length']=str(length)
        await send({'type':'http.response.start','status':206,'headers':headers.raw})
        if not send_header_only:
            for start,end in ranges:
                await send({'type':'http.response.body','body':header(start,end),'more_body':True})
                await self._bytes(send,start,end)
                await send({'type':'http.response.body','body':b'\r\n','more_body':True})
        await send({'type':'http.response.body','body':b'' if send_header_only else f'--{boundary}--'.encode('ascii'),'more_body':False})
