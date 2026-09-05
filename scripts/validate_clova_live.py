#!/usr/bin/env python3
"""Opt-in, sub-five-minute real CLOVA test with public FLEURS audio only.

No production account, lecture, DB, recording archive, or browser is changed.
This sends public evaluation audio to the configured paid CLOVA domain.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from server.clova_transcriber import ClovaStreamingTranscriber, ClovaTranscriptionError
from server.settings import Settings
from scripts.validate_chunk_pipeline import normalize, edit_distance, best_suffix_cer, repeated_ngram_excess, current_rss_bytes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="explicitly authorize billable real CLOVA requests")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; this test uses the configured CLOVA domain")
    folder = ROOT / ".samples/fleurs/ko_kr"
    references = dict(line.split(maxsplit=1) for line in (folder / "ko_kr.trans.txt").read_text().splitlines())
    corpus = []
    for path in sorted(folder.glob("ko_kr_*.wav"))[:10]:
        audio, rate = sf.read(path, dtype="float32")
        assert rate == 16000 and audio.ndim == 1
        corpus.append((audio, references[path.stem]))
    assert corpus
    portions, source_texts, used, index = [], [], 0, 0
    # End on a complete utterance, not an arbitrarily cut syllable.
    while True:
        audio, text = corpus[index % len(corpus)]
        if used + len(audio) + 4000 > 263 * 16000:
            break
        portions.extend((audio, np.zeros(4000, dtype=np.float32)))
        source_texts.append(text); used += len(audio) + 4000; index += 1
    assert used >= 245 * 16000
    audio = np.concatenate(portions)
    # A real quiet section tests silence acknowledgements without truncating
    # speech or changing the wall-clock duration of the recognition session.
    silence_start = 128000
    audio = np.concatenate((audio[:silence_start], np.zeros(160000, dtype=np.float32), audio[silence_start:]))
    settings = replace(Settings.from_env(), clova_stream_response_timeout_seconds=10,
                       clova_stream_max_age_seconds=240)
    engine = ClovaStreamingTranscriber(settings)
    assert engine.configured
    make_transport = engine._make_transport
    origin = time.monotonic(); transports = []
    def tracked_transport():
        transports.append(round(time.monotonic() - origin, 3))
        return make_transport()
    engine._make_transport = tracked_transport
    username, lecture_id = "validation-user", str(uuid.uuid4())
    owner = (username, lecture_id)
    texts, rss_values, processing = [], [current_rss_bytes()], []
    position, chunks, repeated_checked = 0, 0, False
    last_progress, intentional_reconnect = 0, False
    try:
        while position < len(audio):
            if time.monotonic() - origin > 287:
                raise RuntimeError("bounded deadline reached")
            due = origin + position / 16000
            while time.monotonic() < due:
                time.sleep(min(.25, due - time.monotonic()))
            overlap = min(position, 48000)
            end = min(len(audio), position + 128000)
            samples = audio[position - overlap:end]
            final = end == len(audio)
            # Disconnect only this diagnostic client's already-acknowledged
            # stream, after naturally crossing the four-minute rotation.
            if len(transports) >= 2 and position / 16000 >= 256 and not intentional_reconnect:
                with engine._state_lock:
                    session = engine._sessions.get(owner)
                if session is not None and session.channel is not None:
                    session.channel.close()
                    time.sleep(.3)
                    intentional_reconnect = True
            kwargs = dict(lecture_id=lecture_id, username=username,
                          start_seconds=(position-overlap)/16000,
                          payload_hash=hashlib.sha256(samples.tobytes()).hexdigest())
            began = time.monotonic()
            result = engine.transcribe(samples, "ko", overlap/16000, final, **kwargs)
            processing.append(time.monotonic()-began)
            if not repeated_checked:
                assert engine.transcribe(samples, "ko", overlap/16000, final, **kwargs) == result
                repeated_checked = True
            texts.extend(item["text"] for item in result)
            rss_values.append(current_rss_bytes()); position = end; chunks += 1
            elapsed = time.monotonic()-origin
            if elapsed-last_progress >= 30:
                print(json.dumps({"event":"progress", "wall_seconds":round(elapsed,1),
                                  "audio_seconds":round(position/16000,1),"chunks":chunks,
                                  "native_streams":len(transports)}), flush=True)
                last_progress = elapsed
        reference, hypothesis = " ".join(source_texts), " ".join(texts)
        tail_cer, _ = best_suffix_cer(source_texts[-1], hypothesis)
        normalized = normalize(reference)
        active_after_final = engine.status()["active_sessions"]
        report = {"event":"summary", "wall_seconds":round(time.monotonic()-origin,3),
                  "audio_seconds":round(len(audio)/16000,3), "chunks":chunks,
                  "native_stream_open_seconds":transports,
                  "natural_rotation_observed":len(transports)>=2 and transports[1]>=240,
                  "intentional_reconnect":intentional_reconnect,
                  "cached_retry_matches":repeated_checked, "active_sessions_after_final":active_after_final,
                  "cer":round(edit_distance(normalized, normalize(hypothesis))/max(1,len(normalized)),4),
                  "tail_cer":round(tail_cer,4),
                  "excess_repeated_8grams":repeated_ngram_excess(reference,hypothesis),
                  "rss_growth_mib":round((rss_values[-1]-rss_values[0])/2**20,3),
                  "rss_peak_mib":round(max(rss_values)/2**20,3),
                  "provider_processing_seconds":round(sum(processing),3)}
        assert report["natural_rotation_observed"] and active_after_final == 0
    finally:
        engine.close()
    report["reader_threads_after_close"] = sum(t.name == "clova-response-reader" for t in threading.enumerate())
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # No gateway diagnostics, credentials, endpoints, or audio text.
        print(json.dumps({"event":"failed", "error_type":type(error).__name__,
                          "code":error.code if isinstance(error,ClovaTranscriptionError) else "validation_failed"}), flush=True)
        raise SystemExit(1) from None
