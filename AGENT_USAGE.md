# Vecta Agent — Setup Guide & How It Works

This guide explains what a user must do to set up the agent, and exactly how the agent
behaves once it is running. It is written for the **current agent behavior** — use it as
the source of truth when the frontend/dashboard UI and the agent fall out of sync.

---

## The short version

There are only two one-time setup steps, and one always-on step:

1. **Register the machine** — proves "this machine is allowed to talk to Vecta".
2. **Set up the destination** — run `vecta-agent setup <JOB_ID>` once after creating a job
   in the dashboard: it stores the destination's credentials locally and initializes the
   (encrypted) restic repository.
3. **Run** — done automatically by cron every few minutes. The agent pulls jobs from the
   backend, runs them, and reports back.

Everything else (schedules, "is this job due?", "run now") is decided by the **backend**.
The agent is a dumb worker: it asks "what should I do now?", does it, and exits.

---

## Step 1 — Register the machine (one-time)

Run this once on each machine that will be backed up:

```bash
vecta-agent register --token <REGISTRATION_TOKEN>
```

- The token is generated in the dashboard (`POST /api/agents/generate-token`) and is
  single-use, expiring after 24 hours.
- On success the agent saves `agent_id` and `api_key` to `~/.config/vecta/config.toml`
  (chmod 600). The API key is shown **only once**; if it is lost, deactivate and re-register
  the agent.
- This step only establishes identity. It does **not** create or touch any backup repository.

---

## Step 2 — Set up the destination (one-time, per destination)

Create the job in the Vecta dashboard first — it only collects non-secret config
(source path, destination URL, schedule). Then run once on the agent machine:

```bash
vecta-agent setup <JOB_ID>
```

The command:

1. Fetches the job's non-secret config from the backend (a read-only lookup that works
   even when the job has never been due to run yet).
2. Checks whether credentials for this destination are already stored locally — if so,
   nothing is prompted.
3. Otherwise prompts (hidden input) for the credential fields the destination type needs
   (see the table below). SFTP destinations are instead verified with a non-interactive
   SSH key-auth test, and the command prints copy-pasteable `ssh-keygen` /
   `ssh-copy-id` commands if that fails.
4. Checks whether the restic repository already exists. If not, it initializes it and
   generates a strong repository password, prints it once, and **blocks until you type
   `SAVED`** to confirm you stored a copy in a password manager.
5. Prints a summary: destination configured, repository ready, job will run whenever its
   schedule next triggers.

