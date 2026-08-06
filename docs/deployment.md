# Deployment and rollback

## Compatibility contract

Production keeps these paths unchanged:

- application root: `/opt/qqbot`
- environment file: `/etc/qqbot.env`
- entrypoint: `/opt/qqbot/main.py`
- configuration source: `/opt/qqbot/config.json` (migration input)
- active configuration: `/var/lib/qqbot/config.json`
- persistent state: `/opt/qqbot/data/`
- runtime temporary files: `/opt/qqbot/data/tmp/`
- service: `qqbot.service`

## Validation

Run before every deployment:

```bash
git diff --check
./venv/bin/python -m pip install -r requirements-dev.txt
./venv/bin/ruff check . --no-cache --select E9,F524,F63,F7,F82
./venv/bin/bandit -q -r app bot deploy -x tests --severity-level high --confidence-level high
./venv/bin/python -m compileall -q app bot tests main.py
./venv/bin/python -m unittest discover -s tests -t . -v
```

The architecture regression suite verifies legacy imports, entrypoint paths,
data-file locations, dependency freeze, and maximum sizes for historical
monolith modules.

## Staged deployment

1. Push the tested commit to `main`.
2. Create a server archive with `git archive HEAD`.
3. Copy `/opt/qqbot` to an isolated `/tmp` test directory.
4. Upload changed files to the isolated copy.
5. Run compile and full tests with `/opt/qqbot/venv/bin/python`.
6. Upload the same files to `/opt/qqbot`.
7. Fetch and reset the server repository to the tested commit.
8. Install and restart `napcat.service`, then restart `qqbot.service`.
9. Confirm both services are active, the Git worktree is clean, 114 commands are
   registered, and OneBot WebSocket connects.
10. Inspect recent journal entries for exceptions and confirm NapCat message
    bodies are not present in journald.

## Rollback

If startup, connection, or behavior validation fails:

1. Stop `qqbot.service`.
2. Reset `/opt/qqbot` to the previous known-good commit, or extract the
   pre-stage archive from `/root/qqbot-backups/`.
3. Re-run compile and tests in the server virtual environment.
4. Start `qqbot.service` and confirm OneBot connectivity.
5. Keep `config.json`, `/etc/qqbot.env`, and `data/` from the current server;
   code archives intentionally do not replace runtime secrets or state.
