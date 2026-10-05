# Vecta Agent — Setup Guide & How It Works

This guide explains what a user must do to set up the agent, and exactly how the agent
behaves once it is running. It is written for the **current agent behavior** — use it as
the source of truth when the frontend/dashboard UI and the agent fall out of sync.

---

## The short version

There are only two one-time setup steps, and one always-on step:

1. **Register the machine** — proves "this machine is allowed to talk to Vecta".
2. **Set up the destination** — run `sudo vecta-agent setup <JOB_ID>` once after creating a job
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
sudo vecta-agent register --token <REGISTRATION_TOKEN>
```

- The token is generated in the dashboard (`POST /api/agents/generate-token`) and is
  single-use, expiring after 24 hours.
- On success the agent saves `agent_id` and `api_key` to `~/.config/vecta/config.toml`
  (chmod 600). The API key is shown **only once**; if it is lost, deactivate and re-register
  the agent.
- This step only establishes identity. It does **not** create or touch any backup repository.

---

## Step 2 — Set up the destination (one-time, per destination)

Create the job in the Vecta dashboard first — database settings, file paths, hooks, and
destination details are non-secret config. Database passwords remain local. Then run once
on the agent machine:

```bash
sudo vecta-agent setup <JOB_ID>
```

The command:

1. Fetches the job's non-secret config from the backend (a read-only lookup that works
   even when the job has never been due to run yet).
2. Checks whether credentials for this destination are already stored locally — if so,
   nothing is prompted.
3. Otherwise prompts (hidden input) for the credential fields the destination type needs
   (see the table below). SFTP destinations are instead verified with a non-interactive
   SSH key-auth test that requests the sftp subsystem — exactly what restic uses, so
   sftp-only servers (e.g. `ForceCommand internal-sftp` setups like atmoz/sftp) work —
   and the command prints copy-pasteable `ssh-keygen` / `ssh-copy-id` commands if that
   fails.
4. Checks whether the restic repository already exists. If not, it initializes it and
   generates a strong repository password, prints it once, and **blocks until you type
   `SAVED`** to confirm you stored a copy in a password manager.
5. Prints a summary: destination configured, repository ready, job will run whenever its
   schedule next triggers.

Credentials are stored in `~/.config/vecta/credentials.toml` (chmod 600), keyed by a
fingerprint of the destination — see [Destination credentials](#destination-credentials).

### PostgreSQL database jobs

For a PostgreSQL job, `setup` also fetches the non-secret connection settings and asks for
the database password using hidden input (twice for confirmation). The password is stored
in a connection-scoped libpq password file named `pgpass-<fingerprint>.conf` in the Vecta
configuration directory, with mode `0600`. It is separate from destination credentials,
so entering a database password never replaces cloud/SFTP credentials. The agent sets
`PGPASSFILE` only for the Restic child process that runs `pg_dump`.

Install `pg_dump` 9.0 or newer and Restic 0.17.0 or newer on the agent. Capability
reporting checks both version output and Restic's backup help for `--stdin-from-command`
and `--stdin-filename`. Setup checks the local tool requirements, prompts for a password
when needed, and runs a bounded schema-only `pg_dump` connection test (up to 30 seconds).
It verifies database connectivity and schema access, but does not create a full archive or
test the Restic upload; the first backup run is the end-to-end check. The password is saved
to its mode-0600 pgpass file after destination/repository setup completes.

PostgreSQL uses a custom archive stream named `postgresql-<sanitized-database>.dump` in
the snapshot. The agent invokes a typed `pg_dump -Fc -Z 0` argv through Restic's
`--stdin-from-command` mode. Restic waits for the producer and does not create a snapshot
when the command fails. The archive is not first written as a full temporary dump file;
Restic caches, temporary upload files, local repositories, and runtime data still use
disk. `-Z 0` is an initial deduplication candidate, not a storage-reduction guarantee.
Restic compression is available only with Restic 0.14+ and repository format v2; the
agent does not upgrade existing repositories to enable it.

To restore the archived database manually, extract the run's `stdin_filename` from Restic
and pipe it to `pg_restore` (substitute shell-quoted values and provide local Restic and
PostgreSQL credentials). The generated command does not configure `pg_restore` authentication;
the agent's connection-scoped pgpass file is used for backup runs, not automatically for
restores. Configure libpq credentials for the target database under the user running
`pg_restore`:

```bash
sudo restic --repo '<repository>' dump '<snapshot-id>' 'postgresql-orders.dump' \
  | pg_restore --dbname='<target-database>'
