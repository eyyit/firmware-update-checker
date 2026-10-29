# Firmware builder and tester

Host-side orchestration with short-lived Docker workers. No permanent service,
database, web UI, firmware upload command, or production signing key is required.

## Local configuration

The public source is a deployment template. Keep installation-specific settings
in the ignored `config.json`; only `config.example.json` belongs in Git.
The example contains one DFM profile. Add other profiles locally, with source
paths and configuration filenames appropriate for your installation.

```sh
cp config.example.json config.json
# Edit config.json before running any checks or builds.
docker build -t local/firmware-tests:maintained -f Dockerfile.tests .
python3 -B -m unittest -v test_orchestrate
```

Review the dependency baselines and pinned images in the example; they are not
a claim that these are the newest or currently accepted versions. Set writable
state and metrics paths, prepare the metrics directory, and configure monitoring
before installing a schedule. `--config /path/to/config.json` selects a different
local configuration. Configuration may contain operational details and must not
be published. Do not mount it into workers or add production credentials.

The host-side Docker build above uses this checkout as its build context. Keep
private files excluded with `.dockerignore`, as well as from Git.

## Scheduled behavior

This is an on-demand job, not an always-on container or service.

On this host, root's existing Monday 09:00 Docker maintenance entry point calls
`/opt/repos/docker/run-monday-maintenance.sh`. It refreshes Docker first; only
when that succeeds does it call `run-weekly.sh` from this repository:

1. Check upstream releases, public advisories and the vendor bulletin index.
2. If the check succeeds, test and build eligible stable patch candidates.
3. Send changed review/failure/candidate notices through the existing mail relay.

The source checkouts remain separate from Docker maintenance. Build workers
start for the job and are removed afterward. No firmware service is added to
Compose and nothing is automatically flashed or deployed.

There is no daily check, hourly notification watcher, or separate 11:30 build
job. Notifications and pending-mail retries are evaluated during scheduled job
completion. The default stale thresholds are eight days for this weekly cadence;
without an independent monitor, a completely missed Monday run cannot email
about itself until something invokes the checker/notifier again.

A global lock prevents overlapping firmware runs. Unchanged successful inputs
reference their previous run instead of recompiling. The weekly build refreshes
the small host-test image without cached apt layers and records its exact image
ID. This changes the test environment, not deployed device firmware.

The accepted baseline is recorded in ignored `config.json`; candidates do not
overwrite it. New framework series need explicit review. Docker maintenance
failure stops the chain and is reported by the existing cron monitoring wrapper.
`cron.example` documents this integration and a standalone alternative for other
hosts; do not install both.

## Profiles and source ownership

Profiles refer to committed source checkouts that you maintain separately.
The public example targets the independent DFM ESP-IDF project. ESPHome profiles
need a compatible source tree containing the expected host tests and build
inputs; this runner is not a generic installer for arbitrary ESPHome YAML.

Source changes need an explicit, secret-free snapshot or commit. Both the source
Git commit and candidate framework image digest are recorded. The runner does
not silently migrate frameworks or synchronize development machines.

## Isolation and checks

Workers run as UID/GID 1000, with capabilities dropped, no-new-privileges, 2 CPUs,
2 GiB RAM, a PID limit and bounded command/log sizes. A timeout removes the named
worker. No worker mounts the Docker socket. Online compilation uses a dedicated
`firmware-build` bridge with private IPv4 destinations and host access blocked;
IPv6 is disabled by scoped firewall rules. Public DNS is explicit. Offline tests
stay on network `none`. `ensure-network.sh` reinstates these rules before builds;
it does not change VLANs or existing containers. Source is a clean Git archive;
uncommitted tracked changes block a build.

DFM: run the real C/Python/JS suite in a dedicated test image, using cJSON from
the candidate SDK image. Compile twice in clean directories and compare the
application, bootloader and partition table. The newer SDK's component manager
is disabled because this project has no external managed dependencies.

ESPHome: run existing fault tests and UI tests, then compile each profile in
the selected pinned ESPHome container. Dependency downloads use a per-profile,
per-image cache. Online compilation uses only fake credentials. These artifacts
are **test-only and cannot be deployed**, even if all host tests pass. Production
credential preflight and authenticated OTA verification are separate release gates.
Reproducibility is not claimed for ESP8266 outputs with build metadata.

