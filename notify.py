"""Send deduplicated operator emails through the host mail relay."""

from __future__ import annotations

import argparse
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import fcntl
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

from email_formatting import render_email


def read_json(path: Path, default: dict | None = None) -> dict:
  if not path.exists():
    return {} if default is None else default
  return json.loads(path.read_text())


def atomic_json(path: Path, data: dict) -> None:
  temporary = path.with_suffix('.tmp')
  temporary.write_text(json.dumps(data, indent=2) + '\n')
  temporary.chmod(0o600)
  temporary.replace(path)


def event(title: str, detail: str, identity: object) -> dict:
  return {
    'title': title,
    'detail': detail,
    'fingerprint': hashlib.sha256(
      json.dumps(identity, sort_keys=True).encode()
    ).hexdigest(),
  }


def failure_reason(report: dict) -> str:
  """Keep raw command errors, which could contain credentials, out of email."""
  error = str(report.get('error', ''))
  match = re.search(r'HTTP(?: Error)? (\d{3})', error)
  if match:
    source = (
      'Espressif advisory feed'
      if 'espressif.com' in error
      else 'GitHub metadata feed'
      if 'api.github.com' in error
      else 'Upstream request'
    )
    return f'{source} failed with HTTP {match[1]} after retries.'
  if 'disk space' in error.lower():
    return 'The configured free-space reserve was reached.'
  return 'A source request, test, build or local operation failed.'


def collect_events(config: dict, now: float) -> dict:
  root = Path(config['state'])
  state = read_json(root / 'status.json')
  wrapper = read_json(root / 'wrapper-status.json')
  events = {}
  settings = config['notifications']
  for mode, maximum in [('check', 8 * 86400), ('build', 8 * 86400)]:
    job = state.get(mode, {})
    recent_wrapper = wrapper.get(mode, {})
    failed_wrapper = recent_wrapper.get(
      'exit_code', 0
    ) != 0 and recent_wrapper.get('time', 0) >= job.get('attempt', 0)
    if failed_wrapper or (job and not job.get('running') and not job.get('ok')):
      report = read_json(root / f'latest-{mode}.json')
      reason = (
        'The scheduled command exited unsuccessfully.'
        if failed_wrapper and job.get('ok')
        else failure_reason(report)
      )
      events[f'failure:{mode}'] = event(
        f'Firmware {mode} failed',
        f'{reason}\nInspect {root}/latest-{mode}.json and runner.log.',
        reason,
      )
    limit = settings.get(f'{mode}_stale_seconds', maximum)
    if not job.get('success') or now - job['success'] > limit:
      events[f'stale:{mode}'] = event(
        f'Firmware {mode} is overdue',
        f'No successful {mode} within {limit // 3600} hours. '
        'Check the schedule, host and source availability.',
        limit,
      )
    runtime_limit = 900 if mode == 'check' else 10 * 3600
    if job.get('running') and now - job.get('attempt', now) > runtime_limit:
      events[f'stalled:{mode}'] = event(
        f'Firmware {mode} may be stalled',
        'The recorded run exceeded its expected duration. Inspect the '
        'process and logs before stopping anything.',
        job.get('attempt'),
      )

  # Failed checks do not turn old dependency data into a fresh finding.
  if state.get('check', {}).get('ok'):
    for name, dependency in state.get('dependencies', {}).items():
      if dependency.get('status') != 'checked':
        continue
      pinned = dependency.get('pinned')
      candidate = dependency.get('candidate')
      latest = dependency.get('latest_stable')
      advisories = dependency.get('advisories', [])
      review = dependency.get('support_expired') or (
        dependency.get('support_status') == 'update_requires_review'
      )
      if not (
        (candidate and candidate != pinned)
        or (latest and latest != pinned)
        or advisories
        or review
      ):
        continue
      identity = [
        pinned,
        candidate,
        latest,
        sorted(a['id'] for a in advisories),
        bool(review),
      ]
      events[f'review:{name}'] = event(
        f'{name}: update or security review needed',
        f'Configured baseline: {pinned}. Eligible patch: {candidate}. '
        f'Latest stable overall: {latest}.\n'
        f'{len(advisories)} potentially relevant public advisories; '
        'applicability needs review. Newer series are not automatically '
        'accepted.\nReview the report, then schedule a test build if needed. '
        'Do not flash test artifacts.',
        identity,
      )
    report = read_json(root / 'latest-check.json')
    vendor = report.get('vendor_index', {})
    digest = vendor.get('sha256')
    if digest:
      events['vendor-index'] = event(
        'Espressif advisory index needs review',
        f'{len(vendor.get("bulletins", []))} vendor bulletin links are '
        'tracked. A changed index requires device-specific review; it is '
        'not proof that your devices are affected.\n' + vendor.get('url', ''),
        digest,
      )

  for profile in config.get('profiles', []):
    name = profile['id']
    if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
      continue
    last = read_json(root / f'{name}-last.json')
    run_id = last.get('run_id', '')
    if not re.fullmatch(r'\d{8}T\d{6}Z-[a-f0-9]{8}', run_id):
      continue
    candidate_path = root / 'runs' / run_id / name / 'candidate.json'
    candidate = read_json(candidate_path)
    if (
      candidate.get('state') == 'HOST_TESTED_AWAITING_RELEASE_REVIEW'
      and candidate.get('deployable') is False
    ):
      events[f'candidate:{name}'] = event(
        f'{name}: new tested candidate ready for review',
        f'Host tests/build checks passed. Report: {candidate_path}\n'
        'This is a test candidate, not production-ready firmware. Review '
        'dependencies and test results, complete hardware acceptance, and '
        'rebuild/sign with approved private inputs before deployment. '
        'Nothing has been flashed or deployed.',
        last.get('fingerprint', run_id),
      )
  return events


