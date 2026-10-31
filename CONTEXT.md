# Context

AI Task Handoff is a local, manual task-state keeper. Its current and last-approved snapshots are stored separately in SQLite. Approved exports update the comparison baseline only after the user reviews and approves the preview.

The schema has an objective plus decisions, files, checks, questions, and failed approaches. IDs stay strings and are unique across lists. Unknown fields are rejected. A check counts as passed only with non-empty evidence. The computed completion state also requires a non-empty objective, at least one check, and no open questions.

Resume JSON is schema-validated, diffed against the last approved snapshot, and only loaded after explicit acceptance. The preview is bound to hashes of both current and approved state, so a concurrent edit invalidates it. Export previews are bound to the current draft and similarly become invalid after an edit. Redaction is limited to defined sections and uses an immutable preview snapshot.

Redacting question text preserves each question ID and state with placeholder text, so an open question remains open and keeps completion in progress.

The app has a manual JSON adapter only. It does not access conversations, credentials, external services, or start another session. It listens on loopback and enforces Host, Origin, and per-process action-token checks.
