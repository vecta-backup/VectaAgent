#!/usr/bin/env bash
#
# Vecta agent installer.
#
# Downloads the official restic binary and the compiled vecta-agent CLI,
# registers this machine with the Vecta dashboard, installs a cron entry that
# runs backups every 2 minutes, and does an initial pass so
# the server shows up as online immediately.
#
# Usage:
#   curl -fsSL https://vectaapp.com/install.sh | sudo bash -s -- --token <REGISTRATION_TOKEN>
#
set -euo pipefail

RESTIC_VERSION="0.19.1"
VECTA_AGENT_VERSION="0.2.0"
VECTA_AGENT_URL="${VECTA_AGENT_URL:-https://github.com/vecta-backup/VectaAgent/releases/download/v${VECTA_AGENT_VERSION}/vecta-agent}"
RESTIC_SHA256SUMS_URL="https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/SHA256SUMS"
VECTA_AGENT_SHA256SUMS_URL="https://github.com/vecta-backup/VectaAgent/releases/download/v${VECTA_AGENT_VERSION}/SHA256SUMS"
BIN_DIR="/usr/local/bin"
RESTIC_BIN="$BIN_DIR/restic"
AGENT_BIN="$BIN_DIR/vecta-agent"
CRON_FILE="/etc/cron.d/vecta"
CRON_LINE="*/2 * * * * root flock -n /var/lock/vecta-agent.lock $AGENT_BIN run >> /var/log/vecta-agent.log 2>&1"
CONFIG_PATH="/root/.config/vecta/config.toml"

TOKEN=""

usage() {
  cat <<EOF
Usage: $0 --token <REGISTRATION_TOKEN>

Registers a new Vecta backup agent on this machine.

  --token    One-time registration token from the Vecta dashboard (required)
  -h, --help Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token)
      TOKEN="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

# --- Preflight checks ---------------------------------------------------------

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Error: this installer must be run as root (try 'sudo bash $0 ...')." >&2
  exit 1
fi

arch="$(uname -m)"
if [[ "$arch" != "x86_64" ]]; then
  echo "Error: unsupported architecture '$arch' (only x86_64 is supported)." >&2
  exit 1
fi

if [[ -z "$TOKEN" ]]; then
  echo "Error: --token is required." >&2
  usage >&2
  exit 1
fi

# --- temp files ----------------------------------------------------------------

tmp_sums="$(mktemp)"
trap 'rm -f "$tmp_sums"' EXIT

# --- restic --------------------------------------------------------------------

if command -v restic >/dev/null 2>&1; then
  echo "restic already installed: $(command -v restic)"
else
  if ! command -v bunzip2 >/dev/null 2>&1; then
    echo "Error: 'bunzip2' is required to extract restic. Install bzip2" >&2
    echo "  (e.g. 'apt-get install -y bzip2') and re-run this installer." >&2
    exit 1
  fi
  echo "Downloading restic v$RESTIC_VERSION ..."
  tmp_archive="$(mktemp)"
  trap 'rm -f "$tmp_archive" "$tmp_sums"' EXIT
  curl -fsSL -o "$tmp_archive" \
    "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_amd64.bz2"
  echo "Verifying restic checksum ..."
  curl -fsSL -o "$tmp_sums" "$RESTIC_SHA256SUMS_URL"
  expected="$(grep -E "\srestic_${RESTIC_VERSION}_linux_amd64\.bz2$" "$tmp_sums" | awk '{print $1}')"
  if [[ -z "$expected" ]]; then
    echo "ERROR: restic hash not found in SHA256SUMS" >&2
    exit 1
  fi
  echo "$expected  $tmp_archive" | sha256sum -c - || {
    echo "ERROR: restic checksum mismatch" >&2
    exit 1
  }
  bunzip2 -c "$tmp_archive" > "$RESTIC_BIN"
  chmod 755 "$RESTIC_BIN"
  echo "Installed restic to $RESTIC_BIN"
fi

# --- vecta-agent ---------------------------------------------------------------

echo "Downloading vecta-agent v$VECTA_AGENT_VERSION ..."
curl -fsSL -o "$AGENT_BIN" "$VECTA_AGENT_URL"
echo "Verifying vecta-agent checksum ..."
curl -fsSL -o "$tmp_sums" "$VECTA_AGENT_SHA256SUMS_URL"
expected="$(grep -E '\svecta-agent$' "$tmp_sums" | awk '{print $1}')"
if [[ -z "$expected" ]]; then
  echo "ERROR: vecta-agent hash not found in SHA256SUMS" >&2
  exit 1
fi
echo "$expected  $AGENT_BIN" | sha256sum -c - || {
  echo "ERROR: vecta-agent checksum mismatch" >&2
  exit 1
}
chmod +x "$AGENT_BIN"
echo "Installed vecta-agent to $AGENT_BIN"

# --- Register (idempotent) -----------------------------------------------------

if [[ -f "$CONFIG_PATH" ]]; then
  echo "Agent already registered ($CONFIG_PATH exists); skipping registration."
else
  echo "Registering agent with the Vecta dashboard ..."
  "$AGENT_BIN" register --token "$TOKEN"
fi

# --- Cron ----------------------------------------------------------------------

echo "$CRON_LINE" > "$CRON_FILE"
chmod 0644 "$CRON_FILE"
echo "Installed cron entry: $CRON_FILE"

# --- First heartbeat -----------------------------------------------------------

echo "Running an initial pass to report this server as online ..."
if "$AGENT_BIN" run; then
  :
else
  echo "Note: initial run failed (expected if no repository password is set yet)."
  echo "      Run 'sudo vecta-agent repo init <DESTINATION> --generate' (see below)."
fi

echo
echo "Done. Vecta agent installed and scheduled."
echo
echo "Next steps:"
echo "  1. Create a backup job in the dashboard (this defines source and destination)."
echo "  2. Set this machine's repository password ONCE, as root:"
echo "       sudo vecta-agent repo init <DESTINATION> --generate"
echo "     (use the destination from your first job). This one password is used"
echo "     for every repo on this machine and cannot be recovered if lost."
echo "  3. Backups run automatically every 2 minutes; new destinations are"
echo "     auto-initialized - no further repo init needed."