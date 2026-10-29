#!/usr/bin/env python3
"""Build-only firmware maintenance without signing or deployment."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import signal
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

UTC = dt.timezone.utc


def version(text: str) -> tuple[int, int, int]:
  """Reject prereleases and arbitrary tags; only numeric stable versions."""
  match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', text)
  if not match:
    raise ValueError(f'Not a stable version: {text}')
  return tuple(map(int, match.groups()))


def select_patch(current: str, releases: list[dict]) -> str:
  base = version(current)
  eligible = [
    r['tag_name']
    for r in releases
    if not r.get('draft')
    and not r.get('prerelease')
    and re.fullmatch(r'v?\d+\.\d+\.\d+', r.get('tag_name', ''))
    and version(r['tag_name'])[:2] == base[:2]
    and version(r['tag_name']) >= base
  ]
  return max(eligible, key=version) if eligible else current


def affected(current: str, expression: str) -> bool | None:
  """Unknown range syntax stays unknown, never silently unaffected."""
  value = version(current)
  clauses = expression.split(',')
  result = True
  for clause in clauses:
    match = re.fullmatch(r'\s*(>=|<=|>|<|=)?\s*v?(\d+\.\d+\.\d+)\s*', clause)
    if not match:
      return None
    other = version(match[2])
    result &= {
      '>=': value >= other,
      '<=': value <= other,
      '>': value > other,
      '<': value < other,
      '=': value == other,
    }[match[1] or '=']
  return result


def public_source(url: str, limit: int, accept: str) -> bytes:
  """Retry temporary refusals without accepting incomplete source coverage."""
  request = urllib.request.Request(
    url,
    headers={
      'User-Agent': 'firmware-update-checker/1.0',
      'Accept': accept,
    },
  )
  parts = urllib.parse.urlsplit(url)
  source = parts.hostname + parts.path
  for attempt in range(3):
    try:
      with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(limit + 1)
      if len(data) > limit:
        raise ValueError(f'{source}: response exceeds size limit')
      return data
    except urllib.error.HTTPError as exc:
      code = exc.code
      exc.close()
      retry = code in (403, 429, 500, 502, 503, 504)
      if not retry or attempt == 2:
        raise OSError(
          f'{source}: HTTP {code} after {attempt + 1} attempts'
        ) from None
    except urllib.error.URLError:
      if attempt == 2:
        raise OSError(f'{source}: network failure after 3 attempts') from None
    time.sleep((1, 3)[attempt])
  raise AssertionError('Unreachable request state')


def fetch(url: str) -> list | dict:
  return json.loads(
    public_source(url, 8 * 1024**2, 'application/vnd.github+json')
  )


def atomic_json(path: Path, value: object) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary = path.with_suffix(path.suffix + '.tmp')
  temporary.write_text(json.dumps(value, indent=2) + '\n')
  os.replace(temporary, path)


def run(
  args: list[str],
  *,
  cwd: Path | None = None,
  timeout: int = 120,
  log: Path | None = None,
) -> str:
  if log:
    with log.open('ab') as output:
      result = subprocess.run(
        args,
        cwd=cwd,
        stdout=output,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
        preexec_fn=lambda: resource.setrlimit(
          resource.RLIMIT_FSIZE, (50 * 1024**2, 50 * 1024**2)
        ),
      )
    if result.returncode:
      raise RuntimeError(
        f'Command failed ({result.returncode}); see {log.name}'
      )
    return ''
  result = subprocess.run(
    args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
  )
  if result.returncode:
    raise RuntimeError(f'{args[0]} failed: {result.stderr[-1500:]}')
  return result.stdout.strip()


def git(repo: str, *args: str) -> str:
  return run(
    [
      'git',
      '--no-optional-locks',
      '-c',
      f'safe.directory={repo}',
      '-C',
      repo,
      *args,
    ]
  )


def snapshot(repo: str, target: Path) -> str:
  revision = git(repo, 'rev-parse', 'HEAD')
  if git(repo, 'status', '--porcelain', '--untracked-files=no'):
    raise ValueError(f'Tracked source changes require a commit: {repo}')
  target.mkdir(parents=True)
  archive = target.parent / (target.name + '.tar')
  run(
    [
      'git',
      '-c',
      f'safe.directory={repo}',
      '-C',
      repo,
      'archive',
      '--output',
      str(archive),
      revision,
    ]
  )
  with tarfile.open(archive) as source:
    source.extractall(target, filter='data')
  archive.unlink()
  for path in [target, *target.rglob('*')]:
    if path.is_symlink():
      raise ValueError('Build sources may not contain symlinks')
    os.chown(path, 1000, 1000)
  return revision


def docker(
  image: str,
  work: Path,
  command: list[str],
  log: Path,
  *,
  online: bool = False,
  cwd: str = '/work',
  cache: Path | None = None,
  timeout: int = 1800,
) -> None:
  name = 'firmware-test-' + uuid.uuid4().hex[:12]
  args = [
    'docker',
    'run',
    '--rm',
    '--name',
    name,
    '--init',
    '--user',
    '1000:1000',
    '--log-driver=none',
    '--cap-drop=ALL',
    '--security-opt=no-new-privileges',
    '--cpus=2',
    '--memory=2g',
    '--pids-limit=256',
    '--network',
    'firmware-build' if online else 'none',
    '-e',
    'HOME=' + cwd,
    '-e',
    'CCACHE_DIR=' + cwd + '/.ccache',
    '-e',
    'IDF_COMPONENT_MANAGER=0',
    '-e',
    'IDF_COMPONENT_CACHE_PATH=' + cwd + '/.idf-components',
    '-e',
    'GIT_CONFIG_COUNT=1',
    '-e',
    'GIT_CONFIG_KEY_0=safe.directory',
    '-e',
    'GIT_CONFIG_VALUE_0=/opt/esp/idf',
    '-v',
    f'{work}:{cwd}',
    '-w',
    cwd,
  ]
  if online:
    args += ['--dns', '1.1.1.1']
  if cache:
    cache.mkdir(parents=True, exist_ok=True)
    os.chown(cache, 1000, 1000)
    args += ['-v', f'{cache}:{cwd}/.esphome']
  # Entrypoint override only: never inherit an upload/server command.
  args += ['--entrypoint', command[0], image, *command[1:]]
  try:
    run(args, log=log, timeout=timeout)
  finally:
    # A timed-out docker client must not leave a compiler running forever.
    subprocess.run(
      ['docker', 'rm', '-f', name], capture_output=True, timeout=30
    )


def image_digest(reference: str, log: Path) -> str:
  run(['docker', 'pull', reference], timeout=600, log=log)
  info = json.loads(run(['docker', 'image', 'inspect', reference]))[0]
  repository = reference.split('@')[0].rsplit(':', 1)[0]
  for digest in info.get('RepoDigests', []):
    if digest.split('@')[0] == repository:
      return digest
  raise ValueError(f'No registry digest resolved for {reference}')


def discover(config: dict) -> dict:
  """Fetch complete public repo feeds; partial/failing coverage is explicit."""
  results = {}
  for name, dependency in config['dependencies'].items():
    record = {
      'pinned': dependency['version'],
      'image': dependency['image'],
      'repository': dependency['repository'],
      'status': 'unknown',
    }
    try:
      base = 'https://api.github.com/repos/' + dependency['repository']
      releases = fetch(base + '/releases?per_page=100')
      record['candidate'] = select_patch(dependency['version'], releases)
      stable = [
        r['tag_name']
        for r in releases
        if not r.get('draft')
        and not r.get('prerelease')
        and re.fullmatch(r'v?\d+\.\d+\.\d+', r.get('tag_name', ''))
      ]
      record['latest_stable'] = max(stable, key=version) if stable else None
      advisories = []
      for page in range(1, 21):
        batch = fetch(base + f'/security-advisories?per_page=100&page={page}')
        advisories.extend(batch)
        if len(batch) < 100:
          break
      else:
        raise ValueError('Advisory pagination exceeds supported limit')
      record['advisories'] = []
      for advisory in advisories:
        checks = [
          affected(dependency['version'], v.get('vulnerable_version_range', ''))
          for v in advisory.get('vulnerabilities', [])
        ]
        if not checks or any(x is not False for x in checks):
          record['advisories'].append(
            {
              'id': advisory['ghsa_id'],
              'severity': advisory.get('severity'),
              'url': advisory['html_url'],
              'applicability': 'version_match_needs_review'
              if True in checks
              else 'needs_review',
            }
          )
      # Support expiry is recorded, not inferred from latest tags.
      expires = dependency.get('support_until')
      record['support_until'] = expires
      record['support_expired'] = bool(
        expires and dt.date.today() >= dt.date.fromisoformat(expires)
      )
      if dependency.get('latest_stable_only'):
        current = version(dependency['version'])
        latest = record['latest_stable']
        record['support_status'] = (
          'latest_stable'
          if latest and current == version(latest)
          else 'update_requires_review'
        )
      else:
        record['support_status'] = (
          'recorded' if expires else 'requires_policy_review'
        )
      record['status'] = 'checked'
      record['coverage'] = (
        'Public repo advisories only; '
        'transitive/vendor bulletins require review.'
      )
    except (OSError, ValueError, KeyError, TypeError) as exc:
      record['error'] = str(exc)
    results[name] = record
  return results


def vendor_index() -> dict:
  """Watch official bulletin links; leave device applicability to review."""
  url = 'https://www.espressif.com/en/support/documents/advisories'
  raw = public_source(url, 2 * 1024**2, 'text/html')
  links = re.findall(r'href=["\x27]([^"\x27]+)', raw.decode())
  selected = sorted({link for link in links if '/advisory_downloads/' in link})
  if not selected:
    raise ValueError('Vendor index parser found no security bulletins')
  digest = hashlib.sha256(json.dumps(selected).encode()).hexdigest()
  return {
    'url': url,
    'sha256': digest,
    'bulletins': selected,
    'applicability': 'requires_device_specific_review',
  }


def build_profile(
  profile: dict, dependency: dict, config: dict, stage: Path
) -> dict:
  work = stage / 'source'
  revision = snapshot(profile['source'], work)
  log = stage / 'build.log'
  tag = dependency['candidate'].lstrip('v')
  digest = dependency['resolved_image']
  record = {
    'source': profile['source'],
    'commit': revision,
    'candidate_version': dependency['candidate'],
    'image': digest,
    'deployable': False,
    'emulator': 'NOT_RUN_UNVALIDATED',
    'hardware': 'NOT_RUN_REQUIRES_APPROVAL',
    'signing': 'NOT_PERFORMED',
    'credential_mode': 'no_production_credentials',
  }
  if profile['framework'] == 'esp-idf':
    lock = work / 'tools/build-env.json'
    data = json.loads(lock.read_text())
    data.update(image=digest, sdk='v' + tag)
    atomic_json(lock, data)
    sdk = work / 'artifacts/sdk'
    sdk.mkdir(parents=True)
    # Exact candidate cJSON is extracted by host, not a Docker socket in tests.
    for filename in ('cJSON.c', 'cJSON.h'):
      text = run(
        [
          'docker',
          'run',
          '--rm',
          '--network',
          'none',
          '--entrypoint',
          'cat',
          digest,
          '/opt/esp/idf/components/json/cJSON/' + filename,
        ]
      )
      (sdk / filename).write_text(text + '\n')
    for path in [*work.rglob('*')]:
      if path.is_file() or path.is_dir():
        os.chown(path, 1000, 1000)
    docker(config['test_image'], work, ['sh', 'tools/test-all.sh'], log)
    for directory in ('build_a', 'build_b'):
      docker(
        digest,
        work,
        [
          '/opt/esp/entrypoint.sh',
          'idf.py',
          '-B',
          directory,
          '-DIDF_TARGET=esp32c6',
          'reconfigure',
          'build',
        ],
        log,
      )
    binaries = [
      'airscape_dfm.bin',
      'bootloader/bootloader.bin',
      'partition_table/partition-table.bin',
    ]
    for binary in binaries:
      if (work / 'build_a' / binary).read_bytes() != (
        work / 'build_b' / binary
      ).read_bytes():
        raise ValueError('Reproducibility failure: ' + binary)
    artifact = work / 'build_a/airscape_dfm.bin'
    record['reproducible'] = True
    record['candidate_lock'] = data
  elif profile['framework'] == 'esphome':
    # Fake credentials. These images must never be deployed.
    (work / 'secrets.yaml').write_text(
      'wifi_ssid: "firmware-test-isolated"\n'
      'wifi_password: "test-only-not-production"\n'
      'ota_password: "' + 'abcd0123456789ef' * 4 + '"\n'
    )
    os.chown(work / 'secrets.yaml', 1000, 1000)
    selected = work / profile['configuration']
    if selected != work / 'sensor-office.yaml':
      shutil.copyfile(selected, work / 'sensor-office.yaml')
    docker(digest, work, ['sh', 'tests/run-fault-tests.sh'], log, cwd='/config')
    docker(
      config['test_image'],
      work,
      ['node', '--test', 'tests/test_sensor_ui.mjs'],
      log,
      cwd='/config',
    )
    cache = (
      Path(config['state'])
      / 'cache'
      / profile['id']
      / hashlib.sha256(digest.encode()).hexdigest()[:16]
    )
    docker(
      digest,
      work,
      ['esphome', 'compile', 'sensor-office.yaml'],
      log,
      cwd='/config',
      online=True,
      cache=cache,
    )
    # Reuse downloads, never a previous linked application.
    found = list(cache.glob('build/*/.pioenvs/*/firmware.bin'))
    if len(found) != 1:
      raise ValueError('Expected exactly one compiled firmware')
    artifact = found[0]
    record['reproducible'] = 'NOT_CLAIMED_ESP8266_METADATA'
    record['online_compile'] = 'test credentials only; packages require network'
    record['local_component_review'] = (
      'REQUIRED before production rebuild on changed upstream'
    )
  else:
    raise ValueError('Unsupported framework')
  binary = stage / 'firmware.test-only.bin'
  shutil.copyfile(artifact, binary)
  record.update(
    sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
    size=binary.stat().st_size,
    state='HOST_TESTED_AWAITING_RELEASE_REVIEW',
  )
  atomic_json(stage / 'candidate.json', record)
  coverage = work / 'artifacts/coverage/summary.json'
  if coverage.exists():
    shutil.copyfile(coverage, stage / 'coverage.json')
  # Git commit preserves source; private signing files never live here.
  if work.resolve().parent != stage.resolve() or work.is_symlink():
    raise ValueError('Unsafe temporary source directory')
  shutil.rmtree(work)
  return record


def metrics(config: dict, state: dict) -> None:
  destination = Path(config['metrics'])
  values = [
    f'firmware_maintenance_last_{key}_attempt_timestamp_seconds '
    f'{item["attempt"]}\n'
    f'firmware_maintenance_last_{key}_success_timestamp_seconds '
    f'{item.get("success", 0)}\n'
    f'firmware_maintenance_last_{key}_ok {int(item.get("ok", False))}\n'
    f'firmware_maintenance_{key}_running {int(item.get("running", False))}\n'
    for key, item in state.items()
    if key in ('check', 'build')
  ]
  temporary = destination.with_suffix('.tmp')
  dependencies = state.get('dependencies', {})
  pending = sum(len(v.get('advisories', [])) for v in dependencies.values())
  values.append(f'firmware_maintenance_advisories_needing_review {pending}\n')
  for name, dependency in dependencies.items():
    if not re.fullmatch(r'[a-z0-9_]+', name):
      continue
    newer = False
    if dependency.get('candidate'):
      newer = version(dependency['candidate']) > version(dependency['pinned'])
    review = dependency.get('support_expired', False) or (
      dependency.get('support_status') == 'update_requires_review'
    )
    prefix = 'firmware_maintenance_dependency'
    values.append(
      f'{prefix}_update_available{{dependency="{name}"}} {int(newer)}\n'
    )
    values.append(
      f'{prefix}_support_review{{dependency="{name}"}} {int(review)}\n'
    )
  values.append('firmware_maintenance_advisory_coverage_complete 0\n')
  temporary.write_text(''.join(values))
  os.replace(temporary, destination)
  destination.chmod(0o644)


def prune(root: Path) -> None:
  """Prune old test-only runs while retaining recent candidates."""
  protected = set()
  for last in root.glob('*-last.json'):
    item = json.loads(last.read_text())
    protected.update(item.get('history', []) + [item['run_id']])
  for latest in root.glob('latest-*.json'):
    protected.add(json.loads(latest.read_text())['run_id'])
  for directory in (root / 'runs').glob('*'):
    if (
      directory.is_symlink()
      or directory.resolve().parent != (root / 'runs').resolve()
      or not re.fullmatch(r'\d{8}T\d{6}Z-[a-f0-9]{8}', directory.name)
    ):
      continue
    report = directory / 'report.json'
    if (
      directory.name in protected
      or time.time() - directory.stat().st_mtime < 90 * 86400
      or not report.exists()
    ):
      continue
    data = json.loads(report.read_text())
    if data.get('deployed') is False and data.get('signed') is False:
      shutil.rmtree(directory)


def execute(
  config: dict,
  mode: str,
  force: bool = False,
  profiles: list[str] | None = None,
) -> dict:
  root = Path(config['state'])
  root.mkdir(parents=True, exist_ok=True, mode=0o700)
  os.chmod(root, 0o700)
  with (root / 'run.lock').open('w') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    prune(root)
    state_path = root / 'status.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    previous_success = state.get(mode, {}).get('success', 0)
    started = time.time()
    state[mode] = {
      'attempt': started,
      'success': previous_success,
      'ok': False,
      'running': True,
    }
    atomic_json(state_path, state)
    metrics(config, state)
    run_id = (
      dt.datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
    )
    directory = root / 'runs' / run_id
    directory.mkdir(parents=True, mode=0o700)
    report = {
      'run_id': run_id,
      'mode': mode,
      'started': started,
      'deployed': False,
      'signed': False,
      'profiles': {},
      'selected_profiles': profiles or 'all',
      'state': 'RUNNING',
    }
    try:
      report['orchestrator_commit'] = git(
        str(Path(__file__).resolve().parent), 'rev-parse', 'HEAD'
      )
      report['config_sha256'] = hashlib.sha256(
        json.dumps(config, sort_keys=True).encode()
      ).hexdigest()
      if shutil.disk_usage(root).free < config.get(
        'minimum_free_bytes', 5 * 1024**3
      ):
        raise RuntimeError('Insufficient disk space; retaining prior artifacts')
      report['vendor_index'] = vendor_index()
      dependencies = discover(config)
      report['dependencies'] = dependencies
      state['dependencies'] = dependencies
      atomic_json(directory / 'report.json', report)
      if any(v['status'] != 'checked' for v in dependencies.values()):
        raise RuntimeError('Dependency/advisory source unavailable; see report')
      if mode == 'build':
        if config.get('refresh_test_image'):
          run(
            [
              'docker',
              'build',
              '--pull',
              '--no-cache',
              '-f',
              str(Path(__file__).with_name('Dockerfile.tests')),
              '-t',
              config['test_image_tag'],
              str(Path(__file__).resolve().parent),
            ],
            timeout=900,
            log=directory / 'refresh-test-image.log',
          )
        if config.get('isolate_online_builds'):
          run(['sh', str(Path(__file__).with_name('ensure-network.sh'))])
        if config.get('test_image_tag'):
          config['test_image'] = run(
            [
              'docker',
              'image',
              'inspect',
              config['test_image_tag'],
              '--format',
              '{{.Id}}',
            ]
          )
        report['test_image'] = config['test_image']
        for name, dependency in dependencies.items():
          if name not in config['dependencies']:
            continue
          original = config['dependencies'][name]
          repository = original['image'].split('@')[0]
          prefix = 'v' if original['version'].startswith('v') else ''
          tag = prefix + dependency['candidate'].lstrip('v')
          dependency['resolved_image'] = image_digest(
            repository + ':' + tag, directory / 'resolve-images.log'
          )
        for profile in config['profiles']:
          if profiles and profile['id'] not in profiles:
            continue
          dependency = dependencies[profile['dependency']]
          fingerprint = hashlib.sha256(
            json.dumps(
              [
                git(profile['source'], 'rev-parse', 'HEAD'),
                dependency.get('candidate'),
                dependency.get('resolved_image'),
                profile,
                config['test_image'],
              ],
              sort_keys=True,
            ).encode()
          ).hexdigest()
          last = root / (profile['id'] + '-last.json')
          if (
            not force
            and last.exists()
            and json.loads(last.read_text()).get('fingerprint') == fingerprint
          ):
            report['profiles'][profile['id']] = {
              'state': 'UNCHANGED',
              'previous': json.loads(last.read_text())['run_id'],
            }
            continue
          stage = directory / profile['id']
          stage.mkdir()
          try:
            result = build_profile(profile, dependency, config, stage)
            report['profiles'][profile['id']] = result
            old = json.loads(last.read_text()) if last.exists() else {}
            history = (
              ([old['run_id']] + old.get('history', []))[:2] if old else []
            )
            atomic_json(
              last,
              {
                'fingerprint': fingerprint,
                'run_id': run_id,
                'history': history,
              },
            )
          except (
            OSError,
            ValueError,
            RuntimeError,
            subprocess.TimeoutExpired,
          ) as exc:
            report['profiles'][profile['id']] = {
              'state': 'FAILED',
              'error': str(exc),
            }
          atomic_json(directory / 'report.json', report)
        if any(r['state'] == 'FAILED' for r in report['profiles'].values()):
          raise RuntimeError('One or more firmware profiles failed')
      report['state'] = (
        'COMPLETE_BUILD_ONLY' if mode == 'build' else 'CHECKED_PARTIAL_COVERAGE'
      )
      state[mode].update(ok=True, success=time.time())
    except (
      OSError,
      ValueError,
      RuntimeError,
      subprocess.TimeoutExpired,
    ) as exc:
      report.update(state='FAILED', error=str(exc))
    finally:
      state[mode]['running'] = False
      report['ended'] = time.time()
      atomic_json(directory / 'report.json', report)
      atomic_json(root / ('latest-' + mode + '.json'), report)
      atomic_json(state_path, state)
      metrics(config, state)
    return report


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('mode', choices=['check', 'build'])
  parser.add_argument(
    '--config', type=Path, default=Path(__file__).with_name('config.json')
  )
  parser.add_argument('--force', action='store_true')
  parser.add_argument('--refresh-test-image', action='store_true')
  parser.add_argument('--profile', action='append')
  args = parser.parse_args()
  os.umask(0o077)

  def interrupted(signum: int, frame: object) -> None:
    raise InterruptedError('Firmware job interrupted')

  signal.signal(signal.SIGTERM, interrupted)
  config = json.loads(args.config.read_text())
  config['refresh_test_image'] = args.refresh_test_image
  try:
    result = execute(config, args.mode, args.force, args.profile)
  except BlockingIOError:
    print('Existing firmware run owns lock; skipped duplicate')
    return
  print(
    json.dumps(
      {'run_id': result['run_id'], 'state': result['state'], 'deployed': False}
    )
  )
  if result['state'] == 'FAILED':
    raise SystemExit(1)


if __name__ == '__main__':
  main()
