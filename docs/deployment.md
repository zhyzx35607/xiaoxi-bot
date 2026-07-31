# Deployment and rollback

## Compatibility contract

Production keeps these paths unchanged:

- application root: `/opt/qqbot`
- environment file: `/etc/qqbot.env`
- entrypoint: `/opt/qqbot/main.py`
- configuration: `/opt/qqbot/config.json`
- persistent state: `/opt/qqbot/data/`
- service: `qqbot.service`

## Validation

Run before every deployment:

```bash
git diff --check
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
8. Restart `qqbot.service`.
9. Confirm the service is active, the Git worktree is clean, 80 commands are
   registered, and OneBot WebSocket connects.
10. Inspect recent journal entries for exceptions.

## Rollback

If startup, connection, or behavior validation fails:

1. Stop `qqbot.service`.
2. Reset `/opt/qqbot` to the previous known-good commit, or extract the
   pre-stage archive from `/root/qqbot-backups/`.
3. Re-run compile and tests in the server virtual environment.
4. Start `qqbot.service` and confirm OneBot connectivity.
5. Keep `config.json`, `/etc/qqbot.env`, and `data/` from the current server;
   code archives intentionally do not replace runtime secrets or state.
