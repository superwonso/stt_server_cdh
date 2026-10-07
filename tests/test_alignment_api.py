"""Unresolved Qwen output stays private, durable and explicitly retryable."""
import hashlib
import time
import unittest
import uuid
from unittest import mock
from fastapi.testclient import TestClient
from server.app import create_app
from server import transcription_issues
from server.model_protocol import AlignmentUnavailableError
import test_api as api_fixture
import test_import_api as imports
from test_remote_model_api import BoundaryProbe
import test_remote_model_api as remote

PARTIAL = '합성 임시 문장 <script>not executed</script>'


class AlignmentApiTests(unittest.TestCase):
    setUp = remote.RemoteModelChunkApiTests.setUp
    tearDown = remote.RemoteModelChunkApiTests.tearDown
    activate = remote.RemoteModelChunkApiTests.activate
    headers = remote.RemoteModelChunkApiTests.headers
    lecture = remote.RemoteModelChunkApiTests.lecture
    upload = remote.RemoteModelChunkApiTests.upload
    use_probe = remote.RemoteModelChunkApiTests.use_probe
    rows = remote.RemoteModelChunkApiTests.rows
    recording = remote.RemoteModelChunkApiTests.recording

    def issue(self, token, lecture):
        return self.client.get(f'/lectures/{lecture}', headers=self.headers(token)).json()['transcription_issues']

    def test_cached_failure_is_private_preserves_boundary_and_needs_explicit_retry(self):
        token = self.activate()
        other = self.activate('user-beta')
        lecture = self.lecture(token)
        probe = self.use_probe(BoundaryProbe(fail_from=5, error=AlignmentUnavailableError(PARTIAL)))
        audio = api_fixture.wav_audio(seconds=8)
        self.assertEqual(self.upload(token, lecture, audio, extra_headers={'X-Final-Chunk': 'false'}).status_code, 200)
        before = self.rows(lecture)
        before_pcm = self.recording(lecture).read_bytes()
        chunk = str(uuid.uuid4())
        with mock.patch('server.app.log.exception') as logged:
            failed = self.upload(token, lecture, audio, chunk, '5', {'X-Overlap-Seconds': '3'})
        logged.assert_not_called()
        self.assertEqual(failed.status_code, 422, failed.text)
        self.assertEqual(failed.json()['code'], 'alignment_unavailable')
        self.assertFalse(failed.json()['retryable'])
        self.assertEqual(failed.json()['partial_text'], PARTIAL)
        self.assertEqual(failed.headers['X-Local-Model-Retryable'], '0')
        self.assertEqual(self.rows(lecture), before)
        self.assertEqual(self.recording(lecture).read_bytes(), before_pcm)
        self.assertEqual(self.issue(token, lecture)[0]['partial_text'], PARTIAL)
        self.assertEqual(self.client.get(f'/lectures/{lecture}', headers=self.headers(other)).status_code, 404)
        self.assertEqual(self.upload(other, lecture, audio, chunk, '5', {'X-Overlap-Seconds': '3'}).status_code, 404)
        for _ in range(3):
            self.assertEqual(self.upload(token, lecture, audio, chunk, '5', {'X-Overlap-Seconds': '3'}).status_code, 422)
        self.assertEqual(probe.calls, 2)
        self.assertEqual(self.upload(token, lecture, api_fixture.wav_audio(seconds=7), chunk, '5',
                                     {'X-Overlap-Seconds': '3', 'X-Qwen-Retry': '1'}).status_code, 409)
        self.assertEqual(self.client.post(f'/lectures/{lecture}/recording-finalize', headers=self.headers(token)).status_code, 422)
        probe.available.set()
        second_lecture = self.lecture(other)
        self.assertEqual(self.upload(other, second_lecture).status_code, 200, 'another lecture remains usable')
        self.assertEqual(self.upload(token, lecture, audio, chunk, '5', {'X-Overlap-Seconds': '3'}).status_code, 422)
        restored = self.upload(token, lecture, audio, chunk, '5', {'X-Overlap-Seconds': '3', 'X-Qwen-Retry': '1'})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(probe.inputs[1], probe.inputs[-1])
        self.assertEqual(self.issue(token, lecture), [])
        self.assertEqual(len(self.rows(lecture)[0]), 2)
        self.assertEqual(len(self.rows(lecture)[1]), 2)
        calls = probe.calls
        self.assertEqual(self.upload(token, lecture, audio, chunk, '5', {'X-Overlap-Seconds': '3'}).json(), restored.json())
        self.assertEqual(probe.calls, calls)

    def test_failure_receipt_survives_api_recreation_and_protects_later_chunks(self):
        token = self.activate()
        lecture = self.lecture(token)
        probe = self.use_probe(BoundaryProbe(fail_from=0, error=AlignmentUnavailableError(PARTIAL)))
        chunk = str(uuid.uuid4())
        self.assertEqual(self.upload(token, lecture, chunk_id=chunk).status_code, 422)
        self.client.close()
        self.app = create_app(self.settings, self.engine, clova_transcriber=self.clova)
        self.client = TestClient(self.app)
        self.database = self.app.state.database
        self.assertEqual(self.issue(token, lecture)[0]['partial_text'], PARTIAL)
        self.assertEqual(self.upload(token, lecture, chunk_id=chunk).status_code, 422)
        self.assertEqual(self.upload(token, lecture, chunk_id=str(uuid.uuid4())).status_code, 409)
        self.assertEqual(probe.calls, 1)
        self.assertEqual(self.rows(lecture), ([], [], 0))

    def test_failed_final_guard_is_cached_until_explicit_retry_and_never_finishes_early(self):
        token = self.activate()
        lecture = self.lecture(token)
        probe = self.use_probe(BoundaryProbe())
        self.assertEqual(self.upload(token, lecture, api_fixture.wav_audio(seconds=8),
                         extra_headers={'X-Final-Chunk': 'false'}).status_code, 200)
        before = self.rows(lecture)
        audio = self.recording(lecture).read_bytes()
        probe.fail_from = 0
        probe.error = AlignmentUnavailableError(PARTIAL)
        probe.available.clear()
        url = f'/lectures/{lecture}/recording-finalize'
        failed = self.client.post(url, headers=self.headers(token))
        self.assertEqual(failed.status_code, 422, failed.text)
        self.assertEqual(self.issue(token, lecture)[0]['kind'], 'finalize')
        self.assertEqual(self.rows(lecture), before)
        self.assertEqual(self.client.post(url, headers=self.headers(token)).status_code, 422)
        self.assertEqual(probe.calls, 2)
        probe.available.set()
        good = self.client.post(url, headers=self.headers(token) | {'X-Qwen-Retry': '1'})
        self.assertEqual(good.status_code, 200, good.text)
        self.assertTrue(good.json()['recording_finalized'])
        self.assertEqual(self.recording(lecture).read_bytes(), audio)
        self.assertEqual(probe.inputs[1], probe.inputs[2])
        self.assertEqual(self.issue(token, lecture), [])
        self.assertEqual(self.client.post(url, headers=self.headers(token)).json(), good.json())
        self.assertEqual(probe.calls, 3)

    def test_growing_recording_does_not_persist_an_unreplayable_failed_final_guard(self):
        token = self.activate()
        lecture = self.lecture(token)
        probe = self.use_probe(BoundaryProbe())
        self.assertEqual(self.upload(token, lecture, api_fixture.wav_audio(seconds=8),
                         extra_headers={'X-Final-Chunk': 'false'}).status_code, 200)
        def grow_then_fail(*args, **kwargs):
            self.app.state.recording_store.write_chunk('user-alpha', lecture, start_seconds=8,
                                                      overlap_seconds=0, pcm=b'\xe8\x03'*16000)
            raise AlignmentUnavailableError(PARTIAL)
        self.engine.transcribe = grow_then_fail
        url = f'/lectures/{lecture}/recording-finalize'
        result = self.client.post(url, headers=self.headers(token))
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(self.issue(token, lecture), [])
        self.assertEqual(self.rows(lecture)[2], 0)
        self.engine.transcribe = probe.transcribe
        recovered = self.client.post(url, headers=self.headers(token))
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertTrue(recovered.json()['recording_finalized'])


