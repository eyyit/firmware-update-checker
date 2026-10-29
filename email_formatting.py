"""Accessible HTML and plain-text firmware notification templates."""

from html import escape


def presentation(title: str) -> tuple[str, str, str]:
  lower = title.lower()
  if 'recovered' in lower:
    return 'Recovered', '#18704a', 'No action needed unless the issue returns.'
  if 'candidate' in lower:
    return (
      'Test candidate',
      '#215ab5',
      'Reply with this email and ask for a candidate review. '
      'Do not flash the test binaries; a production build and a '
      'one-device pilot still need approval.',
    )
  if 'failed' in lower:
    return (
      'Action needed',
      '#a42b32',
      'Reply with this email and ask me to investigate the failed job.',
    )
  if 'overdue' in lower or 'stalled' in lower:
    return (
      'Check the job',
      '#936010',
      'Reply with this email and ask me to check the job and its schedule. '
      'A slow or missing run does not mean the firmware itself failed.',
    )
  return (
    'Review needed',
    '#936010',
    'Reply with this email and ask for an update review. We will check '
    'applicability and test results before planning any device update.',
  )


def render_email(
  events: list[dict],
  preview_label: str | None = None,
) -> tuple[str, str, str]:
  if not events:
    raise ValueError('An email needs at least one event')
  subject = '[Firmware] ' + (
    events[0]['title']
    if len(events) == 1
    else f'{len(events)} firmware updates and job notices'
  )
  heading = events[0]['title'] if len(events) == 1 else 'Your firmware summary'
  preview = ''
  plain_preview = ''
  if preview_label:
    subject = f'[{preview_label}] ' + subject
    plain_preview = (
      'PREVIEW ONLY: sample data. This does not report a real incident '
      'or a newly available firmware build.\n\n'
    )
    preview = (
      '<div style="background:#fff2cb;color:#654500;padding:16px 24px;'
      'font-size:14px;line-height:1.5;border-bottom:1px solid #e7d49c">'
      '<strong>PREVIEW ONLY</strong><br>Sample data. No real incident or '
      'new firmware build is being reported.</div>'
    )
  cards = []
  plain = []
  for item in events:
    label, color, action = presentation(item['title'])
    detail = ''.join(
      '<p style="margin:12px 0 0;font-size:15px;line-height:1.6;'
      'color:#3c4657;overflow-wrap:anywhere">'
      + escape(paragraph).replace('\n', '<br>')
      + '</p>'
      for paragraph in item['detail'].split('\n\n')
    )
    cards.append(
      '<tr><td style="padding:0 24px 24px">'
      '<table role="presentation" width="100%" cellpadding="0" '
      'cellspacing="0" style="border:1px solid #dce2e9;'
      f'border-top:4px solid {color};border-radius:6px">'
      '<tr><td style="padding:20px">'
      f'<p style="margin:0 0 10px;color:{color};font-size:12px;'
      f'font-weight:700;text-transform:uppercase">{escape(label)}</p>'
      '<h2 style="margin:0;font-size:20px;line-height:1.3;'
      f'color:#172333">{escape(item["title"])}</h2>{detail}'
      '<div style="margin-top:18px;padding:14px;background:#f2f5f8;'
      'border-radius:4px;font-size:14px;line-height:1.5;color:#172333">'
      '<strong>Your next step</strong><br>'
      + escape(action)
      + '</div></td></tr></table></td></tr>'
    )
    plain.append(
      item['title'] + '\n' + item['detail'] + '\n\nYOUR NEXT STEP\n' + action
    )
  footer = (
    'No automatic deployment. Test candidates still need release and '
    'hardware review. Security advisory coverage is partial.'
  )
  html = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    f'<title>{escape(heading)}</title></head>'
    '<body style="margin:0;padding:24px 8px;background:#edf1f5;'
    'font-family:Arial,Helvetica,sans-serif">'
    '<table role="presentation" width="100%" cellpadding="0" '
    'cellspacing="0"><tr><td align="center">'
    '<table role="presentation" width="100%" cellpadding="0" '
    'cellspacing="0" style="max-width:660px;background:#ffffff;'
    'border:1px solid #dce2e9;border-radius:8px">'
    f'<tr><td>{preview}</td></tr>'
    '<tr><td style="padding:28px 24px 24px">'
    '<p style="margin:0 0 10px;font-size:12px;letter-spacing:1px;'
    'font-weight:700;color:#526279">FIRMWARE UPDATE CHECKER</p>'
    '<h1 style="margin:0;font-size:26px;line-height:1.25;'
    f'color:#172333">{escape(heading)}</h1>'
    '<p style="margin:12px 0 0;font-size:14px;line-height:1.5;'
    'color:#526279">A clear status, the reason, and what to do next.</p>'
    '</td></tr>'
    + ''.join(cards)
    + '<tr><td style="padding:0 24px 24px;font-size:12px;'
    f'line-height:1.6;color:#617084">{escape(footer)}</td></tr>'
    '</table></td></tr></table></body></html>'
  )
  return (
    subject,
    plain_preview + '\n\n---\n\n'.join(plain) + '\n\n' + footer,
    html,
  )
