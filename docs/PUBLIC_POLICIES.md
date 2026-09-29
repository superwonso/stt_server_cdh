# Public service policies

Published documents:

- https://superwonso.github.io/stt_server_cdh/privacy.html
- https://superwonso.github.io/stt_server_cdh/terms.html

Use these same URLs in Google Auth Platform → Branding. Both documents are
standalone static HTML, available without JavaScript, login, a working API,
or a Cloudflare tunnel. The shared footer opens them in a separate tab so an
active recording is not navigated away from. No consent, OAuth scope, retention
setting, paid call, or account behavior is changed by this release.

The operator expressly supplied the public name and contact email used in the
documents. No private deployment credentials, account lists, recordings, or
database content are included.

## Implementation basis

- `server/settings.py`: Drive integration, fixed NOVA gateway, model defaults.
- `server/drive_storage.py`: Google `user(permissionId)`, restricted file access,
  and remote deletion by setting `trashed: true`.
- `server/drive_archive.py`: username-based remote folder names and verified
  upload before local cleanup.
- `server/recovery_backup.py`: encrypted recovery bundles, including database
  and configuration; no automatic age-based retention deletion.
- `server/material_service.py`, `server/manual_notes.py`,
  `server/review_service.py`: deleting an item is not a purge of every derived
  note, edit history, undo snapshot, or source-key record.
- `server/review_parse.py`: explicitly submitted timetable text/image sent to
  NOVA; input is not written as a server-side file.
- `web/auth-session.js`: tab session storage, capped at 24 hours and the server
  expiry; browser recovery storage is separate from logout.
- `web/index.html`, `OPERATIONS.md`: existing CLOVA and local audio disclosures.

## Provider and operating details requiring ongoing confirmation

Do not turn these documents into stronger claims without verifying the facts:

- Provider processing countries and exact upstream retention depend on the
  operator's contracts/configuration. They have not been individually verified.
  The page discloses the uncertainty instead of inventing a country or period.
  Verify these details, applicable cross-border notices/consent requirements,
  and the downstream model processor before treating the policy as a complete
  jurisdiction-specific compliance review.
- CLOVA Object Storage behavior depends on the cloud domain/provider setup;
  the app does not itself purge that storage. The document describes possible
  external copies and keeps the existing in-product disclosure consistent.
- Existing backups are not automatically purged with a live record. Full
  deletion requests require an operator to check those copies and histories.
- Publishing the pages does not prove Google domain ownership or complete
  OAuth brand verification. Do not submit a verification application or change
  Google configuration as part of the static publication itself.

## Official references checked for the initial version

- [Google OAuth branding](https://support.google.com/cloud/answer/15549049)
- [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy)
- [Google Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [GitHub Pages visitor data](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)
- [Cloudflare privacy policy](https://www.cloudflare.com/privacypolicy/)
- [Google privacy policy](https://policies.google.com/privacy?hl=ko)
- [Korean Personal Information Protection Act, Article 30](https://www.law.go.kr/법령/개인정보보호법/제30조)

Initial effective date: 2026-09-29. Update the public effective date and change
description whenever the actual processing or commitments materially change.