class AlignmentImportTests(unittest.TestCase):
    setUp = imports.ImportApiTests.setUp
    tearDown = imports.ImportApiTests.tearDown
    headers = imports.ImportApiTests.headers
    create = imports.ImportApiTests.create
    put = imports.ImportApiTests.put
    wait_terminal = imports.ImportApiTests.wait_terminal

    def held(self):
        self.probe = BoundaryProbe(fail_from=5, error=AlignmentUnavailableError(PARTIAL))
        self.engine.supports_boundary_context = True
        self.engine.transcribe = self.probe.transcribe
        audio = imports.wav_file(16)
        result = self.create(audio)
        self.assertEqual(result.status_code, 201, result.text)
        job = result.json()['id']
        part = result.json()['part_bytes']
        for offset in range(0, len(audio), part):
            self.assertEqual(self.put(job, audio[offset:offset+part], offset).status_code, 200)
        self.assertEqual(self.client.post(f'/imports/{job}/complete', headers=self.headers()).status_code, 200)
        state = self.wait_terminal(job)
        self.assertEqual(state['status'], 'failed')
        self.assertTrue(state['needs_review'], state)
        self.assertFalse(state['raw_deleted'])
        self.assertEqual(state['transcription_issues'][0]['partial_text'], PARTIAL)
        return job, state, audio

    def test_held_source_partial_lecture_survive_cleanup_and_restart_then_retry(self):
        job, state, audio = self.held()
        raw = self.settings.data_dir/'imports'/'user-alpha'/f'{job}.upload'
        self.assertEqual(raw.read_bytes(), audio)
        lecture = state['lecture_id']
        self.assertEqual(self.client.get(f'/lectures/{lecture}', headers=self.headers()).status_code, 200)
        before = self.client.get(f'/lectures/{lecture}', headers=self.headers()).json()['segments']
        self.assertTrue(before)
        calls = self.probe.calls
        for _ in range(3):
            self.assertEqual(self.client.get('/imports', headers=self.headers()).status_code, 200)
        self.assertEqual(self.probe.calls, calls)
        self.assertEqual(raw.read_bytes(), audio)
        self.app.state.stop_import_worker()
        self.client.close()
        self.app = create_app(self.settings, self.engine, clova_transcriber=self.clova)
        self.client = TestClient(self.app)
        with self.client:
            self.assertEqual(raw.read_bytes(), audio)
            self.assertTrue(self.client.get(f'/imports/{job}', headers=self.headers()).json()['needs_review'])
            self.assertEqual(self.probe.calls, calls)
            self.assertEqual(self.client.post(f'/imports/{job}/retry-alignment', headers=self.headers('user-beta')).status_code, 404)
            self.probe.available.set()
            retried = self.client.post(f'/imports/{job}/retry-alignment', headers=self.headers())
            self.assertEqual(retried.status_code, 200, retried.text)
            finished = self.wait_terminal(job)
            self.assertEqual(finished['status'], 'completed', finished)
            self.assertFalse(finished['needs_review'])
            self.assertTrue(finished['raw_deleted'])
            after = self.client.get(f'/lectures/{lecture}', headers=self.headers()).json()
            self.assertEqual(after['segments'][:len(before)], before)
            self.assertEqual(after['transcription_issues'], [])
            self.assertFalse(raw.exists())

    def test_explicit_cancel_of_held_import_removes_source_and_partial_lecture(self):
        job, state, audio = self.held()
        response = self.client.post(f'/imports/{job}/cancel', headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'cancelled')
        self.assertTrue(response.json()['raw_deleted'])
        self.assertFalse(response.json()['needs_review'])
        self.assertEqual(self.client.get(f"/lectures/{state['lecture_id']}", headers=self.headers()).status_code, 404)

    def test_failed_retry_receipt_consumes_grant_atomically_before_worker_marks_hold(self):
        job, state, audio = self.held()
        self.app.state.stop_import_worker()
        raw = self.settings.data_dir/'imports'/'user-alpha'/f'{job}.upload'
        lecture = {'id': state['lecture_id'], 'username': 'user-alpha'}
        before = self.client.get(f"/lectures/{lecture['id']}", headers=self.headers()).json()['segments']
        calls = self.probe.calls
        # Recreate the exact persisted boundary after an explicit retry starts,
        # before its second failure is saved and the worker marks it held.
        other = self.create(imports.wav_file(1), username='user-beta').json()['id']
        self.app.state.stop_import_worker()
        database = self.app.state.database
        with database.connect() as connection:
            connection.execute("UPDATE imports SET status='processing',alignment_held=0,alignment_retry_pending=1 WHERE id=?", (job,))
            connection.execute('UPDATE imports SET alignment_retry_pending=1 WHERE id=?', (other,))
            issue = transcription_issues.find(connection, lecture['id'], lecture['username'])

        def save_failed_retry(connection):
            return transcription_issues.save(connection, lecture, issue['chunk_id'], issue['payload_hash'],
                issue['start_seconds'], issue['overlap_seconds'], issue['duration_seconds'],
                bool(issue['final_chunk']), '다시 확인이 필요한 합성 문장', issue['updated_at'], kind=issue['kind'])

        # Receipt and grant consumption must roll back together too.
        with self.assertRaisesRegex(RuntimeError, 'synthetic rollback'):
            with database.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                save_failed_retry(connection)
                self.assertEqual(connection.execute('SELECT alignment_retry_pending FROM imports WHERE id=?', (job,)).fetchone()[0], 0)
                raise RuntimeError('synthetic rollback')
        with database.connect() as connection:
            self.assertEqual(connection.execute('SELECT alignment_retry_pending FROM imports WHERE id=?', (job,)).fetchone()[0], 1)
            self.assertEqual(transcription_issues.find(connection, lecture['id'], lecture['username'])['partial_text'], PARTIAL)

        with database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            saved = save_failed_retry(connection)
        with database.connect() as connection:
            persisted = dict(connection.execute('SELECT * FROM imports WHERE id=?', (job,)).fetchone())
            self.assertEqual((persisted['status'], persisted['alignment_held'], persisted['alignment_retry_pending']), ('processing', 0, 0))
            self.assertEqual(connection.execute('SELECT alignment_retry_pending FROM imports WHERE id=?', (other,)).fetchone()[0], 1)

        # No held-state UPDATE runs before recreation. Startup's real worker
        # must encounter the cached 422, preserve audio, and avoid the model.
        self.client.close()
        self.app = create_app(self.settings, self.engine, clova_transcriber=self.clova)
        self.client = TestClient(self.app)
        with self.client:
            restored = self.wait_terminal(job)
            self.assertEqual(restored['status'], 'failed')
            self.assertTrue(restored['needs_review'])
            self.assertFalse(restored['raw_deleted'])
            self.assertEqual(restored['transcription_issues'][0]['partial_text'], saved['partial_text'])
            self.assertEqual(self.probe.calls, calls, 'restart must not reuse an already-consumed retry grant')
            self.assertEqual(raw.read_bytes(), audio)
            after = self.client.get(f"/lectures/{lecture['id']}", headers=self.headers()).json()
            self.assertEqual(after['segments'], before)


if __name__ == '__main__':
    unittest.main()
