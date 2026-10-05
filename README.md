# Vecta Backup Agent

Stateless backup agent for the [Vecta](https://vectaapp.com) platform. Runs on Linux target machines, executes backups via [restic](https://restic.net), and reports status to the Vecta backend.

## Features

- **Stateless** — no persistent state; the backend decides what to run and when
- **Single binary** — distributed as a standalone PyInstaller executable
- **Auto-init repositories** — restic repos are initialized automatically on first backup
- **Progress reporting** — live progress and cancellation support via cron-driven polling
- **PostgreSQL backups** — streams custom-format `pg_dump` archives into Restic without staging a full dump file
- **Local hooks** — root-controlled pre/post hook catalog with schema-validated parameters and bounded execution
- **Secure by design** — repo passwords and cloud credentials stay on the machine, never sent to the backend

## Quick Start

```bash
# 1. Register this machine (one-time)
sudo vecta-agent register --token <REGISTRATION_TOKEN>

# 2. Create a job in the Vecta dashboard, then run once on this machine:
sudo vecta-agent setup <JOB_ID>

# 3. Run backups (single pass; call from cron)
sudo vecta-agent run
```

`setup` fetches the job's non-secret config from the backend, stores the destination
credentials locally (hidden prompts), initializes the restic repository, and prints a
generated repository password once.

## Installation

### From Vecta dashboard

When registering a new agent on the Vecta dashboard, you will get a one-liner install script that downloads everything (binary, restic) automatically

```bash
curl -fsSL https://vectaapp.com/install.sh | sudo bash -s -- --token <REGISTRATION_TOKEN>
```

### From binary

Download the prebuilt binary from releases and place it on `PATH`:

```bash
sudo curl -L https://github.com/vecta-backup/VectaAgent/releases/latest/download/vecta-agent -o /usr/local/bin/vecta-agent
sudo chmod +x /usr/local/bin/vecta-agent
```

### From source

Requires Python 3.12+:

```bash
git clone https://github.com/vecta-backup/VectaAgent.git
cd VectaAgent
pip install -e .
```

## CLI

| Command | Description |
|---------|-------------|
| `sudo vecta-agent register --token <TOKEN>` | Register this machine with Vecta |
| `sudo vecta-agent setup <JOB_ID>` | Store credentials for a job's destination and initialize its repository |
| `sudo vecta-agent hooks add` | Interactively add a locally installed hook |
| `vecta-agent hooks list` | List hooks in the local catalog |
| `vecta-agent hooks validate` | Check catalog safety and hook definitions |
| `sudo vecta-agent hooks publish` | Publish current capabilities to the dashboard immediately |
| `sudo vecta-agent repo init <DEST>` | Initialize a restic repository at destination (manual escape hatch) |
| `sudo vecta-agent run` | Execute due backup jobs (single pass) |
| `vecta-agent version` | Show version |
| `sudo vecta-agent update` | Update the installed binary to the latest GitHub release (`--check` to compare only, `--version vX.Y.Z` to pin) |

### Repository initialization

```bash
sudo vecta-agent setup <JOB_ID>                              # recommended: credentials + repo init
sudo vecta-agent repo init <DESTINATION>                     # prompts for password
sudo vecta-agent repo init <DESTINATION> --generate          # generates random password
sudo vecta-agent repo init <DESTINATION> --password <P>      # password on command line
sudo vecta-agent repo init <DESTINATION> --password-file <F> # password from file
```

The agent also auto-initializes missing repositories on `run` if a password is configured.

### Local hooks

Install the hook executable on the agent first, then run `sudo vecta-agent hooks add` to add
it to `/etc/vecta/hooks.toml`. The wizard collects its metadata, allowed phase, execution
account, parameters, and fixed argv template. Use `vecta-agent hooks validate` to check the
catalog and `vecta-agent hooks list` to review it. Run `sudo vecta-agent hooks publish` to
make the current catalog available in the dashboard immediately; otherwise the next
`vecta-agent run` reports it automatically. Hook executables and the catalog must remain
root-controlled. The default `vecta-hook` execution account must be provisioned locally, or
select another existing service account in the wizard.

PostgreSQL jobs require Restic 0.17.0+ and `pg_dump` 9.0+. `sudo vecta-agent setup <JOB_ID>`
stores the database password in a connection-scoped mode-0600 pgpass file. The agent uses
`pg_dump -Fc -Z 0` with Restic's `--stdin-from-command`; this avoids a full temporary dump
file but does not eliminate Restic cache/upload disk use or promise a specific storage
reduction. Database backups restore manually with `restic dump <snapshot> <stdin_filename>
| pg_restore --dbname=<target>`. See [AGENT_USAGE.md](AGENT_USAGE.md#postgresql-database-jobs)
for setup, restore, capability, and local hook catalog details.

### Root privileges

On Linux, operational commands (`register`, `setup`, `hooks add`, `hooks publish`, `repo init`, `run`, and `update`) must be
run as root so the agent can access the machine's backup data and system installation paths. Use
`sudo vecta-agent ...`. For an intentional per-user invocation, pass `--allow-non-root` before
the subcommand; this is an escape hatch and may prevent access to protected source paths.

## Configuration

Files are stored in `~/.config/vecta/`:

| File | Contents | Permissions |
|------|----------|-------------|
| `config.toml` | `agent_id`, `api_key`, `name` | `chmod 600` |
| `restic.env` | Global `RESTIC_PASSWORD` and cloud credentials (fallback for destinations without stored credentials) | `chmod 600` |
| `credentials.toml` | Per-destination credentials, one `[<fingerprint>]` section per destination | `chmod 600` |

### restic.env

```bash
RESTIC_PASSWORD=...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

### credentials.toml (per-destination credentials)

Credentials are keyed by a fingerprint of the destination (a hash of the destination
string), never by a user-chosen name — two jobs to the same destination automatically
share one credential set:

```toml
[a1b2c3d4e5f60718]
destination = "s3:https://<account>.r2.cloudflarestorage.com/<bucket>"
RESTIC_PASSWORD=...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

Per-job env resolution: process env → `restic.env` → the destination's stored
credentials (stored wins). Each destination has its own repository password. SFTP
destinations authenticate with SSH keys (no secrets are stored for them); a job's
`port` field is passed to restic via `-o sftp.command="ssh -p <port> <user>@<host> -s sftp"`.

**Important:** The repository password is the encryption key for your backups. It is **never** stored on the Vecta backend and **cannot be recovered**. Store it in a password manager.

## Scheduling

The agent is designed to run from cron:

```cron
*/2 * * * * flock -n /var/lock/vecta-agent.lock /usr/local/bin/vecta-agent run >> /var/log/vecta-agent.log 2>&1
```

- The backend decides when jobs are due via `schedule_interval_minutes`
- `flock -n` prevents overlapping runs; a slow run never overlaps a new one
- Every 2 minutes is a good balance between responsiveness and overhead

## Development

```bash
pip install -e ".[dev]" pre-commit
pre-commit install
pytest -q
```

## Building

PyInstaller builds must run on the target OS (no cross-compilation):

```bash
# On Linux
./build.sh

# On Windows (via Docker)
powershell -File docker-build.ps1
```

Produces `dist/vecta-agent`.

## Requirements

- Python 3.12+ (for building/development)
- Linux (for running the agent)
- [restic](https://restic.net) on `PATH`
- `flock` (part of `util-linux` on most distros)

## License

Apache 2.0
