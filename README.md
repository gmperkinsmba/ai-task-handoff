# AI Task Handoff

Save a task's goal, progress, checks, and next steps for another AI session.

**Install and run on macOS**

The native app opens as a local Mac window. No browser tab or manual server start is needed. In this source folder, build and install it with:

```sh
python3.12 -m venv .desktop-venv
.desktop-venv/bin/python -m pip install -r requirements-desktop.lock.txt
.desktop-venv/bin/python build_desktop.py
.desktop-venv/bin/python install_desktop.py
```

Then open `~/Applications/AI Task Handoff.app` in Finder. Use File → Quit AI Task Handoff or close the window to stop the local listener. To remove only the marked app copy, run `.desktop-venv/bin/python uninstall_desktop.py`. Local current and approved handoffs remain in `~/.local/share/context-handoff` until reset in Settings.

## Browser development

Requires Python 3.10 or newer. The app uses only the Python standard library and SQLite.

```bash
python3 server.py
```

Open <http://127.0.0.1:8347>. State is stored in `~/.local/share/context-handoff/handoffs.sqlite3`. Set `CONTEXT_HANDOFF_DATA` before starting the server to choose a different data directory.

## Workflow

Edit the draft and save it. A task stays in progress if a check is pending, running, failed, skipped, or lacks evidence. It also stays in progress while a question is open. Compare a resumed JSON handoff with the last approved snapshot before you load it. Review and redact sections in the export preview. Then approve and download the file.

The fictional sample has one pending check and one open question. It cannot appear complete. The sample also has a changed file entry compared with the first approved snapshot. This shows how resume comparison works.

## Format and limits

The manual adapter accepts the existing Context Handoff JSON schema version 1. It has no provider connections or automatic session starts. It rejects transcripts and credential fields. Export preview and resume comparison need review. The user must accept each operation. When you redact a question, its open or resolved state stays visible. A hidden open question cannot make the handoff appear complete.

The server binds to loopback. It checks Host, Origin, and a per-process action token. Use it locally, not on a shared network.

## Verify

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile server.py tests/test_app.py
```

MIT licensed. See `LICENSE`.

For browser development, use the command and local address above. The native app targets macOS; other operating systems have not been tested.

The app is now named AI Task Handoff. Existing local data remains in `~/.local/share/context-handoff`.
