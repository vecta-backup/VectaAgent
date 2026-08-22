#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Detect if we're inside a Docker container (no venv needed then)
if [ -f /.dockerenv ] || grep -q docker /proc/1/cgroup 2>/dev/null; then
    echo "Building inside Docker container..."
    pyinstaller vecta-agent.spec
else
    echo "Building in local environment..."
    python3 -m venv .build-venv
    .build-venv/bin/pip install --upgrade pip
    .build-venv/bin/pip install -r requirements.txt pyinstaller
    .build-venv/bin/pyinstaller vecta-agent.spec
fi

echo "Built: dist/vecta-agent"
