"""Bounded real audio/API checks against the managed synthetic Windows profile.

By default this only verifies approved sample hashes and WAV normalization.
--run performs real Qwen inference on the already running local model. It never
starts/stops services, reads operational credentials, or calls a remote service.
Only aggregate metadata is printed; transcript/audio/session/account data stays
in memory or in the pre-existing private synthetic database.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sys
import time
import uuid
import wave

import httpx
import numpy as np
import soundfile as sf

from .windows_local import (MODEL_PORT, MODEL_RUNTIME, PROFILE_KIND, WindowsAPIController,
                            read_profile, verify_test_database)
from .win_model_process import WindowsModelController
from .windows_local_smoke import API_URL, SmokeFailure, require

SAMPLE_RATE = 16000
PART_BYTES = 480 * 1024
MAX_MEDIA_BYTES = 4 * 1024 * 1024
DEFAULT_SAMPLES_ROOT = Path(__file__).resolve().parents[1] / ".samples"
# These four hashes fix the user-approved public/TTS scope. Reference text and
# manifest URLs are deliberately never opened by this tool.
APPROVED = {
    "ko-01": ("public", "ko", "d9da4baddc412d43efd85398aa146e91529392c28ee63e1436626557ae5dd0ba"),
    "en-01": ("public", "en", "9ce35224156f071ab58eb7feb8a5ceae600f6f9f353da2a6cbf797b6b1ac8a23"),
    "synthetic-ko": ("synthetic", "ko", "8cf055d5d057967844d97cdeebe1a0d034051a9ac3ca6ee52df23c96a053f114"),
    "synthetic-en": ("synthetic", "en", "8486e0d8f7ab74a9672eeff52dbff27dd510353df9444d18bf69ab729abf8f4a"),
}


def wav_bytes(pcm: bytes) -> bytes:
    require(len(pcm) % 2 == 0, "invalid_pcm")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)
        audio.writeframes(pcm)
    return output.getvalue()


def fingerprint(payload: bytes) -> str:
    count = (len(payload) + PART_BYTES - 1) // PART_BYTES
    value = hashlib.sha256(
        f"stt-import-fingerprint-v2\0{len(payload)}\0{PART_BYTES}\0{count}\0".encode()
    )
    for offset in range(0, len(payload), PART_BYTES):
        value.update(hashlib.sha256(payload[offset:offset + PART_BYTES]).digest())
    return value.hexdigest()


def no_link_path(path: Path, root: Path) -> Path:
    absolute = path.absolute()
    require(absolute.is_relative_to(root.absolute()), "sample_path_outside_approved_root")
    for component in (absolute, *absolute.parents):
        require(not component.is_symlink() and not component.is_junction(), "sample_link_rejected")
        if component == root.absolute():
            break
    resolved = absolute.resolve(strict=True)
    require(resolved.is_relative_to(root.resolve(strict=True)), "sample_path_outside_approved_root")
    return resolved


def load_samples(root: Path) -> list[dict]:
    require(root.is_absolute(), "sample_root_must_be_absolute")
    selected = {}
    for category, subdirectory in (("public", "vllm-audit/samples"), ("synthetic", "synthetic-samples")):
        directory = no_link_path(root / subdirectory, root)
        manifest = no_link_path(directory / "manifest.json", root)
        require(manifest.stat().st_size <= 256 * 1024, "manifest_size_limit")
        rows = json.loads(manifest.read_text(encoding="utf-8"))
        require(isinstance(rows, list) and len(rows) <= 20, "manifest_shape")
        for row in rows:
            require(isinstance(row, dict), "manifest_row")
            identifier = row.get("id")
            if identifier not in APPROVED:
                continue
            expected_category, language, sha256 = APPROVED[identifier]
            require(identifier not in selected and category == expected_category and
                    row.get("language") == language and row.get("sha256") == sha256, "sample_identity")
            path = no_link_path(Path(row["path"]), directory)
            require(path.is_file() and 44 < path.stat().st_size <= MAX_MEDIA_BYTES, "sample_size_limit")
            content = path.read_bytes()
            require(hashlib.sha256(content).hexdigest() == sha256, "sample_hash")
            # All approved sources are already mono 16kHz. Reject changes instead
            # of introducing an unreviewed resampling or channel conversion.
            samples, rate = sf.read(io.BytesIO(content), dtype="int16", always_2d=True)
            require(rate == SAMPLE_RATE and samples.shape[1] == 1 and
                    SAMPLE_RATE <= len(samples) <= 30 * SAMPLE_RATE, "sample_audio_format")
            pcm = samples[:, 0].astype("<i2", copy=False).tobytes()
            require(np.any(samples), "empty_sample_audio")
            selected[identifier] = {"id": identifier, "category": category, "language": language,
                                    "pcm": pcm, "source_sha256": sha256}
    require(set(selected) == set(APPROVED), "four_approved_samples_required")
    return [selected[identifier] for identifier in APPROVED]


class LocalClient:
    def __init__(self, client):
        require(str(client.base_url).rstrip("/") == API_URL, "nonlocal_client_rejected")
        self.client = client

    def request(self, method, path, *, headers=None, body=None, content=None, expected=200, raw=False):
        require(str(self.client.base_url).rstrip("/") == API_URL, "nonlocal_client_rejected")
        require(re.fullmatch(r"/[a-zA-Z0-9_/-]*", path) is not None and
                not path.startswith("//"), "nonlocal_path_rejected")
        deadline = time.monotonic() + 300
        with self.client.stream(method, path, headers=headers, json=body, content=content) as response:
            require(response.status_code == expected, "unexpected_http_status")
            require(response.headers.get("content-encoding", "identity") in {"identity", ""}, "response_encoding")
            payload = bytearray()
            for part in response.iter_raw():
                payload.extend(part)
                require(len(payload) <= MAX_MEDIA_BYTES and time.monotonic() < deadline, "response_limit")
            if raw:
                return bytes(payload), dict(response.headers)
            try:
                return json.loads(payload) if payload else None
            except (ValueError, UnicodeError):
                raise SmokeFailure("response_json") from None

    def login(self, account):
        result = self.request("POST", "/auth/login", body=account)
        token = result.get("token")
        require(isinstance(token, str) and token.isascii() and 20 <= len(token) <= 128, "login_token")
        require(result.get("user", {}).get("username") == account["username"], "login_identity")
        return {"Authorization": "Bearer " + token}


def read_segments(lecture):
    segments = lecture.get("segments")
    require(isinstance(segments, list), "segments_shape")
    identifiers = []
    for row in segments:
        require(isinstance(row, dict) and isinstance(row.get("text"), str) and
                isinstance(row.get("id"), str), "segment_shape")
        begin, end = row.get("start"), row.get("end")
        require(isinstance(begin, (int, float)) and isinstance(end, (int, float)) and
                math.isfinite(begin) and math.isfinite(end) and 0 <= begin < end, "segment_timeline")
        identifiers.append(row["id"])
    require(len(identifiers) == len(set(identifiers)), "duplicate_segment_ids")
    return segments


def segment_stats(lecture, language):
    rows = read_segments(lecture)
    text = "".join(row["text"] for row in rows)
    require(bool(text.strip()), "actual_transcript_required")
    require(bool(re.search("[가-힣]" if language == "ko" else "[A-Za-z]", text)), "transcript_language")
    return {"segments": len(rows), "characters": len(text)}


def ensure_isolation(api, owner):
    state = api.request("GET", "/status", headers=owner)
    # API status is deliberately a nonblocking cached probe. A freshly started
    # model can be ready while the API still reports its preceding state.
    deadline = time.monotonic() + 10
    while state.get("model_state") != "ready" and time.monotonic() < deadline:
        time.sleep(.25)
        state = api.request("GET", "/status", headers=owner)
    require(state.get("model_state") == "ready", "qwen_model_ready_required")
    require(state.get("transcription_providers", {}).get("clova", {}).get("configured") is False, "clova_disabled")
    for name in ("postprocessing", "summarization", "translation", "question_answering", "study_notes"):
        require(state.get(name, {}).get("configured") is False, "paid_providers_disabled")
    overview = api.request("GET", "/admin/overview", headers=owner)
    for name in ("drive", "backup"):
        require(overview.get(name, {}).get("enabled") is False and
                overview.get(name, {}).get("configured") is False, "external_storage_disabled")
    require(overview.get("tunnel", {}).get("restart_available") is False, "tunnel_disabled")


def new_import(api, owner, sample, payload):
    identifier = str(uuid.uuid4())
    body = {"title": "Windows approved audio integration sample", "language": sample["language"],
            "filename": "approved-sample.wav", "size": len(payload), "file_fingerprint": fingerprint(payload)}
    headers = {**owner, "X-Import-Id": identifier}
    row = api.request("POST", "/imports", expected=201, headers=headers, body=body)
    require(row.get("id") == identifier and row.get("part_bytes") == PART_BYTES and
            row.get("status") == "uploading", "import_creation")
    replay = api.request("POST", "/imports", expected=201, headers=headers, body=body)
    require(replay.get("id") == identifier and replay.get("uploaded_bytes") == 0, "import_creation_replay")
    return row


def upload_part(api, owner, identifier, part, offset, *, expected=200):
    return api.request("PUT", f"/imports/{identifier}", expected=expected, content=part,
                       headers={**owner, "Content-Type": "application/octet-stream",
                                "X-Upload-Offset": str(offset), "X-Part-SHA256": hashlib.sha256(part).hexdigest()})


def cancelled_upload(api, owner, peer, sample):
    # Add silence only in this throw-away transfer test: it is never completed
    # or transcribed. This guarantees the API's 480KiB non-final part contract.
    pcm = sample["pcm"] + bytes(max(0, 16 * SAMPLE_RATE * 2 - len(sample["pcm"])))
    payload = wav_bytes(pcm)
    row = new_import(api, owner, sample, payload)
    identifier = row["id"]
    try:
        api.request("GET", f"/imports/{identifier}", headers=peer, expected=404)
        upload_part(api, peer, identifier, payload[:PART_BYTES], 0, expected=404)
        first = upload_part(api, owner, identifier, payload[:PART_BYTES], 0)
        require(first.get("next_offset") == PART_BYTES, "partial_upload_offset")
        replay = upload_part(api, owner, identifier, payload[:PART_BYTES], 0)
        require(replay.get("uploaded_bytes") == PART_BYTES, "part_replay_did_not_append")
        changed = bytes([payload[0] ^ 1]) + payload[1:PART_BYTES]
        upload_part(api, owner, identifier, changed, 0, expected=409)
        upload_part(api, owner, identifier, payload[PART_BYTES:], PART_BYTES + 1, expected=409)
        api.request("POST", f"/imports/{identifier}/complete", headers=owner, expected=409)
    finally:
        cancelled = api.request("POST", f"/imports/{identifier}/cancel", headers=owner)
        require(cancelled.get("status") == "cancelled" and cancelled.get("raw_deleted") is True and
                cancelled.get("lecture_id") is None, "cancel_deleted_only_own_raw_upload")
    upload_part(api, owner, identifier, payload[:PART_BYTES], 0, expected=409)
    return {"partial_upload_replay_conflict_cancel": True}


def downloaded_wav(api, owner, peer, lecture_id, expected_pcm):
    api.request("POST", f"/lectures/{lecture_id}/recording-download-ticket", headers=peer, expected=404)
    ticket = api.request("POST", f"/lectures/{lecture_id}/recording-download-ticket", headers=owner)["path"]
    require(re.fullmatch(r"/recording-downloads/[A-Za-z0-9_-]{32,80}", ticket) is not None, "ticket_path")
    content, headers = api.request("GET", ticket, raw=True)
    require(headers.get("cache-control") == "no-store" and headers.get("x-content-type-options") == "nosniff",
            "private_download_headers")
    with wave.open(io.BytesIO(content), "rb") as audio:
        require((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, SAMPLE_RATE),
                "download_wav_format")
        require(audio.readframes(audio.getnframes()) == expected_pcm, "download_pcm_exact_no_duplicates_or_tail_loss")
    prefix, partial_headers = api.request("GET", ticket, raw=True, expected=206, headers={"Range": "bytes=0-43"})
    require(prefix == content[:44] and partial_headers.get("content-range") == f"bytes 0-43/{len(content)}",
            "download_prefix_range")
    suffix, _ = api.request("GET", ticket, raw=True, expected=206, headers={"Range": "bytes=-32"})
    require(suffix == content[-32:], "download_suffix_range")
    api.request("GET", ticket, raw=True, expected=416, headers={"Range": f"bytes={len(content)}-"})
    return {"wav_bytes": len(content), "wav_sha256": hashlib.sha256(content).hexdigest()}


def completed_import(api, sessions, accounts, sample, *, resume=False):
    owner, peer = sessions
    # Only the first completed import receives trailing silence so actual
    # partial-byte/new-login resume is checked. Original speech stays intact.
    pcm = sample["pcm"]
    if resume:
        pcm += bytes(max(0, 16 * SAMPLE_RATE * 2 - len(pcm)))
    payload = wav_bytes(pcm)
    row = new_import(api, owner, sample, payload)
    identifier = row["id"]
    started = time.monotonic()
    completed = False
    try:
        first = upload_part(api, owner, identifier, payload[:PART_BYTES], 0)
        next_offset = first.get("next_offset")
        require(next_offset == min(len(payload), PART_BYTES), "import_first_offset")
        if resume:
            require(next_offset < len(payload), "true_partial_upload_required")
            api.request("POST", "/auth/logout", headers=owner)
            api.request("GET", f"/imports/{identifier}", headers=owner, expected=401)
            sessions[0] = owner = api.login(accounts[0])
            resumed = api.request("GET", f"/imports/{identifier}", headers=owner)
            require(resumed.get("next_offset") == next_offset, "new_session_resume_offset")
            upload_part(api, owner, identifier, payload[:PART_BYTES], 0)
        for offset in range(next_offset, len(payload), PART_BYTES):
            uploaded = upload_part(api, owner, identifier, payload[offset:offset + PART_BYTES], offset)
            require(uploaded.get("next_offset") == min(offset + PART_BYTES, len(payload)), "import_next_offset")
        api.request("POST", f"/imports/{identifier}/complete", headers=owner)
        # This is a contract replay, not an inference retry.
        api.request("POST", f"/imports/{identifier}/complete", headers=owner)
        deadline = time.monotonic() + 300
        while True:
            row = api.request("GET", f"/imports/{identifier}", headers=owner)
            if row.get("status") not in {"queued", "processing"}:
                break
            require(time.monotonic() < deadline, "import_completion_deadline")
            time.sleep(.25)
        require(row.get("status") == "completed" and row.get("raw_deleted") is True, "actual_import_completed")
        completed = True
        lecture_id = row["lecture_id"]
        require(isinstance(lecture_id, str) and str(uuid.UUID(lecture_id)) == lecture_id, "import_lecture_identity")
        api.request("GET", f"/lectures/{lecture_id}", headers=peer, expected=404)
        lecture = api.request("GET", f"/lectures/{lecture_id}", headers=owner)
        require(lecture.get("recording_finalized") is True, "import_recording_finalized")
        stats = segment_stats(lecture, sample["language"])
        download = downloaded_wav(api, owner, peer, lecture_id, pcm)
        # Completion remains stable after inference and download.
        again = api.request("POST", f"/imports/{identifier}/complete", headers=owner)
        require(again.get("status") == "completed" and again.get("lecture_id") == lecture_id, "import_terminal_replay")
        require(read_segments(api.request("GET", f"/lectures/{lecture_id}", headers=owner)) == read_segments(lecture),
                "import_replay_no_duplicate_segments")
        return {"import_seconds": round(time.monotonic() - started, 3), "new_session_resumed": resume, **stats, **download}
    finally:
        if not completed:
            api.request("POST", f"/imports/{identifier}/cancel", headers=owner)


def chunk_plan(pcm):
    """Non-final sequential chunks, at most 12s payload and 3s overlap."""
    frames = len(pcm) // 2
    previous_end = 0
    while previous_end < frames:
        begin = max(0, previous_end - 3 * SAMPLE_RATE)
        # For short samples exercise two chunks; for longer ones stay under
        # both the 512KiB upload bound and ordinary browser chunk duration.
        if previous_end == 0:
            end = min(frames, max(SAMPLE_RATE, min(9 * SAMPLE_RATE, frames // 2)))
        else:
            end = min(frames, previous_end + 9 * SAMPLE_RATE)
        yield begin, previous_end - begin, pcm[begin * 2:end * 2]
        previous_end = end


def chunks_and_tail(api, owner, peer, sample):
    lecture_id = str(uuid.uuid4())
    api.request("POST", "/lectures", expected=201, headers={**owner, "X-Lecture-Id": lecture_id},
                body={"title": "Windows approved chunk and final tail sample", "language": sample["language"],
                      "asr_provider": "qwen"})
    started = time.monotonic()
    count = 0
    for begin, overlap, pcm in chunk_plan(sample["pcm"]):
        chunk_id = str(uuid.uuid4())
        payload = wav_bytes(pcm)
        headers = {**owner, "Content-Type": "audio/wav", "X-Chunk-Id": chunk_id,
                   "X-Start-Seconds": str(begin / SAMPLE_RATE), "X-Overlap-Seconds": str(overlap / SAMPLE_RATE),
                   "X-Final-Chunk": "false"}
        api.request("POST", f"/lectures/{lecture_id}/chunks", headers={**headers, **peer}, content=payload, expected=404)
        response = api.request("POST", f"/lectures/{lecture_id}/chunks", headers=headers, content=payload)
        replay = api.request("POST", f"/lectures/{lecture_id}/chunks", headers=headers, content=payload)
        require(read_segments(response) == read_segments(replay), "chunk_retry_stable_segments")
        recovery_headers = {**headers, "X-Chunk-Payload-SHA256": hashlib.sha256(payload).hexdigest()}
        recovered = api.request("GET", f"/lectures/{lecture_id}/chunks/{chunk_id}/result", headers=recovery_headers)
        require(recovered.get("state") == "done" and read_segments(recovered["result"]) == read_segments(response),
                "chunk_ack_recovery_stable")
        changed = payload[:-2] + bytes([payload[-2] ^ 1, payload[-1]])
        api.request("POST", f"/lectures/{lecture_id}/chunks", headers=headers, content=changed, expected=409)
        count += 1
    before = api.request("GET", f"/lectures/{lecture_id}", headers=owner)
    require(before.get("recording_finalized") is False, "nonfinal_recording_open")
    api.request("POST", f"/lectures/{lecture_id}/recording-download-ticket", headers=owner, expected=409)
    api.request("POST", f"/lectures/{lecture_id}/recording-finalize", headers=peer, expected=404)
    finalized = api.request("POST", f"/lectures/{lecture_id}/recording-finalize", headers=owner)
    require(finalized.get("recording_finalized") is True, "guard_tail_finalized")
    replay = api.request("POST", f"/lectures/{lecture_id}/recording-finalize", headers=owner)
    require(read_segments(finalized) == read_segments(replay), "guard_tail_replay_stable")
    after = api.request("GET", f"/lectures/{lecture_id}", headers=owner)
    all_ids = {row["id"] for row in read_segments(after)}
    require(all(row["id"] in all_ids for row in read_segments(before) + read_segments(finalized)),
            "guard_keeps_committed_and_tail_segments")
    download = downloaded_wav(api, owner, peer, lecture_id, sample["pcm"])
    stats = segment_stats(after, sample["language"])
    return lecture_id, {"chunk_seconds": round(time.monotonic() - started, 3), "chunks": count,
                        "tail_segments": len(read_segments(finalized)), **stats, **download}


def audio_checks(api, profile, samples):
    sessions = []
    summaries = []
    try:
        for account in profile["accounts"]:
            sessions.append(api.login(account))
        ensure_isolation(api, sessions[0])
        # Never cancel or adopt another unfinished synthetic test run.
        for session in sessions:
            jobs = api.request("GET", "/imports", headers=session)
            require(all(row.get("status") not in {"uploading", "queued", "processing"} for row in jobs),
                    "existing_active_test_import_requires_review")
        cancellation = cancelled_upload(api, *sessions, samples[0])
        final_lecture = None
        for index, sample in enumerate(samples):
            imports = completed_import(api, sessions, profile["accounts"], sample, resume=index == 0)
            final_lecture, chunks = chunks_and_tail(api, *sessions, sample)
            summaries.append({"sample": sample["id"], "language": sample["language"],
                              "category": sample["category"], "source_sha256": sample["source_sha256"],
                              "import": imports, "chunks": chunks})
        ticket = api.request("POST", f"/lectures/{final_lecture}/recording-download-ticket", headers=sessions[0])["path"]
        api.request("POST", "/auth/logout", headers=sessions[0])
        api.request("GET", "/auth/me", headers=sessions[0], expected=401)
        api.request("GET", ticket, raw=True, expected=404)
        sessions.pop(0)
        return {"profile": PROFILE_KIND, "sample_count": len(samples), "samples": summaries,
                "checks": {**cancellation, "new_login_upload_resume": True, "import_completion_replay": True,
                           "cross_account_access_denied": True, "chunk_retry_and_ack_recovery": True,
                           "guard_finalize_replay": True, "normalized_wav_exact": True,
                           "byte_ranges": True, "logout_revoked_download": True},
                "synthetic_audio_lectures_retained": len(samples) * 2,
                "not_checked": ["process_crash_mid_inference", "long_run_stability", "paid_providers",
                                "production_drive", "live_microphone_capture", "semantic_accuracy"] }
    finally:
        already_failing = sys.exc_info()[0] is not None
        cleanup_failed = False
        for session in sessions:
            try:
                api.request("POST", "/auth/logout", headers=session)
            except Exception:
                cleanup_failed = True
        if cleanup_failed and not already_failing:
            raise SmokeFailure("session_cleanup")


def run(samples):
    profile = read_profile()
    verify_test_database(profile)
    managed = WindowsAPIController()
    record = managed.record()
    require(record is not None and managed.matching(record), "managed_test_api_required")
    require(managed.request(record, "GET", "/health").get("status") == "ok", "private_api_identity")
    model = WindowsModelController(MODEL_RUNTIME, port=MODEL_PORT)
    model_record = model.record()
    require(model_record is not None and model.matching(model_record), "managed_model_required")
    require(model.health(model_record).get("model_state") == "ready", "managed_model_ready_required")
    with httpx.Client(base_url=API_URL, trust_env=False, follow_redirects=False,
                      timeout=httpx.Timeout(300, connect=5, write=15, pool=5),
                      headers={"Accept-Encoding": "identity"}, limits=httpx.Limits(max_connections=1)) as client:
        result = audio_checks(LocalClient(client), profile, samples)
    require(managed.matching(record) and managed.request(record, "GET", "/health").get("status") == "ok",
            "api_remained_running")
    require(model.matching(model_record), "model_remained_running")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples-root", type=Path, default=DEFAULT_SAMPLES_ROOT)
    parser.add_argument("--run", action="store_true", help="Transcribe approved audio on an already-running local model.")
    args = parser.parse_args(argv)
    try:
        samples = load_samples(args.samples_root)
        if args.run:
            result = run(samples)
        else:
            result = {"preflight_only": True, "samples": [
                {"sample": row["id"], "category": row["category"], "language": row["language"],
                 "seconds": round(len(row["pcm"]) / (SAMPLE_RATE * 2), 3)} for row in samples],
                "transcription_performed": False}
    except SmokeFailure as failure:
        print(json.dumps({"ok": False, "failed_check": str(failure)}))
        return 1
    except Exception:
        print(json.dumps({"ok": False, "failed_check": "approved_samples_or_managed_test_service_unavailable"}))
        return 1
    print(json.dumps({"ok": True, **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
