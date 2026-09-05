from __future__ import annotations

import json
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from server.app import create_app
from server.recordings import RecordingCapacityError
import test_api as api_fixture

wav_audio = api_fixture.wav_audio


class QwenBoundaryApiTests(unittest.TestCase):
    # Reuse the temporary, non-provider fixture without inheriting its tests.
    setUp = api_fixture.ApiTests.setUp
    tearDown = api_fixture.ApiTests.tearDown
    activate = api_fixture.ApiTests.activate
    headers = api_fixture.ApiTests.headers
    lecture = api_fixture.ApiTests.lecture
    upload = api_fixture.ApiTests.upload

    def install_boundary_engine(self):
        contexts = []
        self.engine.supports_boundary_context = True

        def transcribe(samples, language, overlap_seconds, final_chunk,
                       *, start_seconds, boundary_context, boundary_output):
            contexts.append(boundary_context)
            self.engine.calls += 1
            boundary_output.update({
                "version": 1, "audio_end": start_seconds + len(samples) / 16000,
                "tokens": [{"text": "private-boundary-only", "start": start_seconds,
                            "end": start_seconds + 0.2, "emitted": True}],
            })
            return [{"start": overlap_seconds, "end": len(samples) / 16000,
                     "text": "확정된 문장"}] if overlap_seconds < len(samples) / 16000 else []

        self.engine.transcribe = transcribe
        return contexts

    def stored_context(self, lecture_id):
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT qwen_boundary_json FROM chunks WHERE lecture_id = ? "
                "AND status = 'done' ORDER BY start_seconds DESC, rowid DESC LIMIT 1",
                (lecture_id,),
            ).fetchone()[0]

    def test_context_is_committed_with_text_and_isolated_by_owner_and_lecture(self):
        contexts = self.install_boundary_engine()
        owner = self.activate()
        other = self.activate("user-beta")
        first = self.lecture(owner)
        second = self.lecture(owner)
        foreign = self.lecture(other)
        payload = wav_audio(seconds=8)
        response = self.upload(owner, first, payload, extra_headers={"X-Final-Chunk": "false"})
        first_context = json.loads(self.stored_context(first))
        self.assertEqual(first_context["audio_end"], 8)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("private-boundary", response.text)
        self.assertNotIn("qwen_boundary", response.text)
        self.upload(other, foreign, payload, extra_headers={"X-Final-Chunk": "false"})
        self.upload(owner, second, payload, extra_headers={"X-Final-Chunk": "false"})
        next_response = self.upload(owner, first, payload, start="5", extra_headers={
            "X-Final-Chunk": "false", "X-Overlap-Seconds": "3",
        })
        self.assertEqual(next_response.status_code, 200)
        self.assertEqual(contexts[:3], [None, None, None])
        self.assertEqual(contexts[3], first_context)
        public = self.client.get(f"/lectures/{first}", headers=self.headers(owner))
        self.assertNotIn("private-boundary", public.text)
        self.assertNotIn("qwen_boundary", public.text)

    def test_failed_storage_does_not_advance_context_and_retry_uses_committed_tail(self):
        contexts = self.install_boundary_engine()
        token = self.activate()
        lecture_id = self.lecture(token)
        payload = wav_audio(seconds=8)
        self.upload(token, lecture_id, payload, extra_headers={"X-Final-Chunk": "false"})
        committed = self.stored_context(lecture_id)
        with mock.patch.object(self.app.state.recording_store, "write_chunk",
                               side_effect=RecordingCapacityError("test-only failure")):
            failed = self.upload(token, lecture_id, payload, start="5", extra_headers={
                "X-Final-Chunk": "false", "X-Overlap-Seconds": "3",
            })
        self.assertEqual(failed.status_code, 507)
        self.assertEqual(self.stored_context(lecture_id), committed)
        retried = self.upload(token, lecture_id, payload, start="5", extra_headers={
            "X-Final-Chunk": "false", "X-Overlap-Seconds": "3",
        })
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(contexts[1], contexts[2])
        self.assertEqual(json.loads(self.stored_context(lecture_id))["audio_end"], 13)

    def test_restart_and_final_guard_use_persisted_context_without_mutating_old_chunk(self):
        contexts = self.install_boundary_engine()
        token = self.activate()
        lecture_id = self.lecture(token)
        self.upload(token, lecture_id, wav_audio(seconds=8),
                    extra_headers={"X-Final-Chunk": "false"})
        previous = self.stored_context(lecture_id)
        new_app = create_app(self.settings, self.engine, clova_transcriber=self.clova)
        with TestClient(new_app) as restarted:
            response = restarted.post(f"/lectures/{lecture_id}/recording-finalize",
                                      headers=self.headers(token))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(contexts[1], json.loads(previous))
        self.assertTrue(response.json()["recording_finalized"])
        self.assertNotIn("private-boundary", response.text)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT qwen_boundary_json FROM chunks WHERE lecture_id = ? ORDER BY rowid",
                (lecture_id,),
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], previous)

    def test_malformed_optional_context_falls_back_without_exposing_private_values(self):
        contexts = self.install_boundary_engine()
        token = self.activate()
        lecture_id = self.lecture(token)
        self.upload(token, lecture_id, wav_audio(seconds=8),
                    extra_headers={"X-Final-Chunk": "false"})
        with self.database.connect() as connection:
            connection.execute("UPDATE chunks SET qwen_boundary_json = ? WHERE lecture_id = ?",
                               ("not-json-private-boundary", lecture_id))
        response = self.upload(token, lecture_id, wav_audio(seconds=8), start="5", extra_headers={
            "X-Final-Chunk": "false", "X-Overlap-Seconds": "3",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(contexts[1])
        self.assertNotIn("private-boundary", response.text)


if __name__ == "__main__":
    unittest.main()
