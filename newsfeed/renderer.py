from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment

from .models import ScoredEmail

_TEMPLATE = """\
{%- macro paywall(email) -%}
{%- if email.paywalled %}<span class="badge badge-lock" title="The email is only a paid-subscriber teaser">🔒 Paywalled</span>{% endif -%}
{%- endmacro -%}
{%- macro reactions() -%}
      <div class="item-actions">
        <button class="star" onclick="toggleStar(this)" title="Star to follow up later">★</button>
        <button class="react react-down" data-sentiment="down" onclick="rate(this)" title="Not interested — rank lower">👎</button>
        <button class="react react-confirm" data-sentiment="confirmed" onclick="rate(this)" title="Read it — score was right">✓ Read &amp; right</button>
        <button class="react react-up" data-sentiment="up" onclick="rate(this)" title="Loved it — rank higher">👍</button>
      </div>
{%- endmacro -%}
{%- macro chips(item) -%}
{%- if item.tags %}
      <div class="chips">
        {%- for tag in item.tags %}
        <span class="chip" data-tag="{{ tag }}" onclick="removeTag(this)" title="Tap to remove"><a href="/library/tag/{{ tag|urlencode }}" onclick="event.stopPropagation()">{{ tag }}</a> ✕</span>
        {%- endfor %}
        <span class="chip chip-add" onclick="addTag(this)" title="Add a tag">+</span>
      </div>
{%- endif -%}
{%- endmacro -%}
<!DOCTYPE html>
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
    .badge-lock   { background: #b4553a; }
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
    .is-read { opacity: 0.4; transition: opacity 0.3s; }
    .item-actions {
      margin-top: 8px;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .react {
      padding: 3px 10px;
      font-size: 0.85rem;
      line-height: 1.3;
      cursor: pointer;
      border: 1px solid #ccc;
      border-radius: 4px;
      background: #f5f5f5;
      color: #555;
    }
    .react:hover { background: #ececec; }
    .react.active { color: white; border-color: transparent; font-weight: bold; }
    .react-down.active    { background: #c0563d; }
    .react-confirm.active { background: #3b7dd8; }
    .react-up.active      { background: #2a9d5c; }
    .star {
      padding: 3px 10px; font-size: 0.85rem; cursor: pointer;
      border: 1px solid #ccc; border-radius: 4px; background: #f5f5f5; color: #999;
    }
    .star:hover { background: #ececec; }
    .star.active { background: #f6b40a; border-color: transparent; color: #222; font-weight: bold; }

    /* Tag chips */
    .chips { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 5px; align-items: center; }
    .chip { font-size: 0.76rem; padding: 1px 8px; border: 1px solid #cbd; border-radius: 10px;
            background: #eef2ff; color: #335; cursor: pointer; }
    .chip a { color: inherit; text-decoration: none; }
    .chip:hover { background: #dde6ff; }

    /* Library nav */
    .nav { font-size: 0.9rem; margin-bottom: 24px; }
    .nav a { margin-right: 14px; color: #2a6; text-decoration: none; }
    .nav a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <div class="nav"><a href="/">🏠 Home</a><a href="/library">📚 Library</a><a href="/library?starred=1">★ Starred</a></div>
  <h1>Newsletter Digest</h1>
  <div class="meta-line">{{ date }} &nbsp;·&nbsp; {{ total }} articles{% if paywalled %} &nbsp;·&nbsp; 🔒 {{ paywalled }} paywalled{% endif %} &nbsp;·&nbsp; generated {{ generated_at }}</div>

  <h2>Must Read <span class="count">({{ high|length }})</span></h2>
  {% if high %}
    {% for item in high %}
    <div class="card" data-msgid="{{ item.email.message_id }}" data-subject="{{ item.email.subject }}" data-sender="{{ item.email.sender_name }}">
      <h3><a href="{{ item.email.archive_path or item.email.url or 'https://mail.google.com/mail/u/0/#all/' ~ item.email.message_id }}" target="_blank" rel="noopener">{{ item.email.subject }}</a></h3>
      <div class="card-meta">
        {{ item.email.sender_name }}
        <span class="badge badge-high">{{ "%.1f"|format(item.interest_score) }}</span>
        {{ paywall(item.email) }}
        &nbsp;·&nbsp; {{ item.topic }}
      </div>
      <div class="card-summary">{{ item.summary or item.one_line }}</div>
      {{ chips(item) }}
      {{ reactions() }}
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
      {{ paywall(item.email) }}
      &nbsp; <span class="medium-subject"><a href="{{ item.email.archive_path or item.email.url or 'https://mail.google.com/mail/u/0/#all/' ~ item.email.message_id }}" target="_blank" rel="noopener">{{ item.email.subject }}</a></span>
      <span class="medium-summary">{{ item.summary or item.one_line }}</span>
      {{ chips(item) }}
      {{ reactions() }}
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
    <li data-msgid="{{ item.email.message_id }}" data-subject="{{ item.email.subject }}" data-sender="{{ item.email.sender_name }}"><span class="low-sender">{{ item.email.sender_name }}:</span> <a href="{{ item.email.archive_path or item.email.url or 'https://mail.google.com/mail/u/0/#all/' ~ item.email.message_id }}" target="_blank" rel="noopener" style="color:#666;text-decoration:none;">{{ item.one_line }}</a> {{ paywall(item.email) }}
      {{ chips(item) }}
      {{ reactions() }}
    </li>
    {% endfor %}
  </ul>
  {% else %}
    <p class="empty">Nothing in the low-interest pile.</p>
  {% endif %}

<script>
// Reflect stored reader state (read / starred / prior reaction) on the freshly
// rendered digest, which is built from new scores and knows none of it.
async function loadState() {
  try {
    const r = await fetch('/api/state');
    if (!r.ok) return;
    const state = await r.json();
    Object.entries(state).forEach(([id, s]) => {
      document.querySelectorAll('[data-msgid="' + id.replace(/"/g, '\\"') + '"]').forEach(el => {
        if (s.read) el.classList.add('is-read');
        if (s.starred) el.querySelectorAll('.star').forEach(b => b.classList.add('active'));
        if (s.feedback) {
          el.querySelectorAll('.react').forEach(b =>
            b.classList.toggle('active', b.dataset.sentiment === s.feedback));
        }
      });
    });
  } catch (_) {}
}

// One tap records a coarse reaction and marks the item read. Tapping the same
// button again clears it (un-reads). The server persists both in one request.
async function rate(btn) {
  const item = btn.closest('[data-msgid]');
  if (!item) return;
  const wasActive = btn.classList.contains('active');
  const sentiment = wasActive ? null : btn.dataset.sentiment;
  try {
    const r = await fetch('/api/rate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        message_id: item.dataset.msgid,
        subject: item.dataset.subject,
        sender: item.dataset.sender,
        sentiment
      })
    });
    if (!r.ok) return;
    item.querySelectorAll('.react').forEach(b => b.classList.remove('active'));
    if (sentiment !== null) btn.classList.add('active');
    item.classList.toggle('is-read', sentiment !== null);
  } catch (_) {}
}

async function post(url, body) {
  try {
    const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)});
    return r.ok;
  } catch (_) { return false; }
}

async function toggleStar(btn) {
  const item = btn.closest('[data-msgid]');
  if (!item) return;
  const starred = !btn.classList.contains('active');
  if (await post('/api/star', {message_id: item.dataset.msgid, starred})) {
    btn.classList.toggle('active', starred);
  }
}

async function removeTag(chip) {
  const item = chip.closest('[data-msgid]');
  if (!item) return;
  if (await post('/api/tag', {message_id: item.dataset.msgid, tag: chip.dataset.tag, op: 'remove'})) {
    chip.remove();
  }
}

async function addTag(btn) {
  const item = btn.closest('[data-msgid]');
  if (!item) return;
  const tag = (prompt('Add tag:') || '').trim();
  if (!tag) return;
  if (await post('/api/tag', {message_id: item.dataset.msgid, tag, op: 'add'})) {
    location.reload();
  }
}

loadState();
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
        paywalled=sum(1 for s in scored if s.email.paywalled),
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
    .nav { font-size: 1rem; margin-bottom: 24px; }
    .nav a { margin-right: 16px; }
  </style>
</head>
<body>
  <h1>Newsletter Digests</h1>
  <div class="nav"><a href="/library">📚 Browse the Library</a><a href="/library?starred=1">★ Starred</a></div>
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
