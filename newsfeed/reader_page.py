"""The Reader: a mobile-first, installable reading app (ADR 0006).

The Archive Server serves three things from here:

* ``/reader``            — a single-page app (this module's ``READER_HTML``) that
  triages the unread queue one card at a time (ADR 0008): **ignore**, **later**,
  **read**, or rate/star straight from the summary; today's digest first, then the
  backlog. No build step, no framework — vanilla JS.
* ``/reader/article/<id>`` — one stored Archive re-served for the phone. The
  immutable archive (ADR 0001) is left on disk untouched; :func:`render_article`
  injects a ``<meta viewport>``, a ``<base>`` so the archive's relative images
  still resolve, and a responsive stylesheet that constrains width while keeping
  the newsletter's own formatting. ``mode="reader"`` re-extracts clean text with
  ``trafilatura`` as a fallback for layouts the CSS can't tame.
* ``/manifest.webmanifest`` + ``/sw.js`` — the PWA bits that let Chrome install the
  Reader to the home screen so it opens in its own window, not a browser tab.
"""
import html as _html
import posixpath

import markdown as _markdown
import trafilatura
from bs4 import BeautifulSoup

# Responsive reset injected into every archived article so a fixed-width desktop
# newsletter fits a phone. Best-effort by design (ADR 0006): it constrains widths
# and reflows rather than restyling, so the host's own colours/type survive; the
# `?mode=reader` fallback covers layouts this can't rescue.
_ARTICLE_CSS = """
  html { -webkit-text-size-adjust: 100%; }
  html, body { margin: 0 !important; padding: 0 !important;
    max-width: 100% !important; background: #fff !important; }
  body { padding: 16px !important; box-sizing: border-box;
    font-size: 18px; line-height: 1.6; color: #1a1a1a;
    overflow-wrap: anywhere; word-break: break-word; }
  /* Nothing may exceed the viewport — the reliable cure for horizontal scroll. */
  * { max-width: 100vw !important; }
  img, video, iframe, svg { max-width: 100% !important; height: auto !important; }
  table { max-width: 100% !important; width: auto !important; table-layout: auto !important; }
  td, th { max-width: 100vw !important; word-break: break-word; }
  pre, code { white-space: pre-wrap !important; word-break: break-word; }
  a { word-break: break-word; }
"""

# Clean document for the reader-mode fallback (trafilatura-extracted article).
_READER_CSS = """
  html { -webkit-text-size-adjust: 100%; }
  body { margin: 0; padding: 20px; max-width: 42rem; margin: 0 auto;
    font: 18px/1.65 Georgia, 'Times New Roman', serif; color: #1a1a1a; background: #fff; }
  img { max-width: 100%; height: auto; }
  a { color: #2a6; }
  pre, code { white-space: pre-wrap; word-break: break-word; }
"""

_VIEWPORT = '<meta name="viewport" content="width=device-width, initial-scale=1">'


def render_article(archive_html: str, base_href: str, *, reader_mode: bool = False) -> str:
    """Re-serve a stored Archive for the phone (ADR 0006).

    ``base_href`` is the archive's own directory (e.g. ``/archive/<date>/<id>/``);
    it becomes a ``<base>`` so the archive's relative ``images/…`` sources resolve
    even though this HTML is served from ``/reader/article/<id>``.
    """
    if reader_mode:
        extracted = trafilatura.extract(
            archive_html, output_format="html", include_images=True,
            include_links=True, favor_recall=True,
        ) or "<p>Could not extract a clean reading view.</p>"
        return (
            f'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">{_VIEWPORT}'
            f'<base href="{base_href}"><style>{_READER_CSS}</style></head>'
            f"<body>{extracted}</body></html>"
        )

    soup = BeautifulSoup(archive_html, "html.parser")

    head = soup.head
    if head is None:
        head = soup.new_tag("head")
        (soup.html or soup).insert(0, head)

    # <base> first so it governs every relative URL that follows.
    base_tag = soup.new_tag("base", href=base_href)
    head.insert(0, base_tag)

    if not soup.find("meta", attrs={"name": "viewport"}):
        head.append(BeautifulSoup(_VIEWPORT, "html.parser"))

    style = soup.new_tag("style")
    style.string = _ARTICLE_CSS
    head.append(style)  # last, so it wins the cascade over the newsletter's own CSS

    return str(soup)


