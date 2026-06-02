from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment

from .models import ScoredEmail

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Newsletter Digest — {{ date }}</title>
  <style>
    body {
      font-family: Georgia, serif;
      max-width: 820px;
      margin: 40px auto;
      padding: 0 24px;
      color: #222;
      line-height: 1.65;
      background: #fafafa;
    }
    h1 { font-size: 1.75rem; border-bottom: 2px solid #333; padding-bottom: 10px; margin-bottom: 4px; }
    .meta-line { color: #777; font-size: 0.9rem; margin-bottom: 36px; }
    h2 { font-size: 1.2rem; color: #333; margin-top: 40px; margin-bottom: 12px; }
    .count { color: #999; font-weight: normal; font-size: 0.95rem; }
    .empty { color: #aaa; font-style: italic; font-size: 0.9rem; }

    /* High interest cards */
    .card {
      background: white;
      border: 1px solid #ddd;
      border-radius: 8px;
      padding: 18px 22px;
      margin: 14px 0;
      box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }
    .card h3 { margin: 0 0 6px; font-size: 1.05rem; color: #111; }
    .card h3 a { color: inherit; text-decoration: none; }
    .card h3 a:hover { text-decoration: underline; }
    .card-meta { font-size: 0.82rem; color: #777; margin-bottom: 10px; }
    .badge {
      display: inline-block;
      border-radius: 4px;
      padding: 1px 7px;
      font-size: 0.78rem;
      font-weight: bold;
      color: white;
      margin-left: 6px;
      vertical-align: middle;
    }
    .badge-high   { background: #2a9; }
    .badge-medium { background: #c90; }
    .badge-low    { background: #999; }
    .card-summary { color: #333; }

    /* Medium interest list */
    .medium-list { list-style: none; padding: 0; margin: 0; }
    .medium-list li {
      padding: 11px 0;
      border-bottom: 1px solid #eee;
      font-size: 0.95rem;
    }
    .medium-list li:last-child { border-bottom: none; }
    .medium-sender { font-weight: bold; color: #444; }
    .medium-subject { color: #222; }
    .medium-subject a { color: inherit; text-decoration: none; }
    .medium-subject a:hover { text-decoration: underline; }
    .medium-summary { color: #555; display: block; margin-top: 2px; }

    /* Low interest list */
    .low-list { list-style: none; padding: 0; margin: 0; }
    .low-list li { padding: 5px 0; font-size: 0.88rem; color: #666; }
    .low-sender { font-weight: bold; color: #444; }

    /* Read tracking and per-item actions */
    .is-read { opacity: 0.35; transition: opacity 0.3s; }
    .item-actions {
      margin-top: 8px;
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 0.78rem;
      color: #999;
    }
    .item-actions input[type=number] {
      width: 46px;
      padding: 2px 4px;
      font-size: 0.78rem;
      border: 1px solid #ccc;
      border-radius: 3px;
    }
    .item-actions button {
      padding: 2px 7px;
      font-size: 0.78rem;
      cursor: pointer;
      border: 1px solid #ccc;
      border-radius: 3px;
      background: #f5f5f5;
      color: #555;
    }
    .item-actions button:disabled { opacity: 0.5; cursor: default; }
  </style>
</head>
<body>
  <h1>Newsletter Digest</h1>
  <div class="meta-line">{{ date }} &nbsp;·&nbsp; {{ total }} articles &nbsp;·&nbsp; generated {{ generated_at }}</div>

  <h2>Must Read <span class="count">({{ high|length }})</span></h2>
  {% if high %}
    {% for item in high %}
    <div class="card" data-msgid="{{ item.email.message_id }}" data-subject="{{ item.email.subject }}" data-sender="{{ item.email.sender_name }}">
      <h3><a href="{{ item.email.archive_path or item.email.url or 'https://mail.google.com/mail/u/0/#all/' ~ item.email.message_id }}" target="_blank" rel="noopener">{{ item.email.subject }}</a></h3>
      <div class="card-meta">
        {{ item.email.sender_name }}
        <span class="badge badge-high">{{ "%.1f"|format(item.interest_score) }}</span>
        &nbsp;·&nbsp; {{ item.topic }}
      </div>
      <div class="card-summary">{{ item.summary or item.one_line }}</div>
      <div class="item-actions">
        <span>Rate:</span>
        <input type="number" class="rate-input" min="0" max="10" step="0.5" placeholder="0–10">
        <button onclick="submitFeedback(this)">→</button>
        <button onclick="markRead(this)">Read ✓</button>
      </div>
    </div>
    {% endfor %}
  {% else %}
    <p class="empty">No must-read articles today.</p>
  {% endif %}

  <h2>Worth a Look <span class="count">({{ medium|length }})</span></h2>
  {% if medium %}
  <ul class="medium-list">
    {% for item in medium %}
    <li data-msgid="{{ item.email.message_id }}" data-subject="{{ item.email.subject }}" data-sender="{{ item.email.sender_name }}">
      <span class="medium-sender">{{ item.email.sender_name }}</span>
      <span class="badge badge-medium">{{ "%.1f"|format(item.interest_score) }}</span>
      &nbsp; <span class="medium-subject"><a href="{{ item.email.archive_path or item.email.url or 'https://mail.google.com/mail/u/0/#all/' ~ item.email.message_id }}" target="_blank" rel="noopener">{{ item.email.subject }}</a></span>
      <span class="medium-summary">{{ item.summary or item.one_line }}</span>
      <div class="item-actions">
        <input type="number" class="rate-input" min="0" max="10" step="0.5" placeholder="0–10">
        <button onclick="submitFeedback(this)">→</button>
        <button onclick="markRead(this)">✓</button>
      </div>
    </li>
    {% endfor %}
  </ul>
  {% else %}
    <p class="empty">No medium-interest articles today.</p>
  {% endif %}

  <h2>Skimmed <span class="count">({{ low|length }})</span></h2>
  {% if low %}
  <ul class="low-list">
    {% for item in low %}
    <li data-msgid="{{ item.email.message_id }}" data-subject="{{ item.email.subject }}" data-sender="{{ item.email.sender_name }}"><span class="low-sender">{{ item.email.sender_name }}:</span> <a href="{{ item.email.archive_path or item.email.url or 'https://mail.google.com/mail/u/0/#all/' ~ item.email.message_id }}" target="_blank" rel="noopener" style="color:#666;text-decoration:none;">{{ item.one_line }}</a>
      <div class="item-actions">
        <input type="number" class="rate-input" min="0" max="10" step="0.5" placeholder="0–10">
        <button onclick="submitFeedback(this)">→</button>
        <button onclick="markRead(this)">✓</button>
      </div>
    </li>
    {% endfor %}
  </ul>
  {% else %}
    <p class="empty">Nothing in the low-interest pile.</p>
  {% endif %}

<script>
async function loadReadState() {
  try {
    const r = await fetch('/read_state.json');
    if (!r.ok) return;
    const ids = await r.json();
    ids.forEach(id => {
      document.querySelectorAll('[data-msgid="' + id.replace(/"/g, '\\"') + '"]')
        .forEach(el => el.classList.add('is-read'));
    });
  } catch (_) {}
}

async function markRead(btn) {
  const item = btn.closest('[data-msgid]');
  if (!item) return;
  try {
    await fetch('/api/mark-read', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message_id: item.dataset.msgid})
    });
    item.classList.add('is-read');
  } catch (_) {}
}

async function submitFeedback(btn) {
  const item = btn.closest('[data-msgid]');
  if (!item) return;
  const input = item.querySelector('.rate-input');
  const score = parseFloat(input.value);
  if (isNaN(score) || score < 0 || score > 10) return;
  try {
    const r = await fetch('/api/feedback', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({subject: item.dataset.subject, sender: item.dataset.sender, score})
    });
    if (r.ok) { btn.textContent = '✓'; btn.disabled = true; input.disabled = true; }
  } catch (_) {}
}

loadReadState();
</script>
</body>
</html>"""


def render_digest(
    scored: list[ScoredEmail],
    target_date: date,
    output_dir: Path,
) -> Path:
    env = Environment(autoescape=True)
    template = env.from_string(_TEMPLATE)

    high = sorted([s for s in scored if s.tier == "high"], key=lambda x: -x.interest_score)
    medium = sorted([s for s in scored if s.tier == "medium"], key=lambda x: -x.interest_score)
    low = sorted([s for s in scored if s.tier == "low"], key=lambda x: -x.interest_score)

    html = template.render(
        date=target_date.strftime("%B %d, %Y"),
        total=len(scored),
        generated_at=datetime.now().strftime("%I:%M %p"),
        high=high,
        medium=medium,
        low=low,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{target_date.isoformat()}.html"
    output_path.write_text(html, encoding="utf-8")

    return output_path


_INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Newsletter Digests</title>
  <style>
    body { font-family: Georgia, serif; max-width: 820px; margin: 40px auto;
           padding: 0 24px; color: #222; line-height: 1.65; background: #fafafa; }
    h1 { font-size: 1.75rem; border-bottom: 2px solid #333; padding-bottom: 10px; }
    ul { list-style: none; padding: 0; }
    li { padding: 10px 0; border-bottom: 1px solid #eee; font-size: 1.1rem; }
    a { color: #2a6; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .empty { color: #aaa; font-style: italic; }
  </style>
</head>
<body>
  <h1>Newsletter Digests</h1>
  {% if dates %}
  <ul>
    {% for d in dates %}
    <li><a href="/digests/{{ d }}.html">{{ d }}</a></li>
    {% endfor %}
  </ul>
  {% else %}
  <p class="empty">No digests yet.</p>
  {% endif %}
</body>
</html>"""


def render_index(digests_dir: Path, serve_root: Path) -> Path:
    """(Re)generate the landing page listing every digest, newest first.

    This is the single page the tablet bookmarks at the server root.
    """
    dates = sorted(
        (p.stem for p in digests_dir.glob("*.html")),
        reverse=True,
    )
    env = Environment(autoescape=True)
    html = env.from_string(_INDEX_TEMPLATE).render(dates=dates)

    serve_root.mkdir(parents=True, exist_ok=True)
    index_path = serve_root / "index.html"
    index_path.write_text(html, encoding="utf-8")
    return index_path
