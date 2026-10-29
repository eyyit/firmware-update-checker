# Firmware maintenance runner

Use two-space Python indentation, single quotes, and an 80-column limit.
`ruff.toml` records these preferences. Run policy tests and formatting/lint checks.

Linux source is `/opt/repos/firmware-update-checker`. The host-side runner is trusted
to launch Docker; workers must never receive the Docker socket, signing key,
production secrets, or an upload command. Scheduled builds cannot deploy.

Test artifacts use fake credentials and MUST NOT be flashed. Keep `deployable`
false. Public repository advisory coverage is partial; unknown is never clean.
Do not conflate successful metadata fetch, clean tests, security assurance,
emulator acceptance, hardware acceptance, and release approval.

Preserve the DFM's independent firmware and the existing ESPHome climate fleet.
Review local ESPHome component patches before production framework upgrades.
Source repositories are versioned Linux snapshots. Do not silently overwrite
Windows development sources or import private files into Git.

Deployment of a separately approved release is outside this runner. It must
still verify device identity, use the proper OTA format, and check health after.
