"""Repeatable API smoke checks for this project's private synthetic profile.

Run after `python -m server.windows_local start --api-only`; add
`--expect-model-offline` to require the independence check. Credentials, account
IDs, session tokens, lecture IDs and response contents are never printed. This
creates/reuses two empty synthetic lectures; it does not transcribe audio or call
CLOVA, LLM, Drive, backups, tunnel controls or deployment services.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

import httpx

from .windows_local import (API_PORT, CONTROL_PREFIX, MODEL_PORT, MODEL_RUNTIME, PROFILE_KIND,
                            WindowsAPIController, read_profile, verify_test_database)
from .win_model_process import WindowsModelController

API_URL = f"http://127.0.0.1:{API_PORT}"
MAX_RESPONSE = 2 * 1024 * 1024


class SmokeFailure(RuntimeError):
    """Contains only a fixed check name, never a server response or credential."""


def require(condition, code):
    if not condition:
        raise SmokeFailure(code)


def request(client, method, path, *, expected=200, headers=None, body=None):
    require(str(client.base_url).rstrip("/") == API_URL, "nonlocal_client_rejected")
    require(path.startswith("/") and not path.startswith("//") and ":" not in path, "nonlocal_path_rejected")
    deadline = time.monotonic() + 10
    with client.stream(method, path, headers=headers, json=body) as response:
        require(response.status_code == expected, "unexpected_http_status")
        require(response.headers.get("content-encoding", "identity") in {"identity", ""}, "unexpected_response_encoding")
        payload = bytearray()
        for part in response.iter_raw():
            payload.extend(part)
            require(len(payload) <= MAX_RESPONSE and time.monotonic() <= deadline, "response_limit")
        try:
            return json.loads(payload) if payload else None
        except (ValueError, UnicodeError):
            raise SmokeFailure("response_json") from None


def api_checks(client, profile, *, model_offline):
    passed = []
    sessions = []
    try:
        require(request(client, "GET", "/health") == {"status": "ok"}, "public_health")
        request(client, "GET", "/auth/me", expected=401)
        request(client, "GET", "/lectures", expected=401)
        request(client, "GET", "/health", expected=403, headers={"Origin": "https://external.invalid"})
        passed.append("public_health_and_access_boundaries")
        for account in profile["accounts"]:
            result = request(client, "POST", "/auth/login", body={"username": account["username"], "password": account["password"]})
            token = result.get("token")
            require(isinstance(token, str) and 20 <= len(token) <= 128 and token.isascii(), "login_token")
            headers = {"Authorization": "Bearer " + token}
            sessions.append(headers)
            require(result.get("user", {}).get("username") == account["username"], "login_identity")
            require(request(client, "GET", "/auth/me", headers=headers).get("username") == account["username"], "session_identity")
        passed.append("two_synthetic_account_logins")

        ids = [str(uuid.uuid5(uuid.UUID(profile["id"]), f"windows-local-smoke-account-{index}")) for index in (0, 1)]
        payload = {"title": "Windows local smoke (synthetic empty lecture)", "language": "ko", "asr_provider": "qwen"}
        for index, headers in enumerate(sessions):
            creation_headers = {**headers, "X-Lecture-Id": ids[index]}
            for _ in range(2):
                created = request(client, "POST", "/lectures", expected=201, headers=creation_headers, body=payload)
                require(created.get("id") == ids[index], "creation_identity")
            own = request(client, "GET", f"/lectures/{ids[index]}", headers=headers)
            require(own.get("id") == ids[index] and own.get("segments") == [], "synthetic_lecture_content")
        # Both records now exist; test denied access in each direction.
        for index, headers in enumerate(sessions):
            request(client, "GET", f"/lectures/{ids[1-index]}", expected=404, headers=headers)
            records = request(client, "GET", "/lectures", headers=headers)
            require(isinstance(records, list), "lecture_list")
            require(sum(row.get("id") == ids[index] for row in records) == 1
                    and all(row.get("id") != ids[1-index] for row in records), "lecture_ownership_and_idempotence")
        passed.extend(["lecture_create_read_and_replay", "cross_account_access_denied"])

        state = request(client, "GET", "/status", headers=sessions[0])
        require(state.get("transcription_providers", {}).get("clova", {}).get("configured") is False, "clova_disabled")
        for name in ("postprocessing", "summarization", "translation", "question_answering", "study_notes"):
            require(state.get(name, {}).get("configured") is False, "paid_providers_disabled")
        overview = request(client, "GET", "/admin/overview", headers=sessions[0])
        request(client, "GET", "/admin/overview", expected=403, headers=sessions[1])
        require(overview.get("drive", {}).get("enabled") is False
                and overview.get("drive", {}).get("configured") is False, "drive_disabled")
        require(overview.get("backup", {}).get("enabled") is False
                and overview.get("backup", {}).get("configured") is False, "backups_disabled")
        require(overview.get("tunnel", {}).get("restart_available") is False, "tunnel_disabled")
        passed.append("administrator_and_disabled_external_services")

        request(client, "POST", CONTROL_PREFIX + "/shutdown", expected=401, headers=sessions[0])
        require(request(client, "GET", "/health") == {"status": "ok"}, "unauthenticated_shutdown_denied")
        passed.append("user_session_cannot_shutdown_api")

        if model_offline:
            deadline = time.monotonic() + 4
            while state.get("model_state") != "offline" and time.monotonic() < deadline:
                time.sleep(.1)
                state = request(client, "GET", "/status", headers=sessions[0])
            require(state.get("model_state") == "offline", "model_offline_status")
            # Login and persisted class reads above all occurred while stopped.
            require(request(client, "GET", f"/lectures/{ids[0]}", headers=sessions[0]).get("id") == ids[0], "offline_lecture_read")
            passed.append("model_offline_login_and_lecture_read")
        return {"profile": PROFILE_KIND, "passed": passed, "model_offline_verified": model_offline,
                "synthetic_lectures_retained": 2,
                "not_checked": ["audio_upload_and_download", "real_transcription", "long_run_stability"]}
    finally:
        check_was_failing = sys.exc_info()[0] is not None
        cleanup_failed = False
        for headers in sessions:
            try:
                request(client, "POST", "/auth/logout", headers=headers)
            except (SmokeFailure, httpx.HTTPError, OSError, ValueError):
                cleanup_failed = True
        if cleanup_failed and not check_was_failing:
            raise SmokeFailure("session_cleanup")


def run_smoke(*, expect_model_offline=False):
    profile = read_profile()
    verify_test_database(profile)
    api = WindowsAPIController()
    record = api.record()
    require(record is not None and api.matching(record), "managed_test_api_required")
    require(api.request(record, "GET", "/health").get("status") == "ok", "private_api_identity")
    model = WindowsModelController(MODEL_RUNTIME, port=MODEL_PORT)
    offline = not model.status()["running"]
    require(not expect_model_offline or offline, "model_must_already_be_stopped")
    with httpx.Client(base_url=API_URL, trust_env=False, follow_redirects=False, timeout=5,
                      headers={"Accept-Encoding": "identity"},
                      limits=httpx.Limits(max_connections=1)) as client:
        result = api_checks(client, profile, model_offline=offline)
    require(api.matching(record) and api.request(record, "GET", "/health").get("status") == "ok", "api_remained_running")
    if offline:
        require(not model.status()["running"], "model_remained_stopped")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-model-offline", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_smoke(expect_model_offline=args.expect_model_offline)
    except SmokeFailure as failure:
        print(json.dumps({"ok": False, "failed_check": str(failure)}))
        return 1
    except Exception:
        print(json.dumps({"ok": False, "failed_check": "private_profile_or_local_api_unavailable"}))
        return 1
    print(json.dumps({"ok": True, **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
