# Vecta Backup Agent

Stateless backup agent for the [Vecta](https://vectaapp.com) platform. Runs on Linux target machines, executes backups via [restic](https://restic.net), and reports status to the Vecta backend.

## Features

- **Stateless** — no persistent state; the backend decides what to run and when
- **Single binary** — distributed as a standalone PyInstaller executable
- **Auto-init repositories** — restic repos are initialized automatically on first backup
- **Progress reporting** — live progress and cancellation support via cron-driven polling
- **Secure by design** — repo passwords and cloud credentials stay on the machine, never sent to the backend

## Quick Start

```bash
# 1. Register this machine (one-time)
vecta-agent register --token <REGISTRATION_TOKEN>

# 2. Initialize the backup repository (one-time, per destination)
vecta-agent repo init <DESTINATION> --generate

# 3. Run backups (single pass; call from cron)
vecta-agent run
```

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
| `vecta-agent register --token <TOKEN>` | Register this machine with Vecta |
| `vecta-agent repo init <DEST>` | Initialize a restic repository at destination |
| `vecta-agent repo init <DEST> --profile <NAME>` | Init and store the password in credential profile `<NAME>` |
| `vecta-agent secret set <NAME>` | Create/update a local credential profile (`--generate`, `--password`, `--password-file`, `--env KEY=VALUE`) |
| `vecta-agent secret list` | List profile names and their keys (never values) |
| `vecta-agent secret remove <NAME>` | Delete a credential profile |
| `vecta-agent run` | Execute due backup jobs (single pass) |
| `vecta-agent version` | Show version |

### Repository initialization

```bash
vecta-agent repo init <DESTINATION>                 # prompts for password
vecta-agent repo init <DESTINATION> --generate      # generates random password
vecta-agent repo init <DESTINATION> --password <P>  # password on command line
vecta-agent repo init <DESTINATION> --password-file <F>  # password from file
```

The agent also auto-initializes missing repositories on `run` if a password is configured.

## Configuration

Files are stored in `~/.config/vecta/`:

| File | Contents | Permissions |
|------|----------|-------------|
| `config.toml` | `agent_id`, `api_key`, `name` | `chmod 600` |
| `restic.env` | Global `RESTIC_PASSWORD` and cloud credentials (fallback for jobs without a profile) | `chmod 600` |
| `secrets.toml` | Credential profiles — one `[profile-name]` section per destination, referenced by a job's `credential_profile` | `chmod 600` |

### restic.env

```bash
RESTIC_PASSWORD=...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

### secrets.toml (credential profiles)

Jobs reference secrets by profile name only — values never reach the backend:

```toml
[prod-s3]
RESTIC_PASSWORD=...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...

[nas-sftp]
RESTIC_PASSWORD=...
```

Per-job env resolution: process env → `restic.env` → the job's profile (profile wins). Each
destination can have its own repository password. SFTP destinations use SSH keys; a job's
`port` field is passed to restic as `-o sftp.args="-p <port>"`.

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
pip install -e .[dev]
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