Credentials are stored in `~/.config/vecta/credentials.toml` (chmod 600), keyed by a
fingerprint of the destination — see [Destination credentials](#destination-credentials).

### The password rules (important)

- The repository password **encrypts the repository**. It is stored **only on the machine**
  and is **never** sent to the backend. It **cannot be recovered**. If you lose it, the
  backups in that repository are permanently unreadable — store a copy in a password manager.
- The agent also accepts `RESTIC_PASSWORD_FILE` or `RESTIC_PASSWORD_COMMAND` in
  `restic.env` if you prefer not to store the raw password.

### `repo init` — manual escape hatch

`vecta-agent repo init <DESTINATION>` (--generate / --password / --password-file) still
exists for setting up a repository without a dashboard job. It stores the password in the
global `restic.env` and **never overwrites** an existing repository or password: if the
repository already exists, it prints
`Repository at <DESTINATION> is already initialized.` and does not touch `restic.env`.

> To attach an **existing** repository to a **new** machine, run `vecta-agent setup`
> for a job with that destination — it detects the existing repository and prompts for
> its password (typed twice, hidden input), then stores it per-destination.

---

## Destination credentials

Credentials are stored **per destination**, keyed by a fingerprint of the destination
string (a hash of e.g. `s3:https://<account>.r2.cloudflarestorage.com/<bucket>`). There is
no user-chosen profile name anywhere: a job's dashboard config only carries the
destination itself, so two jobs pointing at the same destination automatically share one
locally stored credential set. The secrets themselves never leave this machine.

`setup` is normally the only way credentials get written. The file lives in
`~/.config/vecta/credentials.toml` (chmod 600):

```toml
[a1b2c3d4e5f60718]
destination = "s3:https://<account>.r2.cloudflarestorage.com/<bucket>"
RESTIC_PASSWORD=...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

(`destination` is a non-secret annotation so you can tell what each section is for.)

Env resolution for a job run: agent process environment → global `restic.env` → the
destination's stored credentials (stored wins). Destinations that need nothing stored keep
working off `restic.env` / process env / instance roles.

### Destination types

| Destination | `setup` prompts for | Notes |
|---|---|---|
| `/path/to/repo` (local) | repository password (generated) | Nothing else needed. |
| `s3:https://<endpoint>/<bucket>` | `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` (hidden input) + generated repo password | R2/Wasabi/Minio work the same way. |
| `b2:<bucket>:<path>` | `B2_ACCOUNT_ID` + `B2_ACCOUNT_KEY` (hidden input) + generated repo password | restic recommends the S3-compatible API for B2. |
| `sftp:user@host:/path` | nothing — SSH keys only | `setup` runs a non-interactive SSH key-auth probe (`BatchMode`) and prints `ssh-keygen` / `ssh-copy-id -p <port> user@host` fix commands on failure. Non-default ports come from the job's `port` field and are passed to restic as `-o sftp.command="ssh -p <port> <user>@<host> -s sftp"`. |

---

## Step 3 — Running (cron)

The agent is **not a daemon**. Cron invokes it every few minutes and it does a single pass:

```cron
*/5 * * * * flock -n /var/lock/vecta-agent.lock /usr/local/bin/vecta-agent run >> /var/log/vecta-agent.log 2>&1
```

- `flock -n` makes overlapping runs fail fast instead of stacking up.
- The cron interval only bounds how late a due job starts; the **backend** decides whether a
  job is due using `schedule_interval_minutes`.

---

## What the agent does on each run

For a single `run`, the agent does exactly this:

```
jobs = fetch due jobs from the backend
if none: exit

for each job:
    report "running"
    if no repository password configured:
        report "failed" -> "No repository password configured. Run 'vecta-agent setup <JOB_ID>' ..."
        next job

    probe the repository (restic cat config):
        - exists        -> proceed
        - missing       -> initialize it (restic init)
        - indeterminate -> report "failed" (do NOT initialize)

    run restic backup (source -> destination), streaming progress + polling cancel
    report "success" (with snapshot id, counts, bytes) or "failed" (with stderr tail)
```

Key points:

- **Auto-initialization is safe and conservative.** The agent only initializes when restic
  reports the repository is *definitively* missing (exit code 10, or a "no such file /
  does not exist" error). If the check is inconclusive — network unreachable, wrong
  password, permission denied — the job is reported failed and **no** initialization
  happens. `restic init` never overwrites an existing repository.
- **The agent never deletes or prunes anything.** It only reads the source and appends to
  the repository. Restores are manual (via restic) and never performed automatically.

---

## Files the agent manages

| Path | Contents | Written by |
|---|---|---|
| `~/.config/vecta/config.toml` | `agent_id`, `api_key`, `name` | `register` |
| `~/.config/vecta/restic.env` | global `RESTIC_PASSWORD` (+ optional cloud creds) | `repo init` (or you, manually) |
| `~/.config/vecta/credentials.toml` | per-destination credentials (one `[<fingerprint>]` section per destination) | `setup` (or you, manually) |

All are chmod 600. Cloud credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, etc.)
are **never** stored on the backend; put them in `restic.env` or let `vecta-agent setup`
store them per destination in `credentials.toml`.

---

## Troubleshooting / common messages

| Message | What it means | What to do |
|---|---|---|
| `Config not found ... Run 'vecta-agent register --token <TOKEN>' first` | Machine not registered | Register (Step 1). |
| `No repository password configured for this destination. Run 'vecta-agent setup <JOB_ID>' ...` | A job ran but no repo password exists for its destination | Run `vecta-agent setup <JOB_ID>` on this machine. |
| `This looks like an authentication failure. Run 'vecta-agent setup <JOB_ID>' ...` | restic hit an auth error (missing/wrong stored credentials or SSH keys) | Run `vecta-agent setup <JOB_ID>` to (re)configure this destination's credentials. |
| `Repository at <DESTINATION> is already initialized.` | You re-ran `repo init` on an existing repo | Nothing — this is fine and safe. The existing password was not changed. |
| `SSH key authentication to the SFTP destination failed.` | The SSH key-auth probe in `setup` failed | Run the printed `ssh-keygen` / `ssh-copy-id` commands, then re-run setup. |
| `restic executable not found on PATH` | restic binary is missing | Install restic. |
| `restic init failed: storage unreachable` (or similar) | Destination unreachable / credentials wrong | Fix the destination/credentials; the password was not saved. |
| Job status stuck at `running` | A run crashed before reporting a final status | The next due run will retry; no data is lost. |