def article_base_href(archive_path: str) -> str:
    """Directory of an ``archive_path`` (``/archive/<d>/<id>/index.html`` → ``…/<id>/``)."""
    return posixpath.dirname(archive_path) + "/"


# The long-form summary (ADR 0007) rendered from stored Markdown. Serif reading
# column matching the reader-mode article view, with room for the `##` sections
# the summary prompt produces.
_SUMMARY_CSS = """
  html { -webkit-text-size-adjust: 100%; }
  body { margin: 0; padding: 24px 20px max(24px, env(safe-area-inset-bottom));
    max-width: 42rem; margin: 0 auto;
    font: 18px/1.65 Georgia, 'Times New Roman', serif; color: #1a1a1a; background: #fff; }
  .doc-head { border-bottom: 1px solid #e4e4e4; padding-bottom: 14px; margin-bottom: 22px; }
  .doc-head .kicker { font: 600 .72rem/1.4 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    text-transform: uppercase; letter-spacing: .06em; color: #2a6b4f; }
  .doc-head h1 { font-size: 1.5rem; line-height: 1.25; margin: 6px 0 8px; }
  .doc-head .meta { font: .8rem/1.5 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    color: #6b6b6b; }
  h2 { font-size: 1.2rem; margin: 1.8em 0 .5em; }
  p { margin: 0 0 1em; }
  ul, ol { margin: 0 0 1em; padding-left: 1.4em; }
  li { margin: .3em 0; }
  blockquote { margin: 0 0 1em; padding: 2px 0 2px 14px; border-left: 3px solid #cbd8d0;
    color: #444; font-style: italic; }
  em { color: #444; }
  a { color: #2a6; }
  code { background: #f2f2f2; padding: 1px 4px; border-radius: 3px;
    font-size: .9em; word-break: break-word; }
"""


# The summary page is opened in a non-sandboxed browser tab from the Library and
# digest, and python-markdown passes raw HTML through verbatim — so a prompt
# injection in the source article could plant live markup. Render to an allowlist:
# unknown tags are unwrapped (text kept), and only safe link schemes survive.
_SUMMARY_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "blockquote", "strong", "em", "b", "i", "code", "pre",
    "a", "table", "thead", "tbody", "tr", "th", "td",
}
_SAFE_LINK_SCHEMES = ("http:", "https:", "mailto:")


def _sanitize_summary_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(True):
        if tag.name not in _SUMMARY_TAGS:
            tag.unwrap()  # keep the text, drop the element (script/style/img/…)
            continue
        if tag.name == "a":
            href = str(tag.get("href") or "").strip()
            if not href.lower().startswith(_SAFE_LINK_SCHEMES):
                del tag["href"]
            tag.attrs = {k: v for k, v in tag.attrs.items() if k == "href"}
        else:
            tag.attrs = {}  # strip on* handlers, style, everything
    return str(soup)


def render_summary_page(
    subject: str,
    summary_md: str,
    *,
    model: str = "",
    source: str = "",
    word_count: int = 0,
    created_at: str = "",
) -> str:
    """Render a stored long-form summary (Markdown) as a standalone reading page."""
    body_html = _sanitize_summary_html(
        _markdown.markdown(summary_md, extensions=["extra", "sane_lists"])
    )
    bits = []
    if word_count:
        bits.append(f"{word_count:,} words")
    if source == "url":
        bits.append("from the full article")
    if model:
        bits.append(model)
    if created_at:
        bits.append(created_at)
    meta = " · ".join(bits)
    meta_html = f'<div class="meta">{_html.escape(meta)}</div>' if meta else ""
    return (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">{_VIEWPORT}'
        f"<style>{_SUMMARY_CSS}</style></head><body>"
        f'<div class="doc-head"><div class="kicker">Summary</div>'
        f"<h1>{_html.escape(subject)}</h1>{meta_html}</div>"
        f"{body_html}</body></html>"
    )