No emulator has been validated with the exact target image and sensor model yet.
Reports explicitly say `NOT_RUN_UNVALIDATED`, rather than passing a dummy model.
Real radio, sensor and OTA acceptance still requires an approved hardware pilot.

## Results and observability

Private state and artifacts use the configured `state` directory, mode 0700.
Each run stores `report.json`, stage logs and per-profile `candidate.json` with
source commit, versions/digests, binary hash/size, test state and missing gates.
Successful source/build working directories are cleaned; exact commits and test
artifacts remain. Downloads stay in the private cache.

Public, low-cardinality job metrics are written atomically to the existing
configured node-exporter textfile directory. They include last attempt/success, failed run
state and advisories needing review. Check an attempt newer than success and a
weekly success older than eight days. These metrics do not mean firmware is secure.

`firmware_maintenance_advisory_coverage_complete` is deliberately zero: repository
feeds and a watched vendor bulletin index do not establish the applicability
of every vendor bulletin, transitive dependency or private issue. The report preserves that limitation. Support expiry is recorded where
authoritative dates are configured; otherwise it requires policy review.

Cron output goes to `/var/log/firmware-update-checker/runner.log`, rotated by logrotate.
Direct firmware email is configured locally. No Grafana alert rules are
required. The existing root cron/script-monitor route also reports failures
of the combined Monday maintenance command.

Runs older than 90 days are pruned only under this tool's own run directory and
only when unsigned and undeployed. The latest reports and up to three recent
successful candidates per profile are protected. A free-space check stops work
before consuming the host's reserve. Recovery backups are never managed here.

## Manual operation

Run on Linux as the trusted host operator:

```sh
cd /opt/repos/firmware-update-checker
python3 -m unittest -v test_orchestrate
python3 orchestrate.py check
python3 orchestrate.py build
python3 orchestrate.py build --profile dfm --force
```

`--force` rebuilds; it does not override failed tests or enable uploads. Build
output is not a production release. The next release process must rebuild with
approved private inputs where needed, review local component differences,
verify source/identity and sign through a separate trusted step.

Disable scheduling by removing only this tool's cron file. Existing Docker
maintenance, the sensors, Prometheus and other cron entries remain independent.

## Existing installation compatibility

The source directory and scheduled entry points use `firmware-update-checker`.
Existing operators may keep their configured state, log and metrics-file paths;
renaming the source does not migrate or delete historical job results. The
Prometheus metric prefix remains `firmware_maintenance_` for time-series
compatibility. Local `config.json` controls state and metrics destinations.

## Direct email notifications

Optional notifications use the host's `/usr/sbin/sendmail`; no SMTP password
belongs in this repository or in build workers. Enable `notifications` in the
ignored local configuration and choose the sender/recipient for your host.
The public example is disabled and uses reserved example addresses.

`run-scheduled.sh` reports successful and unsuccessful command exits. Pending
mail is retried at the next invocation; there is no hourly watcher. Run
`python3 -B notify.py --initialize` once to establish the baseline for historical
candidates, then run `python3 -B notify.py` to send current notices. Scheduled
wrapper runs notify immediately on completion. Direct manual orchestrator runs
need an explicit notifier invocation if immediate email is wanted.

Emails cover failed checks/builds, overdue or stalled jobs, changed dependency
updates/advisories, changed vendor bulletin indexes, new tested candidates and
failure recovery. Unchanged conditions are not resent. Failed local mail
submission keeps events pending for the next attempt. Mail acceptance by the
host relay does not prove delivery to the recipient's inbox. If this host,
cron, or its mail relay is down, an external monitor is still required.

Candidate messages mean test results need human review, not that production
firmware is ready. Test artifacts may contain fake credentials. Hardware
acceptance, approved private inputs, signing and deployment remain separate;
this runner never flashes devices or pushes Git source automatically.

Policy and notification tests run without real email, Docker or firmware:

```sh
python3 -B -m unittest -v test_orchestrate test_notify
```

### Email previews

Live alerts include an HTML version with status, explanation and next steps,
plus a plain-text fallback. No remote images or attachments are required.
To send the operator a full sample catalogue explicitly, run:

```sh
python3 -B notify.py --send-previews
```

This sends 14 clearly numbered PREVIEW messages covering check/build failures,
overdue and stalled jobs, framework reviews, vendor bulletins, tested candidates,
recoveries and a combined summary. Samples use synthetic data, do not execute
checks or builds, and do not update the live notification ledger. Do not add
this preview command to cron.
