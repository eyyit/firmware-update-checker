"""Send clearly labeled samples without touching live notification history."""

import copy
import json
from pathlib import Path
import tempfile
import time

import notify as n


def preview_cases() -> list[tuple[str, list[dict]]]:
  now = time.time()
  cases = []
  with tempfile.TemporaryDirectory(prefix='firmware-email-preview-') as name:
    root = Path(name)
    config = {
      'state': str(root),
      'notifications': {},
      'profiles': [{'id': 'dfm'}, {'id': 'climate-sensor'}],
    }
    healthy = {
      mode: {'ok': True, 'success': now, 'attempt': now, 'running': False}
      for mode in ['check', 'build']
    }
    for mode in ['check', 'build']:
      state = copy.deepcopy(healthy)
      state[mode]['ok'] = False
      n.atomic_json(root / 'status.json', state)
      n.atomic_json(
        root / f'latest-{mode}.json',
        {
          'error': 'www.espressif.com/advisories: HTTP 403 after 3 attempts'
          if mode == 'check'
          else 'One or more firmware profiles failed',
        },
      )
      cases.append(
        (f'{mode} failed', [n.collect_events(config, now)[f'failure:{mode}']])
      )
    for mode in ['check', 'build']:
      state = copy.deepcopy(healthy)
      state[mode]['success'] = now - 10 * 86400
      n.atomic_json(root / 'status.json', state)
      cases.append(
        (f'{mode} overdue', [n.collect_events(config, now)[f'stale:{mode}']])
      )
    for mode in ['check', 'build']:
      state = copy.deepcopy(healthy)
      state[mode].update(running=True, attempt=now - 12 * 3600)
      n.atomic_json(root / 'status.json', state)
      cases.append(
        (f'{mode} stalled', [n.collect_events(config, now)[f'stalled:{mode}']])
      )
    state = copy.deepcopy(healthy)
    state['dependencies'] = {
      'idf': {
        'status': 'checked',
        'pinned': 'v5.5.5',
        'candidate': 'v5.5.6',
        'latest_stable': 'v6.0.3',
        'advisories': [{'id': 'GHSA-example-review'}],
      },
      'esphome': {
        'status': 'checked',
        'pinned': '2026.10.0',
        'candidate': '2026.10.1',
        'latest_stable': '2026.10.1',
        'advisories': [],
        'support_status': 'update_requires_review',
      },
    }
    n.atomic_json(root / 'status.json', state)
    for dependency in ['idf', 'esphome']:
      cases.append(
        (
          f'{dependency} review',
          [n.collect_events(config, now)[f'review:{dependency}']],
        )
      )
    n.atomic_json(
      root / 'latest-check.json',
      {
        'vendor_index': {
          'sha256': 'sample-index',
          'bulletins': ['sample-bulletin.pdf'],
          'url': 'https://www.espressif.com/en/support/documents/advisories',
        }
      },
    )
    cases.append(
      (
        'vendor advisory review',
        [n.collect_events(config, now)['vendor-index']],
      )
    )
    run_id = '20261028T120000Z-aaaaaaaa'
    for profile in ['dfm', 'climate-sensor']:
      n.atomic_json(
        root / f'{profile}-last.json',
        {
          'run_id': run_id,
          'fingerprint': 'sample-candidate',
        },
      )
      candidate = root / 'runs' / run_id / profile
      candidate.mkdir(parents=True)
      n.atomic_json(
        candidate / 'candidate.json',
        {
          'state': 'HOST_TESTED_AWAITING_RELEASE_REVIEW',
          'deployable': False,
        },
      )
      cases.append(
        (
          f'{profile} candidate',
          [n.collect_events(config, now)[f'candidate:{profile}']],
        )
      )
    for mode in ['check', 'build']:
      cases.append(
        (
          f'{mode} recovered',
          [
            n.event(
              mode + ': monitoring recovered',
              'The previously reported failure or overdue condition has cleared.',
              'sample-recovery',
            )
          ],
        )
      )
    cases.append(
      ('combined summary', [cases[0][1][0], cases[7][1][0], cases[9][1][0]])
    )
    for _, events in cases:
      for item in events:
        item['detail'] = item['detail'].replace(
          str(root), '/example/firmware-reports'
        )
  return cases


def send_previews(settings: dict) -> dict:
  cases = preview_cases()
  results = []
  for index, (name, events) in enumerate(cases, 1):
    message_id = n.send_email(
      settings, events, preview_label=f'PREVIEW {index:02d}/{len(cases)}'
    )
    results.append({'preview': name, 'message_id': message_id})
    print(json.dumps(results[-1]), flush=True)
    time.sleep(1)
  return {'state': 'PREVIEWS_SUBMITTED', 'count': len(results)}
