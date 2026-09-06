#!/usr/bin/env python3
"""Explicit local-operator CLI. No API calls and no production restore action."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.recovery_backup import BackupError, RecoveryBackupManager, verify_recovery_archive
from server.settings import Settings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Encrypted recovery backups to the selected Windows D drive.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Generate a private identity and public recipient; does not enable backups.")
    commands.add_parser("enable", help="Enable the server's once-daily encrypted backup scheduler.")
    commands.add_parser("disable", help="Disable future scheduled jobs without deleting any backups.")
    commands.add_parser("export", help="Encrypt and copy one bundle; retry a pending bundle if present.")
    commands.add_parser("status", help="Print status counts without paths, accounts or secrets.")
    verify = commands.add_parser("verify", help="Decrypt and verify into a NEW private /tmp directory only.")
    verify.add_argument("archive", type=Path)
    verify.add_argument("--identity", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            result = verify_recovery_archive(args.archive, args.identity)
        else:
            manager = RecoveryBackupManager(Settings.from_env())
            if args.command == "init":
                result = manager.initialize()
            elif args.command == "export":
                result = manager.export()
            elif args.command in {"enable", "disable"}:
                result = manager.set_enabled(args.command == "enable")
            else:
                result = manager.status()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        code = error.code if isinstance(error, BackupError) else "operation_failed"
        print(json.dumps({"ok": False, "error_code": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
