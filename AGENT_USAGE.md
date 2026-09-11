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
vecta-agent repo init <DESTINATION> --profile <NAME>     # store the password in a credential profile
```

- `DESTINATION` is the same value the dashboard puts in a job's `destination` field
  (e.g. `s3:s3.amazonaws.com/my-bucket/backups` or `/mnt/backups/repo`). The repository is
  created **at** this location; a subfolder is not created for you.
- This command runs `restic init`, then stores the password as `RESTIC_PASSWORD` in
  `~/.config/vecta/restic.env` (chmod 600) — and **only** after init succeeds.
- With `--profile <NAME>` the password is stored in that profile in
  `~/.config/vecta/secrets.toml` instead of the global `restic.env`, and jobs that reference
  the same `credential_profile` use it. Use this when different destinations need **different
  passwords** (see Credential profiles below).

### The password rules (important)

- The password **encrypts the repository**. It is stored **only on the machine** and is
  **never** sent to the backend. It **cannot be recovered**. If you lose it, the backups in
  that repository are permanently unreadable — store a copy in a password manager.
- Without `--profile`, `repo init` stores one global `RESTIC_PASSWORD` per machine, shared by
  every repository without a credential profile. To give a destination its own password, create
  a credential profile and reference it from the job in the dashboard.
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

## Credential profiles (recommended)

A **credential profile** holds the secrets for one destination (repository password, cloud
credentials, etc.) under a short name. Jobs in the dashboard reference a profile by name via
their `credential_profile` field — the name is the only thing that ever reaches the backend;
**the secrets themselves never leave this machine**.

Create a profile (the dashboard shows this name in the job form):

```bash
vecta-agent secret set prod-s3 --generate
vecta-agent secret set prod-s3 --env AWS_ACCESS_KEY_ID=AKIA... --env AWS_SECRET_ACCESS_KEY=...
```

- `--generate` / `--password <P>` / `--password-file <F>` set the repository password
  (`RESTIC_PASSWORD`). Omit them and you'll be prompted.
- `--env KEY=VALUE` adds any restic env var (AWS keys for S3/R2, `B2_ACCOUNT_ID`/`B2_ACCOUNT_KEY`,
  `AZURE_ACCOUNT_KEY`, ...). Repeatable; saved values are merged across calls.
- Omitting both flags prompts for a repository password interactively.

Other commands:

```bash
vecta-agent secret list            # profile names + key names only (never values)
vecta-agent secret remove <NAME>   # delete a profile
```

Profiles live in `~/.config/vecta/secrets.toml` (chmod 600):

```toml
[prod-s3]
RESTIC_PASSWORD=...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...

[nas-sftp]
RESTIC_PASSWORD=...
```

Env resolution for a job run: agent process environment → global `restic.env` → the job's
`credential_profile` section (profile wins on conflicts). Jobs **without** a profile keep using
the global `restic.env`, so existing setups keep working unchanged.

### Destination types

| Destination | Auth | Notes |
|---|---|---|
| `/path/to/repo` (local) | repo password | Works with the global password or a profile. |
| `s3:https://<endpoint>/<bucket>` | repo password **+** `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Put the AWS keys in the job's credential profile (`vecta-agent secret set <name> --env AWS_ACCESS_KEY_ID=...`). R2/Wasabi/Minio work the same way. |
| `sftp:user@host:/path` | **SSH keys only** (restic shells out to `ssh`) | Password auth is not possible for automatic backups. Set up `~/.ssh` keys as usual. Non-default ports: set the job's `port` in the dashboard — the agent passes it to restic automatically. |

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
| `~/.config/vecta/restic.env` | global `RESTIC_PASSWORD` (+ optional cloud creds) | `repo init` without `--profile` (or you, manually) |
| `~/.config/vecta/secrets.toml` | credential profiles (one `[section]` per profile) | `secret set` / `repo init --profile` (or you, manually) |

All are chmod 600. Cloud credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, etc.)
are **never** stored on the backend; put them in `restic.env` or in the job's credential
profile.

---

## Troubleshooting / common messages

| Message | What it means | What to do |
|---|---|---|
| `Config not found ... Run 'vecta-agent register --token <TOKEN>' first` | Machine not registered | Register (Step 1). |
| `No repository password configured. Run 'vecta-agent repo init <DESTINATION>' ...` | A job ran but no repo password exists | Run `repo init` for the job's destination. |
| `No repository password configured. Run 'vecta-agent secret set <NAME>' ...` | The job references a credential profile that has no `RESTIC_PASSWORD` | Set the profile's repository password. |
| `Credential profile '<NAME>' is not configured on this machine.` | The job references a profile that doesn't exist locally | Run `vecta-agent secret set <NAME>` with the secrets for this job. |
| `Repository at <DESTINATION> is already initialized.` | You re-ran `repo init` on an existing repo | Nothing — this is fine and safe. The existing password was not changed. |
| `restic executable not found on PATH` | restic binary is missing | Install restic. |
| `restic init failed: storage unreachable` (or similar) | Destination unreachable / credentials wrong | Fix the destination/credentials; the password was not saved. |
| Job status stuck at `running` | A run crashed before reporting a final status | The next due run will retry; no data is lost. |