def send_email(
  settings: dict,
  events: list[dict],
  *,
  preview_label: str | None = None,
) -> str:
  recipients = settings.get('to', [])
  sender = settings.get('from', 'firmware-update-checker@localhost')
  addresses = [sender, *recipients]
  if not recipients or any(
    not isinstance(a, str) or not re.fullmatch(r'[\w.+@-]+', a)
    for a in addresses
  ):
    raise ValueError('Configure valid notification sender and recipients')
  message = EmailMessage()
  message['From'] = sender
  message['To'] = ', '.join(recipients)
  message['Date'] = formatdate(localtime=True)
  message['Message-ID'] = make_msgid()
  subject, plain, html = render_email(events, preview_label)
  message['Subject'] = subject
  message.set_content(plain)
  message.add_alternative(html, subtype='html')
  result = subprocess.run(
    ['/usr/sbin/sendmail', '-i', '-t'],
    input=message.as_bytes(),
    capture_output=True,
    timeout=30,
  )
  if result.returncode:
    raise OSError('Local mail submission failed; notification will retry')
  return str(message['Message-ID'])


def notify(
  config: dict,
  *,
  mode: str | None = None,
  exit_code: int | None = None,
  initialize: bool = False,
) -> dict:
  settings = config.get('notifications', {})
  if not settings.get('enabled'):
    return {'state': 'DISABLED'}
  root = Path(config['state'])
  root.mkdir(parents=True, exist_ok=True, mode=0o700)
  with (root / 'notify.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if mode is not None and exit_code is not None:
      wrapper = read_json(root / 'wrapper-status.json')
      wrapper[mode] = {'exit_code': exit_code, 'time': time.time()}
      atomic_json(root / 'wrapper-status.json', wrapper)
    active = collect_events(config, time.time())
    ledger_path = root / 'notifications.json'
    ledger = read_json(ledger_path)
    previous = ledger.get('active', {})
    seen = ledger.get('seen', dict(previous))
    if initialize:
      # Do not announce historical test builds as newly produced candidates.
      for key, value in active.items():
        if key.startswith('candidate:'):
          previous[key] = value['fingerprint']
          seen[key] = value['fingerprint']
      ledger['active'] = previous
      ledger['seen'] = seen
      atomic_json(ledger_path, ledger)
      return {'state': 'INITIALIZED'}
    outgoing = [
      value
      for key, value in active.items()
      if value['fingerprint']
      != (
        previous if key.startswith(('failure:', 'stale:', 'stalled:')) else seen
      ).get(key)
    ]
    for key in previous.keys() - active.keys():
      if key.startswith(('failure:', 'stale:', 'stalled:')):
        outgoing.append(
          event(
            key.split(':', 1)[1] + ': monitoring recovered',
            'The previously reported failure or overdue condition has cleared.',
            key,
          )
        )
    if outgoing:
      try:
        message_id = send_email(settings, outgoing)
      except (OSError, ValueError, subprocess.SubprocessError):
        ledger['last_delivery_ok'] = False
        atomic_json(ledger_path, ledger)
        raise
      ledger.update(last_delivery_ok=True, last_sent=time.time())
    else:
      message_id = None
    ledger['active'] = {k: v['fingerprint'] for k, v in active.items()}
    seen.update(ledger['active'])
    ledger['seen'] = seen
    atomic_json(ledger_path, ledger)
    return {
      'state': 'SUBMITTED' if outgoing else 'UNCHANGED',
      'event_count': len(outgoing),
      'message_id': message_id,
    }


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
    '--config', type=Path, default=Path(__file__).with_name('config.json')
  )
  parser.add_argument('--mode', choices=['check', 'build'])
  parser.add_argument('--exit-code', type=int)
  parser.add_argument('--initialize', action='store_true')
  parser.add_argument('--send-previews', action='store_true')
  args = parser.parse_args()
  if args.send_previews:
    from email_previews import send_previews

    config = read_json(args.config)
    print(json.dumps(send_previews(config['notifications'])))
    return
  try:
    result = notify(
      read_json(args.config),
      mode=args.mode,
      exit_code=args.exit_code,
      initialize=args.initialize,
    )
  except BlockingIOError:
    result = {'state': 'BUSY'}
  except (OSError, ValueError, subprocess.SubprocessError) as exc:
    print(f'Email notification failed ({type(exc).__name__}); will retry.')
    raise SystemExit(1) from None
  print(json.dumps(result))


if __name__ == '__main__':
  main()
