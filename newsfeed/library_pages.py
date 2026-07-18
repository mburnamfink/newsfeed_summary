"""HTML for the Library retrieval surface served from ``articles.db``.

These pages are rendered on demand by the Archive Server from live DB queries and
link through to the existing offline archives. They share the digest's serif look
and carry the same one-tap controls — feedback reactions, a ★ star toggle, and
tag chips — wired to the server's write endpoints.
"""
from jinja2 import Environment

from .library import Article

_env = Environment(autoescape=True)

_STYLE = """
  body { font-family: Georgia, serif; max-width: 860px; margin: 40px auto;
         padding: 0 24px; color: #222; line-height: 1.6; background: #fafafa; }
  a { color: #2a6; text-decoration: none; }
  a:hover { text-decoration: underline; }
  h1 { font-size: 1.6rem; border-bottom: 2px solid #333; padding-bottom: 10px; }
  .nav { font-size: 0.9rem; margin-bottom: 20px; }
  .nav a { margin-right: 14px; }
  .search { margin: 18px 0 28px; }
  .search input[type=text] { font-size: 1rem; padding: 8px 10px; width: 70%;
         border: 1px solid #ccc; border-radius: 5px; }
  .search button { font-size: 1rem; padding: 8px 14px; border: 1px solid #ccc;
         border-radius: 5px; background: #f0f0f0; cursor: pointer; }
  .facets { display: flex; gap: 40px; flex-wrap: wrap; }
  .facet { flex: 1; min-width: 240px; }
  .facet h2 { font-size: 1.05rem; }
  .facet ul { list-style: none; padding: 0; margin: 0; }
  .facet li { padding: 3px 0; font-size: 0.95rem; }
  .facet .n { color: #999; font-size: 0.85rem; }
  .card { background: white; border: 1px solid #ddd; border-radius: 8px;
          padding: 16px 20px; margin: 12px 0; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }
  .card h3 { margin: 0 0 5px; font-size: 1.03rem; }
  .card h3 a { color: #111; }
  .card-meta { font-size: 0.82rem; color: #777; margin-bottom: 8px; }
  .badge { display: inline-block; border-radius: 4px; padding: 1px 7px;
           font-size: 0.76rem; font-weight: bold; color: white; margin-left: 4px; }
  .badge-high { background: #2a9; } .badge-medium { background: #c90; }
  .badge-low { background: #999; } .badge-lock { background: #b4553a; }
  .card-summary { color: #333; font-size: 0.95rem; }
  .chips { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 5px; align-items: center; }
  .chip { font-size: 0.76rem; padding: 1px 8px; border: 1px solid #cbd; border-radius: 10px;
          background: #eef2ff; color: #335; cursor: pointer; }
  .chip:hover { background: #dde6ff; }
  .item-actions { margin-top: 8px; display: flex; align-items: center; gap: 6px; }
  .react, .star { padding: 3px 10px; font-size: 0.85rem; cursor: pointer;
          border: 1px solid #ccc; border-radius: 4px; background: #f5f5f5; color: #555; }
  .react:hover, .star:hover { background: #ececec; }
  .react.active { color: white; border-color: transparent; font-weight: bold; }
  .react-down.active { background: #c0563d; } .react-confirm.active { background: #3b7dd8; }
  .react-up.active { background: #2a9d5c; }
  .star.active { background: #f6b40a; border-color: transparent; color: #222; font-weight: bold; }
  .is-read { opacity: 0.45; }
  .empty { color: #aaa; font-style: italic; }
"""

# One tap per control; each POSTs to the server and reflects the new state. Shared
# by every Library page (and mirrors the digest's own script).
_SCRIPT = """
async function post(url, body) {
  try {
    const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)});
    return r.ok;
  } catch (_) { return false; }
}
async function rate(btn) {
  const item = btn.closest('[data-msgid]'); if (!item) return;
  const wasActive = btn.classList.contains('active');
  const sentiment = wasActive ? null : btn.dataset.sentiment;
  if (!await post('/api/rate', {message_id:item.dataset.msgid, subject:item.dataset.subject,
      sender:item.dataset.sender, sentiment})) return;
  item.querySelectorAll('.react').forEach(b => b.classList.remove('active'));
  if (sentiment !== null) btn.classList.add('active');
  item.classList.toggle('is-read', sentiment !== null);
}
async function toggleStar(btn) {
  const item = btn.closest('[data-msgid]'); if (!item) return;
  const starred = !btn.classList.contains('active');
  if (!await post('/api/star', {message_id:item.dataset.msgid, starred})) return;
  btn.classList.toggle('active', starred);
}
async function removeTag(chip) {
  const item = chip.closest('[data-msgid]'); if (!item) return;
  if (!await post('/api/tag', {message_id:item.dataset.msgid, tag:chip.dataset.tag, op:'remove'})) return;
  chip.remove();
}
async function addTag(btn) {
  const item = btn.closest('[data-msgid]'); if (!item) return;
  const tag = (prompt('Add tag:') || '').trim(); if (!tag) return;
  if (!await post('/api/tag', {message_id:item.dataset.msgid, tag, op:'add'})) return;
  location.reload();
}
"""

