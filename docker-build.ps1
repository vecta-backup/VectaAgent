# Build the Vecta agent binary inside a Linux Docker container.
# Run from PowerShell in the repo root.

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "Building Docker image..."
docker build -f "$repo\Dockerfile.build" -t vecta-builder $repo

Write-Host "Building binary inside container..."
docker run --rm -v "${repo}:/build" -w /build vecta-builder bash build.sh

Write-Host "Testing binary..."
docker run --rm -v "${repo}:/build" -w /build vecta-builder bash -c "./dist/vecta-agent version"

Write-Host "Done: dist/vecta-agent"