```

The dashboard stores `backup_type` and `stdin_filename` on each run so later job edits do
not change historical restore instructions. A failed dump is not a verified restorable
snapshot.

### Local hook catalog

Hooks are optional, root-managed local capabilities. The default catalog is
`/etc/vecta/hooks.toml`; the catalog, every parent directory, and each executable must be
root-owned and not writable by group or other users. Symlinks are rejected. A missing or
invalid catalog advertises no hooks and selected unknown/removed hooks fail closed.

For an interactive setup, install the executable and run `sudo vecta-agent hooks add`.
Then run `vecta-agent hooks validate` and `vecta-agent hooks list`; `sudo vecta-agent hooks publish`
updates the dashboard immediately. Normal `vecta-agent run` also reports the
catalog, so publishing manually is only needed when you want the dashboard refreshed now.
The wizard defaults to the `vecta-hook` account, which the machine administrator must
provision, or you can choose another existing service account.

Example entry (metadata is sent to the backend; executable and argv mapping stay local):

```toml
[[hooks]]
id = "refresh-cache"
name = "Refresh application cache"
description = "Refresh the cache for the configured service."
phases = ["pre", "post"]
executable = "/usr/local/libexec/vecta-refresh-cache"
argv = ["--service", "${service}"]
run_as = "vecta-hook"
requires_root = false
timeout_seconds = 60
output_limit_bytes = 8192
parameters_schema = { type = "object", properties = { service = { type = "string", minLength = 1, maxLength = 64, pattern = "[A-Za-z0-9_-]+" } }, required = ["service"], additionalProperties = false }
```

Parameter schema supports narrow string, integer, and boolean values. A parameter
placeholder must occupy a whole argv element. Hook arguments are executed directly with
`shell=False`; jobs cannot supply paths, commands, argv, working directories, or
environment overrides. `run_as = "root"` is permitted only with `requires_root = true`;
otherwise hooks run as the named local service account. The default `vecta-hook` account
is not provisioned by this repository's installer and must be created by the machine
administrator (or installer integration). If privilege dropping fails, the hook does not
run as root as a fallback.

Catalog timeout/output limits can only tighten the agent caps: 120 seconds and 16 KiB per
output stream. Hook output is bounded and is not included in backend status. Cancellation
interrupts active pre-hooks, source capture, Restic, and a post-hook on the normal success
path. After a failure/cancellation, a configured post-hook gets one bounded cleanup
opportunity; a repeated cancellation does not interrupt that cleanup.

During each run pass, the agent reports `postgresql_stdin_backup` only when Restic 0.17.0+, pg_dump
9.0+, and both stdin flags are available. It also reports a safe check for each known feature.
Unavailable checks use a fixed reason code, such as `pg_dump_missing` or
`restic_stdin_options_missing`, so the dashboard can explain what to fix. It reports
`hook_catalog_v1` and safe hook metadata only when the catalog validates; an invalid catalog
is reported as `hook_catalog_invalid`. Reports never contain local executable paths, command
output, script contents, credentials, or secret file paths. This negotiation is compatibility
metadata, not a trust boundary for jobs.

### The password rules (important)

- The repository password **encrypts the repository**. It is stored **only on the machine**
  and is **never** sent to the backend. It **cannot be recovered**. If you lose it, the
  backups in that repository are permanently unreadable — store a copy in a password manager.
- The agent also accepts `RESTIC_PASSWORD_FILE` or `RESTIC_PASSWORD_COMMAND` in
  `restic.env` if you prefer not to store the raw password.

### `repo init` — manual escape hatch

`sudo vecta-agent repo init <DESTINATION>` (--generate / --password / --password-file) still
exists for setting up a repository without a dashboard job. It stores the password in the
global `restic.env` and **never overwrites** an existing repository or password: if the
repository already exists, it prints
`Repository at <DESTINATION> is already initialized.` and does not touch `restic.env`.

> To attach an **existing** repository to a **new** machine, run `sudo vecta-agent setup`
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
| `sftp:user@host:/path` | nothing — SSH keys only | The colon before the path is required. `setup` validates the destination format, then runs a non-interactive SSH key-auth probe that requests the sftp subsystem (`ssh -o BatchMode=yes -s <user>@<host> sftp`), so sftp-only servers pass; on failure it prints `ssh-keygen` / `ssh-copy-id -p <port> user@host` fix commands. Non-default ports come from the job's `port` field and are passed to restic as `-o sftp.command="ssh -p <port> <user>@<host> -s sftp"`. |

---

## Step 3 — Running (cron)

On Linux, `register`, `setup`, `hooks add`, `hooks publish`, `repo init`, `run`, and `update` require root because they
access backup data, protected configuration, or the installed binary. Use `sudo` for manual
invocations. `version` and `help` remain available without root. To intentionally run an
operational command as the current user, put `--allow-non-root` before the subcommand, for
example `vecta-agent --allow-non-root run`; protected paths may then be inaccessible. This
enforcement does not change non-POSIX behavior.

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
    run_id = new UUID
    report "running" (with run_id)
    if no repository password configured:
        report "failed" -> "No repository password configured. Run 'vecta-agent setup <JOB_ID>' ..."
        next job

    probe the repository (restic cat config):
        - exists        -> proceed
        - missing       -> initialize it (restic init)
        - indeterminate -> report "failed" (do NOT initialize)
      cancellation polling covers this phase too: a cancel kills the hung
      probe/init command and reports "Cancelled by user" instead of waiting
      out the 120s probe timeout

    run restic backup (source -> destination), streaming progress + polling cancel
    a watchdog kills restic after 12h and reports a distinct timeout failure
    report "success" (with snapshot id, counts, bytes),
           "warning" (backup completed but zero files were scanned — check the source path),
           or "failed" (with stderr tail)
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
| `~/.config/vecta/pgpass-<fingerprint>.conf` | connection-scoped PostgreSQL password | `setup` |
| `/etc/vecta/hooks.toml` | root-controlled local hook catalog | `hooks add` (or machine administrator) |

Configuration and credential files are chmod 600. The hook catalog and executables must
remain root-controlled and not writable by group or other users. Cloud credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, etc.)
are **never** stored on the backend; put them in `restic.env` or let `vecta-agent setup`
store them per destination in `credentials.toml`.

---

## Troubleshooting / common messages

| Message | What it means | What to do |
|---|---|---|
| `Config not found ... Run 'vecta-agent register --token <TOKEN>' first` | Machine not registered | Register with `sudo vecta-agent register --token <TOKEN>` (Step 1). |
| `No repository password configured for this destination. Run 'vecta-agent setup <JOB_ID>' ...` | A job ran but no repo password exists for its destination | Run `sudo vecta-agent setup <JOB_ID>` on this machine. |
| `This looks like an authentication failure. Run 'vecta-agent setup <JOB_ID>' ...` | restic hit an auth error (missing/wrong stored credentials or SSH keys) | Run `sudo vecta-agent setup <JOB_ID>` to (re)configure this destination's credentials. |
| `Repository at <DESTINATION> is already initialized.` | You re-ran `repo init` on an existing repo | Nothing — this is fine and safe. The existing password was not changed. |
| `SSH key authentication to the SFTP destination failed.` | The SSH key-auth probe in `setup` failed | Run the printed `ssh-keygen` / `ssh-copy-id` commands, then re-run setup. |
| `restic executable not found on PATH` | restic binary is missing | Install restic. |
| `restic init failed: storage unreachable` (or similar) | Destination unreachable / credentials wrong | Fix the destination/credentials; the password was not saved. |
| Job status stuck at `running` | A run crashed before reporting a final status | The next due run will retry; no data is lost. |
