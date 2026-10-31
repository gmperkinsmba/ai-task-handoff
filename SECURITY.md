# Security

AI Task Handoff stores task notes, checks, and next steps in a local database. Its runtime folder, database, and SQLite sidecars are checked for owner-only permissions; startup fails if those checks do not pass. Saved notes are plaintext. Do not include passwords, tokens, or private client details. SHA-256 would not provide secrecy. Use made-up task text in reports.

The native app has an ad-hoc code signature. This lets the installer check whether its files changed. It does not prove who made the app. The build is not notarized. Use your Mac’s disk encryption to protect saved files.

Report security issues through [GitHub private vulnerability reporting](https://github.com/gmperkinsmba/ai-task-handoff/security/advisories/new).
