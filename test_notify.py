from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import notify as n


class NotificationTests(unittest.TestCase):
  def setUp(self):
    self.directory = tempfile.TemporaryDirectory()
    self.addCleanup(self.directory.cleanup)
    self.root = Path(self.directory.name)
    self.config = {
      'state': str(self.root),
      'profiles': [{'id': 'dfm'}],
      'notifications': {'enabled': True, 'to': ['operator@example.invalid']},
    }
    self.state = {
      'check': {'ok': True, 'success': time.time(), 'running': False},
      'build': {'ok': True, 'success': time.time(), 'running': False},
    }
    self.save()

  def save(self):
    n.atomic_json(self.root / 'status.json', self.state)

  def test_failures_recover_and_unchanged_conditions_do_not_repeat(self):
    self.state['check']['ok'] = False
    self.save()
    with patch.object(n, 'send_email', return_value='id') as send:
      self.assertEqual(n.notify(self.config)['event_count'], 1)
      self.assertEqual(n.notify(self.config)['event_count'], 0)
      self.state['check']['ok'] = True
      self.save()
      self.assertEqual(n.notify(self.config)['event_count'], 1)
      self.assertIn('recovered', send.call_args.args[1][0]['title'])
      self.state['check']['ok'] = False
      self.save()
      self.assertEqual(n.notify(self.config)['event_count'], 1)

  def test_failed_mail_remains_pending_for_retry(self):
    self.state['build']['ok'] = False
    self.save()
    with patch.object(n, 'send_email', side_effect=OSError('offline')):
      with self.assertRaises(OSError):
        n.notify(self.config)
    with patch.object(n, 'send_email', return_value='id'):
      self.assertEqual(n.notify(self.config)['event_count'], 1)
      self.assertEqual(n.notify(self.config)['event_count'], 0)

  def test_reviews_are_not_repeated_after_a_failed_check(self):
    self.state['dependencies'] = {
      'idf': {
        'status': 'checked',
        'pinned': 'v1.0.0',
        'candidate': 'v1.0.1',
        'latest_stable': 'v1.0.1',
        'advisories': [{'id': 'GHSA-test'}],
      }
    }
    self.save()
    with patch.object(n, 'send_email', return_value='id'):
      self.assertEqual(n.notify(self.config)['event_count'], 1)
      self.assertEqual(n.notify(self.config)['event_count'], 0)
      self.state['check']['ok'] = False
      self.save()
      self.assertEqual(n.notify(self.config)['event_count'], 1)
      self.state['check']['ok'] = True
      self.save()
      self.assertEqual(n.notify(self.config)['event_count'], 1)
      self.state['dependencies']['idf']['candidate'] = 'v1.0.2'
      self.save()
      self.assertEqual(n.notify(self.config)['event_count'], 1)

  def candidate(self, fingerprint):
    run_id = '20261028T120000Z-aaaaaaaa'
    n.atomic_json(
      self.root / 'dfm-last.json',
      {
        'run_id': run_id,
        'fingerprint': fingerprint,
      },
    )
    path = self.root / 'runs' / run_id / 'dfm'
    path.mkdir(parents=True, exist_ok=True)
    n.atomic_json(
      path / 'candidate.json',
      {
        'state': 'HOST_TESTED_AWAITING_RELEASE_REVIEW',
        'deployable': False,
      },
    )

  def test_only_new_candidates_are_announced_after_initialization(self):
    self.candidate('historical')
    n.notify(self.config, initialize=True)
    with patch.object(n, 'send_email', return_value='id') as send:
      self.assertEqual(n.notify(self.config)['event_count'], 0)
      self.candidate('new')
      self.assertEqual(n.notify(self.config)['event_count'], 1)
      self.assertIn('not production-ready', send.call_args.args[1][0]['detail'])
      self.assertEqual(n.notify(self.config)['event_count'], 0)

  def test_stale_and_stalled_jobs_are_reported(self):
    self.state['check'].update(
      running=True,
      attempt=time.time() - 1000,
      success=time.time() - 10 * 86400,
    )
    self.save()
    events = n.collect_events(self.config, time.time())
    self.assertIn('stale:check', events)
    self.assertIn('stalled:check', events)

  def test_wrapper_failures_persist_until_a_successful_wrapper_run(self):
    with patch.object(n, 'send_email', return_value='id'):
      self.assertEqual(
        n.notify(
          self.config,
          mode='build',
          exit_code=1,
        )['event_count'],
        1,
      )
      self.assertEqual(n.notify(self.config)['event_count'], 0)
      self.assertEqual(
        n.notify(
          self.config,
          mode='build',
          exit_code=0,
        )['event_count'],
        1,
      )

  def test_mail_uses_fixed_command_and_never_includes_raw_error(self):
    detail = n.failure_reason({'error': 'command failed password=private'})
    self.assertNotIn('private', detail)
    result = subprocess.CompletedProcess([], 0, b'', b'')
    with patch.object(n.subprocess, 'run', return_value=result) as run:
      n.send_email(self.config['notifications'], [n.event('test', detail, 1)])
    self.assertEqual(run.call_args.args[0], ['/usr/sbin/sendmail', '-i', '-t'])
    self.assertNotIn(b'password=', run.call_args.kwargs['input'])

  def test_reject_header_injection_and_disabled_notifications(self):
    with self.assertRaises(ValueError):
      n.send_email({'to': ['root\nBcc: attacker']}, [])
    self.assertEqual(n.notify({'state': '/unused'})['state'], 'DISABLED')

  def test_scheduled_wrapper_reports_abnormal_exit_without_hiding_it(self):
    source = Path(__file__).with_name('run-scheduled.sh')
    wrapper = self.root / 'run-scheduled.sh'
    shutil.copy2(source, wrapper)
    fake = self.root / 'python3'
    trace = self.root / 'invocations'
    fake.write_text(
      '#!/bin/sh\n'
      'printf "%s\\n" "$*" >> "$TRACE"\n'
      'case "$*" in *orchestrate.py*) exit 7;; esac\n'
      'exit 0\n'
    )
    fake.chmod(0o755)
    result = subprocess.run(
      ['sh', str(wrapper), 'check'],
      capture_output=True,
      text=True,
      env={
        **os.environ,
        'PATH': str(self.root) + ':/usr/bin:/bin',
        'TRACE': str(trace),
      },
    )
    self.assertEqual(result.returncode, 7)
    self.assertIn('--mode check --exit-code 7', trace.read_text())

  def test_vendor_changes_are_not_repeated(self):
    report = {
      'vendor_index': {
        'sha256': 'first',
        'url': 'https://example.invalid/advisories',
        'bulletins': ['one'],
      }
    }
    n.atomic_json(self.root / 'latest-check.json', report)
    with patch.object(n, 'send_email', return_value='id'):
      self.assertEqual(n.notify(self.config)['event_count'], 1)
      self.assertEqual(n.notify(self.config)['event_count'], 0)
      report['vendor_index']['sha256'] = 'changed'
      n.atomic_json(self.root / 'latest-check.json', report)
      self.assertEqual(n.notify(self.config)['event_count'], 1)


if __name__ == '__main__':
  unittest.main()
