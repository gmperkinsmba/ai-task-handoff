# AI Task Handoff

Local app to save a task's goal, progress, checks, and next steps for another AI session.

## Run

```sh
python3 server.py
```

The Mac app is built with `build_desktop.py` and installed with `install_desktop.py`; the README gives the exact steps and the pinned requirements file.

## Test

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile server.py tests/test_app.py
```

Run the tests before committing. There is no separate lint step.

## Layout

- Core code: `server.py`
- Browser UI: `desktop.css`
- Mac app wrapper, build and install: `build_desktop.py`, `desktop-app-owner.txt`, `desktop-assets.txt`, `desktop.py`, `desktop_policy.py`, `desktop_server.py`, `install_desktop.py`, `uninstall_desktop.py`
- Installer and runtime safety checks: `install_security.py`, `runtime_security.py`
- Dependencies: `requirements-desktop.lock.txt`, `requirements-desktop.txt`
- Tests: `test_desktop.py`, `tests/`
- Docs: `CONTEXT.md`, `LICENSE`, `README.md`, `SECURITY.md`, `third_party_licenses/`

## Conventions

- `README.md` describes behaviour and limits; `CONTEXT.md` holds the domain terms and rules. Keep both in step with code changes.
- Read `SECURITY.md` before changing request handling, file access, or the installer.
- Demo data, fixtures and examples are made up. Keep them that way.
- Never commit secrets, keys, tokens, or runtime data.
- A behaviour change comes with a test.
