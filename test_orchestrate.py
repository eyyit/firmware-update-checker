import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import orchestrate as m


class PolicyTests(unittest.TestCase):
  def setUp(self):
    self.vendor = patch.object(m, 'vendor_index', return_value={})
    self.vendor.start()
    self.addCleanup(self.vendor.stop)

  def test_git_reads_do_not_rewrite_owner_checkout_index(self):
    with patch.object(m, 'run', return_value='clean') as run:
      self.assertEqual(m.git('/source', 'status', '--porcelain'), 'clean')
    self.assertIn('--no-optional-locks', run.call_args.args[0])

  def test_patch_selection_not_latest_minor_or_prerelease(self):
    releases = [
      {'tag_name': v}
      for v in ['v5.5.4', 'v5.5.6', 'v5.6.0', 'v6.0.0', 'v5.5.7-rc1']
    ]
    self.assertEqual(m.select_patch('v5.5.5', releases), 'v5.5.6')
    releases[1]['draft'] = True
    self.assertEqual(m.select_patch('v5.5.5', releases), 'v5.5.5')

  def test_ranges_do_not_call_unknown_clean(self):
    self.assertTrue(m.affected('2026.10.0', '>=2026.9.0, <2026.10.1'))
    self.assertFalse(m.affected('2026.10.2', '>=2026.9.0, <2026.10.1'))
    self.assertIsNone(m.affected('5.5.5', 'all releases before the fix'))
    with self.assertRaises(ValueError):
      m.version('v5.5.6-rc1')

  def test_api_failure_stays_unknown(self):
    config = {
      'dependencies': {
        'idf': {
          'version': 'v5.5.5',
          'image': 'x@sha256:abc',
          'repository': 'espressif/esp-idf',
        }
      }
    }
    with patch.object(m, 'fetch', side_effect=OSError('offline')):
      self.assertEqual(m.discover(config)['idf']['status'], 'unknown')

  def test_docker_has_no_socket_or_deployment_access(self):
    with tempfile.TemporaryDirectory() as directory:
      with patch.object(m, 'run') as run, patch.object(m.subprocess, 'run'):
        m.docker(
          'sha256:test',
          Path(directory),
          ['python3', '-V'],
          Path(directory) / 'log',
        )
      args = run.call_args.args[0]
      self.assertNotIn('--privileged', args)
      self.assertNotIn('host', args)
      self.assertFalse(
        any('docker.sock' in a or 'signing.key' in a for a in args)
      )
      self.assertIn('none', args)

  def test_failed_build_preserves_last_success_and_no_candidate(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      config = {
        'state': str(root),
        'metrics': str(root / 'metrics.prom'),
        'minimum_free_bytes': 0,
        'dependencies': {},
        'profiles': [{'id': 'dfm', 'source': '/test', 'dependency': 'idf'}],
        'test_image': 'sha256:test',
      }
      m.atomic_json(
        root / 'status.json',
        {'build': {'attempt': 1, 'success': 123, 'ok': True}},
      )
      metadata = {
        'idf': {'status': 'checked', 'candidate': 'v5.5.5', 'pinned': 'v5.5.5'}
      }
      with (
        patch.object(m, 'discover', return_value=metadata),
        patch.object(m, 'git', return_value='a' * 40),
        patch.object(
          m, 'build_profile', side_effect=RuntimeError('test failed')
        ),
      ):
        result = m.execute(config, 'build')
      self.assertEqual(result['state'], 'FAILED')
      self.assertFalse(result['deployed'])
      self.assertEqual(
        json.loads((root / 'status.json').read_text())['build']['success'], 123
      )
      self.assertFalse((root / 'dfm-last.json').exists())

  def test_success_is_never_signed_deployed_or_production_ready(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      config = {
        'state': str(root),
        'metrics': str(root / 'metrics.prom'),
        'minimum_free_bytes': 0,
        'dependencies': {},
        'profiles': [{'id': 'dfm', 'source': '/test', 'dependency': 'idf'}],
        'test_image': 'sha256:test',
      }
      with (
        patch.object(
          m, 'discover', return_value={'idf': {'status': 'checked'}}
        ),
        patch.object(m, 'git', return_value='a' * 40),
        patch.object(
          m,
          'build_profile',
          return_value={
            'state': 'HOST_TESTED_AWAITING_RELEASE_REVIEW',
            'deployable': False,
          },
        ),
      ):
        first = m.execute(config, 'build')
        second = m.execute(config, 'build')
      self.assertEqual(first['state'], 'COMPLETE_BUILD_ONLY')
      self.assertFalse(first['signed'])
      self.assertEqual(second['profiles']['dfm']['state'], 'UNCHANGED')

  def test_stage_timeout_always_cleans_container(self):
    import subprocess

    with tempfile.TemporaryDirectory() as directory:
      with (
        patch.object(
          m, 'run', side_effect=subprocess.TimeoutExpired('docker', 1)
        ),
        patch.object(m.subprocess, 'run') as cleanup,
      ):
        with self.assertRaises(subprocess.TimeoutExpired):
          m.docker(
            'sha256:test', Path(directory), ['false'], Path(directory) / 'log'
          )
      self.assertEqual(cleanup.call_args.args[0][:3], ['docker', 'rm', '-f'])

  def test_pruning_keeps_last_candidate_and_ignores_foreign_paths(self):
    import os
    import time

    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      runs = root / 'runs'
      runs.mkdir()
      names = [
        '20000101T000000Z-aaaaaaaa',
        '20000102T000000Z-bbbbbbbb',
      ]
      for name in names:
        path = runs / name
        path.mkdir()
        m.atomic_json(
          path / 'report.json',
          {
            'signed': False,
            'deployed': False,
          },
        )
        old = time.time() - 100 * 86400
        os.utime(path, (old, old))
      m.atomic_json(root / 'dfm-last.json', {'run_id': names[0]})
      foreign = runs / 'not-a-run'
      foreign.mkdir()
      m.prune(root)
      self.assertTrue((runs / names[0]).exists())
      self.assertFalse((runs / names[1]).exists())
      self.assertTrue(foreign.exists())

  def test_snapshot_rejects_dirty_sources_before_copy(self):
    with tempfile.TemporaryDirectory() as directory:
      with patch.object(m, 'git', side_effect=['a' * 40, ' M source.c']):
        with self.assertRaises(ValueError):
          m.snapshot('/source', Path(directory) / 'snapshot')
      self.assertFalse((Path(directory) / 'snapshot').exists())

  def test_vendor_index_reports_review_links_not_security_verdict(self):
    import io

    self.vendor.stop()
    content = (
      b'<a href="https://example/security.pdf">ignore</a>'
      b'<a href="/advisory_downloads/Security-test.pdf">review</a>'
    )
    with patch.object(
      m.urllib.request, 'urlopen', return_value=io.BytesIO(content)
    ):
      result = m.vendor_index()
    self.assertEqual(len(result['bulletins']), 1)
    self.assertEqual(result['applicability'], 'requires_device_specific_review')

  def test_vendor_index_layout_change_cannot_report_clean(self):
    import io

    self.vendor.stop()
    with patch.object(
      m.urllib.request,
      'urlopen',
      return_value=io.BytesIO(b'<html>changed</html>'),
    ):
      with self.assertRaises(ValueError):
        m.vendor_index()

  def test_online_workers_use_only_restricted_network(self):
    with tempfile.TemporaryDirectory() as directory:
      with patch.object(m, 'run') as run, patch.object(m.subprocess, 'run'):
        m.docker(
          'sha256:test',
          Path(directory),
          ['python3', '-V'],
          Path(directory) / 'log',
          online=True,
        )
      args = run.call_args.args[0]
      self.assertEqual(args[args.index('--network') + 1], 'firmware-build')
      self.assertEqual(args[args.index('--dns') + 1], '1.1.1.1')

  def test_test_image_refresh_failure_sets_failed_job(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      config = {
        'state': str(root),
        'metrics': str(root / 'metrics.prom'),
        'minimum_free_bytes': 0,
        'dependencies': {},
        'profiles': [],
        'test_image': 'sha256:test',
        'test_image_tag': 'test:local',
        'refresh_test_image': True,
      }
      with patch.object(m, 'discover', return_value={}):
        with patch.object(m, 'git', return_value='a' * 40):
          with patch.object(
            m, 'run', side_effect=RuntimeError('refresh failed')
          ):
            result = m.execute(config, 'build')
      self.assertEqual(result['state'], 'FAILED')
      status = json.loads((root / 'status.json').read_text())
      self.assertFalse(status['build']['ok'])
      self.assertFalse(status['build']['running'])


class PublicSourceTests(unittest.TestCase):
  def test_vendor_403_retries_then_parses_real_index(self):
    import io
    import urllib.error

    refusal = urllib.error.HTTPError(
      'https://example.invalid', 403, 'Forbidden', {}, None
    )
    page = io.BytesIO(b'<a href="/advisory_downloads/test.pdf">test</a>')
    with patch.object(m.urllib.request, 'urlopen', side_effect=[refusal, page]):
      with patch.object(m.time, 'sleep') as sleep:
        result = m.vendor_index()
    self.assertEqual(len(result['bulletins']), 1)
    sleep.assert_called_once_with(1)

  def test_permanent_403_stays_failure_and_names_source(self):
    import urllib.error

    refusal = urllib.error.HTTPError(
      'https://example.invalid', 403, 'Forbidden', {}, None
    )
    with patch.object(m.urllib.request, 'urlopen', side_effect=refusal) as url:
      with patch.object(m.time, 'sleep'):
        with self.assertRaisesRegex(OSError, 'example.invalid.*HTTP 403'):
          m.public_source('https://example.invalid/feed', 100, 'text/html')
    self.assertEqual(url.call_count, 3)

  def test_nonretryable_errors_fail_immediately(self):
    import urllib.error

    error = urllib.error.HTTPError(
      'https://example.invalid', 404, 'Not found', {}, None
    )
    with patch.object(m.urllib.request, 'urlopen', side_effect=error) as url:
      with self.assertRaisesRegex(OSError, 'HTTP 404'):
        m.public_source('https://example.invalid/feed', 100, 'text/html')
    self.assertEqual(url.call_count, 1)

  def test_size_limit_and_explicit_request_headers(self):
    import io

    with patch.object(
      m.urllib.request, 'urlopen', return_value=io.BytesIO(b'12345')
    ) as url:
      with self.assertRaisesRegex(ValueError, 'size limit'):
        m.public_source('https://example.invalid/feed', 4, 'text/html')
    request = url.call_args.args[0]
    self.assertEqual(request.get_header('Accept'), 'text/html')
    self.assertIn('firmware-update-checker', request.get_header('User-agent'))


if __name__ == '__main__':
  unittest.main()
