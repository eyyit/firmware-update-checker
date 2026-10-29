# Initial source publication

Publish a clean source snapshot, not the existing private Git history.
Earlier local commits contain installation configuration and compiled Python
caches; removing those files at HEAD does not remove them from history.
Preserve the original local history privately before any migration. Never
force-push it to a public repository or use `git push --mirror`.

The reviewed public snapshot must contain only the allowlisted source,
tests, generic examples and documentation. Exclude `config.json`, Python
caches, private files, job state, logs and generated firmware. Check the
complete candidate for secrets and private identifiers, run the policy suite
and Ruff checks, and inspect the staged diff before an initial public commit.

The live ignored `config.json` remains the default used by scheduled jobs.
Public preparation does not authorize changing schedules, building firmware,
signing, uploading firmware or deploying to hardware.

Create an empty GitHub repository named `firmware-update-checker`; do not initialize
it with a README, .gitignore or license. Repository creation and the eventual
source commit/push are separate from firmware release or deployment approval.