_NAV = '<div class="nav"><a href="/">🏠 Home</a><a href="/library">📚 Library</a>' \
       '<a href="/library?starred=1">★ Starred</a></div>'

_ITEM = """
    {%- macro tier_badge(a) -%}
      {%- if a.tier == 'high' %}badge-high{% elif a.tier == 'medium' %}badge-medium{% else %}badge-low{% endif -%}
    {%- endmacro -%}
    <div class="card {{ 'is-read' if a.read }}" data-msgid="{{ a.message_id }}"
         data-subject="{{ a.subject }}" data-sender="{{ a.sender_name }}">
      <h3><a href="{{ a.archive_path or ('https://mail.google.com/mail/u/0/#all/' ~ a.message_id) }}"
             target="_blank" rel="noopener">{{ a.subject }}</a></h3>
      <div class="card-meta">
        <a href="/library/author/{{ a.sender_name|urlencode }}">{{ a.sender_name }}</a>
        <span class="badge {{ tier_badge(a) }}">{{ "%.1f"|format(a.score) }}</span>
        {%- if a.paywalled %}<span class="badge badge-lock">🔒</span>{% endif %}
        {%- if a.date %} &nbsp;·&nbsp; {{ a.date }}{% endif %}
      </div>
      <div class="card-summary">{{ a.display_summary }}</div>
      <div class="chips">
        {%- for tag in a.tags %}
        <span class="chip" data-tag="{{ tag }}" onclick="removeTag(this)" title="Tap to remove"><a href="/library/tag/{{ tag|urlencode }}" onclick="event.stopPropagation()">{{ tag }}</a> ✕</span>
        {%- endfor %}
        <span class="chip" onclick="addTag(this)" title="Add a tag">+</span>
      </div>
      <div class="item-actions">
        <button class="star {{ 'active' if a.starred }}" onclick="toggleStar(this)" title="Star to follow up">★</button>
        <button class="react react-down {{ 'active' if a.feedback == 'down' }}" data-sentiment="down" onclick="rate(this)" title="Rank lower">👎</button>
        <button class="react react-confirm {{ 'active' if a.feedback == 'confirmed' }}" data-sentiment="confirmed" onclick="rate(this)" title="Read &amp; right">✓</button>
        <button class="react react-up {{ 'active' if a.feedback == 'up' }}" data-sentiment="up" onclick="rate(this)" title="Rank higher">👍</button>
      </div>
    </div>
"""

_PAGE = (
    "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\">"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">"
    "<title>{{ title }} — Library</title><style>" + _STYLE + "</style></head><body>"
    + _NAV +
    "<h1>{{ title }}</h1>{% if subtitle %}<p class=\"card-meta\">{{ subtitle }}</p>{% endif %}"
    "{{ body }}"
    "<script>" + _SCRIPT + "</script></body></html>"
)

_HOME_BODY = (
    '<form class="search" action="/library/search" method="get">'
    '<input type="text" name="q" placeholder="Search article bodies…" autofocus>'
    '<button type="submit">Search</button></form>'
    '<div class="facets">'
    '<div class="facet"><h2>Tags</h2><ul>'
    '{% for tag, n in tags %}<li><a href="/library/tag/{{ tag|urlencode }}">{{ tag }}</a> '
    '<span class="n">{{ n }}</span></li>{% endfor %}</ul></div>'
    '<div class="facet"><h2>Authors</h2><ul>'
    '{% for name, n in authors %}<li><a href="/library/author/{{ name|urlencode }}">{{ name }}</a> '
    '<span class="n">{{ n }}</span></li>{% endfor %}</ul></div>'
    '</div>'
)

_LIST_BODY = (
    "{% if articles %}{% for a in articles %}" + _ITEM + "{% endfor %}"
    '{% else %}<p class="empty">Nothing here.</p>{% endif %}'
)


def _render_page(title: str, body_html: str, subtitle: str = "") -> str:
    return _env.from_string(_PAGE).render(title=title, subtitle=subtitle, body=body_html)


def render_home(
    authors: list[tuple[str, int]], tags: list[tuple[str, int]]
) -> str:
    body = _env.from_string(_HOME_BODY).render(authors=authors, tags=tags)
    return _render_page("Library", body)


def render_list(title: str, articles: list[Article], subtitle: str = "") -> str:
    body = _env.from_string(_LIST_BODY).render(articles=articles)
    return _render_page(title, body, subtitle)