MANIFEST = """{
  "name": "Newsfeed Reader",
  "short_name": "Reader",
  "start_url": "/reader",
  "scope": "/reader",
  "display": "standalone",
  "background_color": "#fafafa",
  "theme_color": "#2a6b4f",
  "icons": [
    {"src": "/reader/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}
  ]
}"""

# Inline SVG app icon (a folded-newspaper glyph) so no binary asset is needed.
ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="96" fill="#2a6b4f"/>
  <rect x="128" y="120" width="256" height="272" rx="16" fill="#fafafa"/>
  <rect x="160" y="156" width="192" height="40" rx="6" fill="#2a6b4f"/>
  <rect x="160" y="216" width="192" height="16" rx="6" fill="#9bbfae"/>
  <rect x="160" y="248" width="192" height="16" rx="6" fill="#9bbfae"/>
  <rect x="160" y="280" width="128" height="16" rx="6" fill="#9bbfae"/>
</svg>"""

# Minimal service worker: registering one is what makes Chrome offer "Install".
# It caches nothing (the server is the source of truth); the network-passthrough
# fetch handler exists only so the app satisfies installability checks.
SERVICE_WORKER = """
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
"""

# The single-page app. Two screens — a summary card and an in-place reading view —
# driven by a client-side cursor over the queue fetched once from /api/queue.
READER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#2a6b4f">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Reader">
<link rel="manifest" href="/manifest.webmanifest">
<title>Reader</title>
<style>
  * { box-sizing: border-box; }
  :root { --bg:#fafafa; --card:#fff; --ink:#1a1a1a; --muted:#6b6b6b; --line:#e4e4e4;
          --brand:#2a6b4f; --up:#2a9d5c; --down:#c0563d; --ok:#3b7dd8; --star:#f6b40a; }
  html, body, #app { margin:0; height:100%; }
  body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         background:var(--bg); color:var(--ink); overscroll-behavior:none; overflow-x:hidden; }
  button { font:inherit; cursor:pointer; -webkit-tap-highlight-color:transparent; color:inherit; }

  /* ---- Card screen ---- */
  #card-screen { height:100%; display:flex; flex-direction:column;
                 padding:max(16px,env(safe-area-inset-top)) 16px max(16px,env(safe-area-inset-bottom)); }
  .topbar { display:flex; align-items:center; justify-content:space-between;
            color:var(--muted); font-size:.85rem; margin-bottom:12px; }
  .topbar .counter { font-weight:600; color:var(--ink); }
  .topbar a { color:var(--muted); text-decoration:none; }
  .progress { height:3px; background:var(--line); border-radius:2px; margin:-4px 0 12px; overflow:hidden; }
  .progress div { height:100%; background:var(--brand); transition:width .25s; }
  .card { position:relative; background:var(--card); border:1px solid var(--line); border-radius:16px;
          padding:22px 20px; box-shadow:0 2px 10px rgba(0,0,0,.05); flex:1; min-height:0;
          display:flex; flex-direction:column; touch-action:pan-y; user-select:none;
          transition:transform .2s ease-out; }
  .card.dragging { transition:none; }
  .stamp { position:absolute; top:18px; padding:4px 12px; border:3px solid; border-radius:8px;
           font-weight:800; letter-spacing:.08em; opacity:0; pointer-events:none; }
  .stamp-ignore { right:18px; color:var(--down); transform:rotate(8deg); }
  .stamp-read { left:18px; color:var(--up); transform:rotate(-8deg); }
  .badge { display:inline-block; border-radius:6px; padding:2px 9px; font-size:.72rem;
           font-weight:700; color:#fff; }
  .badge-high{background:#2a9d5c;} .badge-medium{background:#c99a00;} .badge-low{background:#9a9a9a;}
  .badge-lock{background:#b4553a;}
  .src { color:var(--muted); font-size:.85rem; }
  .card h1 { font-size:1.4rem; line-height:1.3; margin:14px 0 6px; }
  .meta { color:var(--muted); font-size:.85rem; margin-bottom:16px; }
  .summary { font-size:1.05rem; line-height:1.6; color:#2a2a2a; overflow-y:auto; flex:1; min-height:0; }
  .chips { margin-top:16px; display:flex; flex-wrap:wrap; gap:6px; }
  .chip { font-size:.74rem; padding:2px 10px; border-radius:12px;
          background:#eef4f0; color:#2a5b45; }

  /* Secondary: judge from the summary without opening. */
  .quick { display:flex; gap:8px; margin-top:12px; }
  .quick button { flex:1; padding:8px 0; border-radius:10px; border:1px solid var(--line);
                  background:var(--card); font-size:1.05rem; }
  .quick button:disabled { opacity:.6; }
  .quick .star.on, .rate-bar .star.on { background:var(--star); border-color:var(--star); }
  .lbl { font-size:.6rem; display:block; color:var(--muted); font-weight:600;
         text-transform:uppercase; letter-spacing:.03em; margin-top:2px; }

  /* Primary triage verbs, in the thumb zone. */
  .actions { display:flex; gap:10px; margin-top:10px; }
  .actions button { flex:1; padding:16px 0; border-radius:12px; font-size:1.05rem; font-weight:600;
                    border:1px solid var(--line); }
  .btn-ignore { background:#fbeeea; color:var(--down); border-color:#f0d3ca !important; }
  .btn-later { background:#f0f0f0; color:#555; }
  .btn-read { background:var(--brand); color:#fff; border-color:var(--brand) !important; flex:1.3 !important; }
  .keys { display:none; text-align:center; color:var(--muted); font-size:.75rem; margin-top:10px; }
  @media (hover:hover) and (pointer:fine) { .keys { display:block; } }
  kbd { font:inherit; border:1px solid var(--line); border-radius:4px; padding:0 4px; background:#fff; }

  /* ---- Reading screen ---- */
  #read-screen { position:fixed; inset:0; background:var(--card); display:flex;
                 flex-direction:column; z-index:10; }
  .read-head { display:flex; align-items:center; gap:8px; padding:8px 12px;
               padding-top:max(10px,env(safe-area-inset-top)); border-bottom:1px solid var(--line); }
  .read-head .title { flex:1; font-size:.9rem; font-weight:600; white-space:nowrap;
                      overflow:hidden; text-overflow:ellipsis; }
  .icon-btn { background:none; border:none; font-size:1rem; padding:8px 10px; border-radius:8px; }
  .icon-btn.on { background:#eef4f0; color:var(--brand); }
  #frame { flex:1; width:100%; border:0; background:#fff; }
  .rate-bar { display:flex; gap:8px; padding:10px 12px;
              padding-bottom:max(10px,env(safe-area-inset-bottom)); border-top:1px solid var(--line); }
  .rate-bar button { flex:1; padding:12px 0; border-radius:12px; border:1px solid var(--line);
                     background:#f5f5f5; font-size:1.2rem; }

  /* ---- Toast ---- */
  #toast { position:fixed; left:50%; bottom:calc(150px + env(safe-area-inset-bottom));
           transform:translate(-50%, 20px); opacity:0; pointer-events:none;
           background:#222; color:#fff; border-radius:24px; padding:10px 8px 10px 18px;
           display:flex; align-items:center; gap:12px; font-size:.9rem; z-index:20;
           transition:opacity .15s, transform .15s; white-space:nowrap; }
  #toast.show { opacity:1; transform:translate(-50%, 0); pointer-events:auto; }
  #toast button { background:none; border:none; color:#8fd3ae; font-weight:700; padding:4px 10px; }
  #toast button[hidden] { display:none; }

  /* ---- Empty / done ---- */
  .center { min-height:100%; display:flex; flex-direction:column; align-items:center;
            justify-content:center; text-align:center; padding:40px; color:var(--muted); gap:10px; }
  .center .big { font-size:3rem; }
  .center .head { color:var(--ink); font-size:1.2rem; font-weight:600; }
  .center a { color:var(--brand); font-weight:600; text-decoration:none; margin-top:8px; }
  .center button { width:100%; max-width:320px; padding:14px; border-radius:12px; font-weight:600;
                   border:1px solid var(--line); background:var(--card); }
  .center button.primary { background:var(--brand); color:#fff; border-color:var(--brand); }
</style>
</head>
<body>

<div id="app"><div class="center"><div>Loading your queue…</div></div></div>
<div id="toast" role="status"><span></span><button type="button">Undo</button></div>

<template id="tpl-card">
  <div id="card-screen">
    <div class="topbar"><span class="counter"></span><a href="/library">Library ↗</a></div>
    <div class="progress"><div></div></div>
    <div class="card">
      <div class="stamp stamp-ignore">IGNORE</div>
      <div class="stamp stamp-read">READ</div>
      <div>
        <span class="badge tier"></span>
        <span class="badge badge-lock lock" style="display:none">🔒</span>
        <span class="src"></span>
      </div>
      <h1 class="subject"></h1>
      <div class="meta"></div>
      <div class="summary"></div>
      <div class="chips"></div>
    </div>
    <div class="quick">
      <button class="star" data-act="star">★<span class="lbl">Star</span></button>
      <button data-sent="down">👎<span class="lbl">Lower</span></button>
      <button data-sent="confirmed">✓<span class="lbl">Right</span></button>
      <button data-sent="up">👍<span class="lbl">Higher</span></button>
      <button class="sum">📄<span class="lbl"></span></button>
    </div>
    <div class="actions">
      <button class="btn-ignore">Ignore</button>
      <button class="btn-later">Later</button>
      <button class="btn-read">Read →</button>
    </div>
    <div class="keys"><kbd>x</kbd> ignore · <kbd>s</kbd> later · <kbd>↵</kbd> read ·
      <kbd>1</kbd><kbd>2</kbd><kbd>3</kbd> rate · <kbd>*</kbd> star · <kbd>u</kbd> undo</div>
  </div>
</template>

<template id="tpl-read">
  <div id="read-screen">
    <div class="read-head">
      <button class="icon-btn back">‹ Back</button>
      <span class="title"></span>
      <button class="icon-btn reader-toggle" title="Reader view">Aa</button>
      <button class="icon-btn open-ext" title="Open original">↗</button>
    </div>
    <iframe id="frame" sandbox="allow-same-origin"></iframe>
    <div class="rate-bar">
      <button class="star" data-act="star">★<span class="lbl">Star</span></button>
      <button data-sent="down">👎<span class="lbl">Lower</span></button>
      <button data-sent="confirmed">✓<span class="lbl">Right</span></button>
      <button data-sent="up">👍<span class="lbl">Higher</span></button>
    </div>
  </div>
</template>

<script>
// Queue state (ADR 0008). Today is worked first; the backlog is opt-in once it's done.
let TODAY = [], BACKLOG = [], BACKLOG_COUNT = 0;
let phase = 'today', Q = [], cursor = 0, phaseTotal = 0;
// Undo stack: each entry restores the cursor and reverses its server write.
let HISTORY = [];

const SENT_LABEL = {down:'👎 Rated lower', confirmed:'✓ Rated right', up:'👍 Rated higher'};
const SWIPE_PX = 100;

async function post(url, body) {
  try {
    const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)});
    return r.ok ? await r.json() : null;
  } catch (_) { return null; }
}
function tierClass(t){ return t==='high'?'badge-high':t==='medium'?'badge-medium':'badge-low'; }
function el(html){ const t=document.createElement('template'); t.innerHTML=html.trim(); return t.content.firstElementChild; }
const app = () => document.getElementById('app');
const current = () => Q[cursor];

// ---- toast -----------------------------------------------------------------
let toastTimer;
function toast(text, undoable) {
  const t = document.getElementById('toast');
  t.querySelector('span').textContent = text;
  t.querySelector('button').hidden = !undoable;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hideToast, 5000);
}
function hideToast(){ document.getElementById('toast').classList.remove('show'); }
document.querySelector('#toast button').onclick = () => undo();

// ---- actions ---------------------------------------------------------------
// Optimistic: advance immediately so swiping never waits on the network; if the
// write fails, roll this step back (when it's still the latest) and say so.
async function act({label, write, reverse, appended=false}) {
  const entry = {cursor, appended, reverse};
  HISTORY.push(entry);
  cursor++;
  closeReader(); render();
  toast(label, true);
  if (write && !(await write())) {
    if (HISTORY[HISTORY.length-1] === entry) { HISTORY.pop(); rollback(entry); }
    toast("Couldn't save — check the connection", false);
  }
}
function rollback(e) {
  if (e.appended) Q.pop();
  if (e.restore) e.restore();
  cursor = e.cursor; closeReader(); render();
}
async function undo() {
  const e = HISTORY.pop();
  if (!e) return;
  hideToast();
  if (e.reverse && !(await e.reverse())) { HISTORY.push(e); toast('Undo failed', false); return; }
  rollback(e);
}

function ignore(a) {
  act({label:'Ignored', write:() => post('/api/dismiss', {message_id:a.message_id}),
       reverse:() => post('/api/dismiss', {message_id:a.message_id, dismissed:false})});
}
function later(a) {
  Q.push(a);
  act({label:'Moved to the end', appended:true});
}
function rate(a, sent) {
  // /api/rate marks the item read; a null sentiment clears the reaction and un-reads it.
  act({label:SENT_LABEL[sent], write:() => post('/api/rate', {message_id:a.message_id, sentiment:sent}),
       reverse:() => post('/api/rate', {message_id:a.message_id, sentiment:null})});
}
async function toggleStar(a, btns) {
  const on = !a.starred;
  a.starred = on; btns.forEach(b => b.classList.toggle('on', on));
  if (!await post('/api/star', {message_id:a.message_id, starred:on})) {
    a.starred = !on; btns.forEach(b => b.classList.toggle('on', !on));
    toast("Couldn't save star", false);
  }
}
async function summarize(a, btn) {
  if (a.has_summary) { openReader(a, true); return; }
  const lbl = btn.querySelector('.lbl');
  btn.disabled = true; lbl.textContent = 'Working…';
  toast('Summarizing — this can take a minute', false);
  const ok = await post('/api/summarize', {message_id: a.message_id});
  btn.disabled = false;
  if (!ok) { lbl.textContent = 'Retry'; toast('Summary failed', false); return; }
  a.has_summary = true;
  hideToast();
  if (current() === a) { lbl.textContent = 'Summary'; openReader(a, true); }
}

// ---- screens ---------------------------------------------------------------
function render() {
  if (cursor < Q.length) showCard(Q[cursor]);
  else if (phase === 'today') showTodayDone();
  else showAllDone();
}

function showCard(a) {
  const node = document.getElementById('tpl-card').content.cloneNode(true);
  const left = Q.length - cursor;
  node.querySelector('.counter').textContent = phase === 'today'
    ? `${left} left today` : `Backlog · ${left} left`;
  node.querySelector('.progress div').style.width =
    `${phaseTotal ? Math.min(100, 100 * (phaseTotal - left) / phaseTotal) : 0}%`;
  const tier = node.querySelector('.tier');
  tier.textContent = a.score != null ? a.score.toFixed(1) : '–';
  tier.classList.add(tierClass(a.tier));
  if (a.paywalled) node.querySelector('.lock').style.display='';
  node.querySelector('.src').textContent = a.source === 'url' ? '  🔗 web' : '';
  node.querySelector('.subject').textContent = a.subject || '(no subject)';
  const readTime = a.reading_minutes ? `${a.reading_minutes} min read` : '';
  node.querySelector('.meta').textContent = [a.sender_name, a.date, readTime].filter(Boolean).join('  ·  ');
  node.querySelector('.summary').textContent = a.summary || '(no summary)';
  const chips = node.querySelector('.chips');
  (a.tags||[]).forEach(t => { const c=document.createElement('span'); c.className='chip'; c.textContent=t; chips.appendChild(c); });

  const star = node.querySelector('.quick .star');
  star.classList.toggle('on', !!a.starred);
  star.onclick = () => toggleStar(a, [star]);
  node.querySelectorAll('.quick [data-sent]').forEach(b => b.onclick = () => rate(a, b.dataset.sent));
  const sum = node.querySelector('.sum');
  sum.querySelector('.lbl').textContent = a.has_summary ? 'Summary' : 'Summarize';
  sum.onclick = () => summarize(a, sum);
  node.querySelector('.btn-ignore').onclick = () => ignore(a);
  node.querySelector('.btn-later').onclick = () => later(a);
  node.querySelector('.btn-read').onclick = () => openReader(a);

  const card = node.querySelector('.card');
  app().replaceChildren(node);
  attachSwipe(card, a);
}

// Horizontal swipes only: the card is `touch-action: pan-y`, so vertical drags
// stay native scrolling of the summary and never trigger an action.
function attachSwipe(card, a) {
  let x0 = null, y0 = 0, dx = 0, horizontal = false;
  const stampI = card.querySelector('.stamp-ignore'), stampR = card.querySelector('.stamp-read');
  card.addEventListener('pointerdown', e => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    x0 = e.clientX; y0 = e.clientY; dx = 0; horizontal = false;
  });
  card.addEventListener('pointermove', e => {
    if (x0 === null) return;
    dx = e.clientX - x0;
    const dy = e.clientY - y0;
    if (!horizontal) {
      if (Math.abs(dx) < 10 || Math.abs(dx) < Math.abs(dy)) return;
      horizontal = true; card.classList.add('dragging'); card.setPointerCapture(e.pointerId);
    }
    card.style.transform = `translateX(${dx}px) rotate(${dx/25}deg)`;
    stampI.style.opacity = Math.max(0, Math.min(1, -dx / SWIPE_PX));
    stampR.style.opacity = Math.max(0, Math.min(1, dx / SWIPE_PX));
  });
  const end = () => {
    if (x0 === null) return;
    x0 = null;
    card.classList.remove('dragging');
    if (horizontal && Math.abs(dx) >= SWIPE_PX) {
      card.style.transform = `translateX(${Math.sign(dx) * window.innerWidth * 1.2}px) rotate(${dx/15}deg)`;
      setTimeout(() => dx < 0 ? ignore(a) : openReader(a), 150);
    } else {
      card.style.transform = ''; stampI.style.opacity = 0; stampR.style.opacity = 0;
    }
  };
  card.addEventListener('pointerup', end);
  card.addEventListener('pointercancel', () => { dx = 0; end(); });
}

function showTodayDone() {
  const node = el(`<div class="center"><div class="big">✓</div>
    <div class="head">Today's digest is done.</div><div class="sub"></div></div>`);
  const sub = node.querySelector('.sub');
  if (!BACKLOG.length) {
    sub.textContent = 'Nothing older is waiting either.';
    node.appendChild(el('<a href="/library">Browse the Library ↗</a>'));
    app().replaceChildren(node); return;
  }
  sub.textContent = `${BACKLOG_COUNT} older unread item${BACKLOG_COUNT === 1 ? '' : 's'} in the backlog.`;
  const go = el('<button class="primary">Triage the backlog</button>');
  go.onclick = startBacklog;
  const low = BACKLOG.filter(a => (a.score ?? 0) < 5).length;
  node.appendChild(go);
  if (low) {
    const bulk = el(`<button>Ignore low-scored backlog (under 5)</button>`);
    bulk.onclick = ignoreLowBacklog;
    node.appendChild(bulk);
  }
  node.appendChild(el('<a href="/library">Browse the Library ↗</a>'));
  app().replaceChildren(node);
}

function showAllDone() {
  app().replaceChildren(el(`<div class="center"><div class="big">✓</div>
    <div class="head">You're all caught up.</div><a href="/library">Browse the Library ↗</a></div>`));
}

function startBacklog() {
  phase = 'backlog'; Q = BACKLOG.slice(); cursor = 0; phaseTotal = Q.length; HISTORY = [];
  render();
}

async function ignoreLowBacklog() {
  const res = await post('/api/dismiss-backlog', {max_score: 5});
  if (!res) { toast("Couldn't reach the server", false); return; }
  const ids = new Set(res.message_ids);
  const prevBacklog = BACKLOG, prevCount = BACKLOG_COUNT;
  BACKLOG = BACKLOG.filter(a => !ids.has(a.message_id));
  BACKLOG_COUNT = Math.max(0, BACKLOG_COUNT - ids.size);
  HISTORY.push({cursor, restore:() => { BACKLOG = prevBacklog; BACKLOG_COUNT = prevCount; },
    reverse:() => ids.size ? post('/api/dismiss', {message_ids:[...ids], dismissed:false}) : Promise.resolve(true)});
  render();
  toast(`Ignored ${ids.size} item${ids.size === 1 ? '' : 's'}`, true);
}

// ---- reading screen --------------------------------------------------------
function openReader(a, summaryMode) {
  closeReader();
  const node = document.getElementById('tpl-read').content.cloneNode(true);
  const screen = node.querySelector('#read-screen');
  const frame = node.querySelector('#frame');
  const readable = !!a.archive_path;
  let readerMode = false;

  function load() {
    if (summaryMode) frame.src = `/reader/summary/${encodeURIComponent(a.message_id)}`;
    else if (readable) frame.src = `/reader/article/${encodeURIComponent(a.message_id)}${readerMode?'?mode=reader':''}`;
    else frame.srcdoc = `<body style="font:18px/1.6 -apple-system,sans-serif;padding:24px;color:#444">`
      + `<p>No offline copy for this item.</p>`
      + (a.url ? `<p>Use ↗ above to open the original.</p>` : '') + `</body>`;
  }
  load();
  node.querySelector('.title').textContent = (summaryMode ? '📄 ' : '') + (a.subject || '');
  node.querySelector('.back').onclick = () => { closeReader(); render(); };
  const toggle = node.querySelector('.reader-toggle');
  toggle.onclick = () => { readerMode=!readerMode; toggle.classList.toggle('on',readerMode); load(); };
  if (!readable || summaryMode) toggle.style.display='none';
  const ext = node.querySelector('.open-ext');
  if (a.url) ext.onclick = () => window.open(a.url, '_blank', 'noopener'); else ext.style.display='none';

  const star = node.querySelector('.star');
  star.classList.toggle('on', !!a.starred);
  star.onclick = () => toggleStar(a, [star]);
  node.querySelectorAll('[data-sent]').forEach(b => b.onclick = () => rate(a, b.dataset.sent));

  document.body.style.overflow='hidden';
  app().appendChild(screen);
  // A history entry so the phone's back gesture closes the article instead of the app.
  history.pushState({reader: true}, '');
}

function closeReader(fromPop) {
  const screen = document.getElementById('read-screen');
  if (!screen) return false;
  screen.remove(); document.body.style.overflow='';
  if (!fromPop && history.state && history.state.reader) history.back();
  return true;
}
window.addEventListener('popstate', () => { if (closeReader(true)) render(); });

// ---- keyboard --------------------------------------------------------------
document.addEventListener('keydown', e => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const reading = !!document.getElementById('read-screen');
  const a = current();
  if (e.key === 'u') { undo(); return; }
  if (e.key === 'Escape' && reading) { closeReader(); render(); return; }
  if (!a) return;
  const rateKeys = {'1':'down', '2':'confirmed', '3':'up'};
  if (rateKeys[e.key]) { rate(a, rateKeys[e.key]); return; }
  if (e.key === '*') { toggleStar(a, document.querySelectorAll('.star')); return; }
  if (reading) return;
  if (e.key === 'x') ignore(a);
  else if (e.key === 's') later(a);
  else if (e.key === 'Enter') { e.preventDefault(); openReader(a); }
});

// ---- boot ------------------------------------------------------------------
async function boot() {
  if ('serviceWorker' in navigator) { try { await navigator.serviceWorker.register('/sw.js'); } catch(_){} }
  let q;
  try {
    const r = await fetch('/api/queue');
    q = await r.json();
  } catch (_) {
    app().replaceChildren(el(
      `<div class="center"><div class="big">⚠️</div><div>Couldn't reach the server.</div></div>`));
    return;
  }
  TODAY = q.today; BACKLOG = q.backlog; BACKLOG_COUNT = q.backlog_count;
  Q = TODAY.slice(); phaseTotal = Q.length;
  render();
}
boot();
</script>
</body>
</html>
"""
