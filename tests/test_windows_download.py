"""Actual native descriptor downloads with public synthetic fixture files.

These test transport/handle behavior separately from private-storage ACL tests.
No app accounts, database, credentials, or real audio are used.
"""
from __future__ import annotations

import asyncio
import gc
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from server.platform_files import open_file, read_at
from server.windows_response import DescriptorFileResponse


@unittest.skipUnless(os.name == "nt", "Windows descriptor transport")
class WindowsDownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.payload = bytes(range(256)) * 1024
        with tempfile.NamedTemporaryFile(prefix="yeobaek-download-synthetic-", delete=False) as output:
            output.write(self.payload)
            self.path = Path(output.name)
        self.descriptors = []

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        for descriptor in self.descriptors:
            try:
                os.fstat(descriptor)
            except OSError:
                continue
            os.close(descriptor)
            self.fail("A download leaked its descriptor")

    def response(self):
        descriptor = open_file(self.path, os.O_RDONLY)
        self.descriptors.append(descriptor)
        return DescriptorFileResponse(descriptor, media_type="audio/wav", filename="synthetic.wav",
                                      headers={"Cache-Control": "no-store"})

    async def invoke(self, response, *, method="GET", headers=(), send=None, extensions=None):
        events = []
        async def collect(event):
            events.append(event)
        async def receive():
            return {"type": "http.disconnect"}
        scope = {"type": "http", "method": method, "headers": list(headers), "extensions": extensions or {},
                 "asgi": {"version": "3.0", "spec_version": "2.4"}}
        await response(scope, receive, send or collect)
        start = next(event for event in events if event["type"] == "http.response.start") if events else None
        body = b"".join(event.get("body", b"") for event in events if event["type"] == "http.response.body")
        return start, body, events

    async def test_discarded_response_closes_handle_before_asgi_invocation(self):
        response = self.response()
        descriptor = response.descriptor
        del response
        gc.collect()
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    async def test_full_download_closes_handle_and_ignores_pathsend(self):
        response = self.response()
        start, body, events = await self.invoke(response, extensions={"http.response.pathsend": {}})
        self.assertEqual(start["status"], 200)
        self.assertEqual(body, self.payload)
        self.assertEqual(dict(start["headers"])[b"content-length"], str(len(body)).encode())
        self.assertEqual(dict(start["headers"])[b"cache-control"], b"no-store")
        self.assertNotIn("http.response.pathsend", [event["type"] for event in events])
        self.assertEqual(response.descriptor, -1)

    async def test_single_range_suffix_and_head(self):
        for header, expected in ((b"bytes=17-300", self.payload[17:301]), (b"bytes=-29", self.payload[-29:])):
            for method in ("GET", "HEAD"):
                with self.subTest(range=header, method=method):
                    response = self.response()
                    start, body, _ = await self.invoke(response, method=method, headers=[(b"range", header)])
                    self.assertEqual(start["status"], 206)
                    self.assertEqual(body, expected if method == "GET" else b"")
                    self.assertEqual(int(dict(start["headers"])[b"content-length"]), len(expected))

    async def test_multipart_ranges_have_exact_payload_length_and_head(self):
        ranges = [(7, 21), (120, 138)]
        for method in ("GET", "HEAD"):
            response = self.response()
            start, body, _ = await self.invoke(response, method=method, headers=[(b"range", b"bytes=7-20,120-137")])
            headers = dict(start["headers"])
            self.assertEqual(start["status"], 206)
            self.assertTrue(headers[b"content-type"].startswith(b"multipart/byteranges; boundary="))
            if method == "HEAD":
                self.assertEqual(body, b"")
                continue
            self.assertEqual(len(body), int(headers[b"content-length"]))
            boundary = headers[b"content-type"].split(b"boundary=", 1)[1]
            for first, last in ranges:
                expected = f"Content-Range: bytes {first}-{last - 1}/{len(self.payload)}".encode()
                self.assertIn(expected.lower(), body.lower())
                self.assertIn(self.payload[first:last], body)
            self.assertTrue(body.endswith(b"--" + boundary + b"--"))

    async def test_full_head_has_no_body_and_invalid_range_closes_handle(self):
        response = self.response()
        start, body, _ = await self.invoke(response, method="HEAD")
        self.assertEqual(start["status"], 200)
        self.assertEqual(body, b"")
        self.assertEqual(int(dict(start["headers"])[b"content-length"]), len(self.payload))
        for value, expected in ((b"bytes=999999999-", 416), (b"invalid", 400)):
            start, _, _ = await self.invoke(self.response(), headers=[(b"range", value)])
            self.assertEqual(start["status"], expected)

    async def test_replacement_and_deletion_keep_validated_original_handle(self):
        response = self.response()
        moved = self.path.with_name(self.path.name + ".old")
        # MoveFileEx cannot replace an open destination on this Windows build.
        # Rename the opened object, then create/delete a new object at its former
        # pathname: a path-based response would fail or serve unrelated bytes.
        try:
            self.path.rename(moved)
            self.path.write_bytes(b"unrelated-replacement")
            self.path.unlink()
            moved.unlink()
            start, body, _ = await self.invoke(response)
            self.assertEqual(start["status"], 200)
            self.assertEqual(body, self.payload)
        finally:
            moved.unlink(missing_ok=True)

    async def test_send_disconnect_closes_handle(self):
        response = self.response()
        async def send(event):
            if event["type"] == "http.response.body":
                raise OSError("Synthetic client disconnected")
        with self.assertRaises(OSError):
            await self.invoke(response, send=send)
        self.assertEqual(response.descriptor, -1)

    async def test_requester_cancellation_closes_handle(self):
        response = self.response()
        reached = asyncio.Event()
        async def send(event):
            if event["type"] == "http.response.body":
                reached.set()
                await asyncio.Future()
        task = asyncio.create_task(self.invoke(response, send=send))
        await asyncio.wait_for(reached.wait(), timeout=3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(response.descriptor, -1)

    async def test_cancellation_during_worker_read_defers_close_until_read_finishes(self):
        response = self.response()
        descriptor = response.descriptor
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def delayed_read(fd, count, offset):
            entered.set()
            try:
                if not release.wait(3):
                    raise TimeoutError("Synthetic read was not released")
                return read_at(fd, count, offset)
            finally:
                finished.set()
        with mock.patch("server.windows_response.read_at", side_effect=delayed_read):
            task = asyncio.create_task(self.invoke(response))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(os.fstat(descriptor).st_size, len(self.payload))
            finally:
                release.set()
                await asyncio.to_thread(finished.wait, 2)
        for _ in range(100):
            if response.descriptor == -1:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(response.descriptor, -1)

    async def test_truncated_source_aborts_instead_of_claiming_full_response(self):
        response = self.response()
        self.path.write_bytes(b"short")
        with self.assertRaises(OSError):
            await self.invoke(response)
        self.assertEqual(response.descriptor, -1)


if __name__ == "__main__":
    unittest.main()
