from email import policy
from email.parser import BytesParser
import copy
import subprocess
import unittest
from unittest.mock import patch

from email_formatting import render_email
from email_previews import preview_cases, send_previews
import notify as n


class HtmlEmailTests(unittest.TestCase):
  def test_untrusted_text_is_escaped_and_preview_is_in_both_versions(self):
    events = [n.event('<script>alert(1)</script>', '<img src=x> & details', 1)]
    subject, plain, html = render_email(events, 'PREVIEW 01/14')
    self.assertTrue(subject.startswith('[PREVIEW 01/14]'))
    self.assertIn('PREVIEW ONLY', plain)
    self.assertIn('PREVIEW ONLY', html)
    self.assertNotIn('<script>', html)
    self.assertNotIn('<img src=x>', html)
    self.assertIn('&lt;img src=x&gt;', html)
    self.assertIn('Your next step', html)

  def test_actual_submission_has_html_plain_fallback_and_no_attachment(self):
    settings = {'to': ['operator@example.invalid']}
    result = subprocess.CompletedProcess([], 0, b'', b'')
    with patch.object(n.subprocess, 'run', return_value=result) as run:
      n.send_email(settings, [n.event('Firmware check failed', 'Sample.', 1)])
    message = BytesParser(policy=policy.default).parsebytes(
      run.call_args.kwargs['input']
    )
    self.assertEqual(message.get_content_type(), 'multipart/alternative')
    self.assertIn('Sample.', message.get_body(('plain',)).get_content())
    self.assertIn('Sample.', message.get_body(('html',)).get_content())
    self.assertFalse(list(message.iter_attachments()))
    self.assertNotIn('PREVIEW', message['Subject'])

  def test_catalogue_covers_each_event_and_sending_never_changes_ledger(self):
    cases = preview_cases()
    self.assertEqual(len(cases), 14)
    names = [name for name, _ in cases]
    for mode in ['check', 'build']:
      for status in ['failed', 'overdue', 'stalled', 'recovered']:
        self.assertIn(f'{mode} {status}', names)
    self.assertIn('idf review', names)
    self.assertIn('esphome review', names)
    self.assertIn('vendor advisory review', names)
    self.assertIn('dfm candidate', names)
    self.assertIn('climate-sensor candidate', names)
    self.assertIn('combined summary', names)
    settings = {'to': ['operator@example.invalid']}
    original = copy.deepcopy(settings)
    with (
      patch.object(n, 'send_email', return_value='id') as send,
      patch('email_previews.time.sleep'),
      patch('email_previews.print'),
      patch.object(n, 'notify', side_effect=AssertionError('live state used')),
    ):
      self.assertEqual(send_previews(settings)['count'], 14)
    self.assertEqual(send.call_count, 14)
    self.assertEqual(settings, original)
    for index, call in enumerate(send.call_args_list, 1):
      self.assertEqual(call.kwargs['preview_label'], f'PREVIEW {index:02d}/14')


if __name__ == '__main__':
  unittest.main()
