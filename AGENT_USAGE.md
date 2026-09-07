# Vecta Agent — Setup Guide & How It Works

This guide explains what a user must do to set up the agent, and exactly how the agent
behaves once it is running. It is written for the **current agent behavior** — use it as
the source of truth when the frontend/dashboard UI and the agent fall out of sync.

---

## The short version

There are only two one-time setup steps, and one always-on step:

1. **Register the machine** — proves "this machine is allowed to talk to Vecta".
2. **Initialize the backup repository** — creates the (encrypted) place backups are stored
   and sets its password.
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

## Step 2 — Initialize the backup repository (one-time, per destination)

```bash
vecta-agent repo init <DESTINATION>                 # prompts for a password (typed twice)
vecta-agent repo init <DESTINATION> --generate      # generates a strong random password
vecta-agent repo init <DESTINATION> --password <P>  # password given on the command line
vecta-agent repo init <DESTINATION> --password-file <F>  # password read from a file
```

- `DESTINATION` is the same value the dashboard puts in a job's `destination` field
  (e.g. `s3:s3.amazonaws.com/my-bucket/backups` or `/mnt/backups/repo`). The repository is
  created **at** this location; a subfolder is not created for you.
- This command runs `restic init`, then stores the password as `RESTIC_PASSWORD` in
  `~/.config/vecta/restic.env` (chmod 600) — and **only** after init succeeds.

### The password rules (important)

- The password **encrypts the repository**. It is stored **only on the machine** and is
  **never** sent to the backend. It **cannot be recovered**. If you lose it, the backups in
  that repository are permanently unreadable — store a copy in a password manager.
- One `RESTIC_PASSWORD` per machine. Multiple jobs sharing a destination must share the
  same password, because it is the repository's encryption key.
- The agent also accepts `RESTIC_PASSWORD_FILE` or `RESTIC_PASSWORD_COMMAND` in
  `restic.env` if you prefer not to store the raw password.

### `repo init` is create-only (strict)

`repo init` will **never overwrite** an existing repository or an existing password.

- If a repository already exists at `DESTINATION`, the command prints
  `Repository at <DESTINATION> is already initialized.` and **does not touch**
  `restic.env`. It exits successfully (exit 0).
- This is deliberate: re-running `repo init` with a different password must not lock you
  out of backups you already have.

> To attach an **existing** repository to a **new** machine, do **not** use `repo init`.
> Manually add the existing repository's password to `~/.config/vecta/restic.env`:
>
> ```bash
> RESTIC_PASSWORD=<the repository's existing password>
> ```
>
> (`repo init` is for creating a brand-new repository, not for joining an existing one.)

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
        report "failed" -> "No repository password configured. Run 'vecta-agent repo init ...'"
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
| `~/.config/vecta/restic.env` | `RESTIC_PASSWORD` (+ optional cloud creds) | `repo init` (or you, manually) |

Both are chmod 600. Cloud credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, etc.)
are **never** stored on the backend; add them to `restic.env` yourself.

---

## Troubleshooting / common messages

| Message | What it means | What to do |
|---|---|---|
| `Config not found ... Run 'vecta-agent register --token <TOKEN>' first` | Machine not registered | Register (Step 1). |
| `No repository password configured. Run 'vecta-agent repo init <DESTINATION>' ...` | A job ran but no repo password exists | Run `repo init` for the job's destination. |
| `Repository at <DESTINATION> is already initialized.` | You re-ran `repo init` on an existing repo | Nothing — this is fine and safe. The existing password was not changed. |
| `restic executable not found on PATH` | restic binary is missing | Install restic. |
| `restic init failed: storage unreachable` (or similar) | Destination unreachable / credentials wrong | Fix the destination/credentials; the password was not saved. |
| Job status stuck at `running` | A run crashed before reporting a final status | The next due run will retry; no data is lost. |
