"""
Quiz-gram -- 10 statements, one at a time, Instagram-style up/down scrolling.
Put this file at:  pages/1_Quiz-gram.py  (next to app.py).
This is a COMPLETE, single-file version: it creates its own helper files (qg_loader.py and the
quizgram_component folder) automatically in the root folder, so there is nothing else to upload.

What this version adds:
  * Adaptive picking (wrong + never-attempted statements), same idea as app.py
  * Auto-save (no copy/paste) through a small custom component
  * GitHub backup of your results (uses the same secrets as app.py)
  * Vibration on a wrong answer (Android phones, "Instant" feedback mode)
  * Fix: component is declared through an auto-created qg_loader.py (avoids "module is None" error)
"""

import base64
import importlib
import json
import os
import random
import re
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

try:
    import requests
except ImportError:
    requests = None

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    st.set_page_config(page_title="Quiz-gram", page_icon="📸", layout="centered")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SAVE_FILE = ROOT / "statement_responses.json"  # separate file, so app.py's scoring is not disturbed
BACKUP_PATH = "backup/statement_responses.json"  # where the GitHub copy lives
COMP_DIR = ROOT / "quizgram_component"           # created automatically below
BOOKMARK_FILE = ROOT / "bookmarked_statements.json"            # your starred statements
BOOKMARK_BACKUP_PATH = "backup/bookmarked_statements.json"     # GitHub copy of the above
# Every file that is backed up to / restored from GitHub: (local file, GitHub path)
BACKUP_FILES = [(SAVE_FILE, BACKUP_PATH), (BOOKMARK_FILE, BOOKMARK_BACKUP_PATH)]

# Make sure qg_loader.py (in the root folder) can be imported from inside pages/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_REVIEW_SHARE = 0.4   # used until you have finished one round (same as app.py)
RECENCY_HALF_LIFE_DAYS = 30
MASTERY_PRIOR = 0.6
MASTERY_PRIOR_STRENGTH = 2.0

SAMPLE_STATEMENTS = [
    {"id": "S-1", "topic": "Polity", "text": "Article 21A provides free education for ages 6 to 14.",
     "is_true": True, "why": "Added by the 86th Amendment, 2002."},
    {"id": "S-2", "topic": "Polity", "text": "The President can be impeached for any reason.",
     "is_true": False, "why": "Only for violating the Constitution (Article 61)."},
    {"id": "S-3", "topic": "History", "text": "Ashoka fought the Kalinga war in 261 BCE.",
     "is_true": True, "why": "It changed him toward Buddhism."},
    {"id": "S-4", "topic": "Economy", "text": "RBI is owned by private banks.",
     "is_true": False, "why": "RBI is fully owned by the Government of India."},
]


# ---------- Loading statements from /data ----------

@st.cache_data
def load_statements():
    """Reads every *.json in /data. Understands two shapes:
    1) your existing 'statements' questions (each statement inside becomes one card)
    2) simple records: {statement_id, parent_topic, text, is_true, why}
    Disputed questions are skipped."""
    found = {}
    for f in sorted(DATA_DIR.glob("*.json")):
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(raw, list):
            continue
        for q in raw:
            if not isinstance(q, dict) or q.get("disputed"):
                continue
            topic = q.get("parent_topic") or q.get("theme") or "General"
            if q.get("question_type") == "statements" and isinstance(q.get("statements"), list):
                for i, s in enumerate(q["statements"], 1):
                    if isinstance(s, dict) and s.get("text") and isinstance(s.get("is_true"), bool):
                        sid = f"{q.get('question_id', 'Q')}#{i}"
                        found[sid] = {"id": sid, "topic": topic, "text": s["text"],
                                      "is_true": s["is_true"], "why": s.get("why", "")}
            elif q.get("text") and isinstance(q.get("is_true"), bool):
                sid = q.get("statement_id") or q.get("question_id")
                if sid:
                    found[sid] = {"id": sid, "topic": topic, "text": q["text"],
                                  "is_true": q["is_true"], "why": q.get("why", "")}
    return list(found.values())


# ---------- Your saved history ----------

def load_history(user):
    """All saved statement answers for this user (oldest first)."""
    if not user or not SAVE_FILE.exists():
        return []
    try:
        text = SAVE_FILE.read_text(encoding="utf-8").strip()
        rows = json.loads(text) if text else []
    except Exception:
        return []
    mine = [r for r in rows if isinstance(r, dict) and r.get("user") == user]
    return sorted(mine, key=lambda r: r.get("timestamp", ""))


def history_stats(history):
    """For each statement: was it attempted, was the LAST try correct,
    the current wrong-in-a-row streak (skips are ignored for the streak), and when it was last seen."""
    attempted, last_correct, streak, last_ts = set(), {}, defaultdict(int), {}
    for e in history:
        sid = e.get("statement_id")
        if not sid:
            continue
        attempted.add(sid)
        last_ts[sid] = e.get("timestamp", "")
        ok = e.get("selected") == e.get("correct_answer")
        last_correct[sid] = ok
        if e.get("selected") != "SKIPPED":
            streak[sid] = 0 if ok else streak[sid] + 1
    return attempted, last_correct, streak, last_ts


def last_round_accuracy(history):
    """Score (0 to 1) of your most recent round, or None if you have no history yet."""
    rounds = defaultdict(list)
    for e in history:
        if e.get("round_id"):
            rounds[e["round_id"]].append(e)
    if not rounds:
        return None
    latest = max(rounds, key=lambda rid: max(x.get("timestamp", "") for x in rounds[rid]))
    rows = rounds[latest]
    return sum(1 for x in rows if x.get("selected") == x.get("correct_answer")) / len(rows)


def topic_weakness(history):
    """0 = fully mastered, 1 = fully weak. Recent answers count more; small samples
    are pulled toward a neutral value (same maths idea as app.py)."""
    now = datetime.now()
    wc, wa = defaultdict(float), defaultdict(float)
    for e in history:
        topic = e.get("topic")
        if not topic:
            continue
        try:
            days = max((now - datetime.fromisoformat(e["timestamp"])).total_seconds() / 86400.0, 0.0)
        except Exception:
            days = 0.0
        w = 0.5 ** (days / RECENCY_HALF_LIFE_DAYS)
        wa[topic] += w
        if e.get("selected") == e.get("correct_answer"):
            wc[topic] += w
    return {t: 1.0 - (wc[t] + MASTERY_PRIOR * MASTERY_PRIOR_STRENGTH) / (wa[t] + MASTERY_PRIOR_STRENGTH)
            for t in wa}


def _hours_since(ts):
    try:
        return max((datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 3600.0, 0.0)
    except Exception:
        return 0.0


def split_pool(pool, history):
    """Three buckets: never attempted / to revise (last try wrong or skipped) / mastered (last try correct)."""
    attempted, last_correct, streak, last_ts = history_stats(history)
    new_items = [s for s in pool if s["id"] not in attempted]
    revise = [s for s in pool if s["id"] in attempted and not last_correct.get(s["id"], False)]
    mastered = [s for s in pool if s["id"] in attempted and last_correct.get(s["id"], False)]
    # Most worrying first: a longer wrong streak, and longer since you last saw it
    revise.sort(key=lambda s: streak.get(s["id"], 0) * 24 + _hours_since(last_ts.get(s["id"], "")), reverse=True)
    return new_items, revise, mastered, last_ts


def weighted_sample(items, weights, k):
    items, weights, out = list(items), list(weights), []
    for _ in range(min(k, len(items))):
        i = random.choices(range(len(items)), weights=weights)[0]
        out.append(items.pop(i))
        weights.pop(i)
    return out


def pick_adaptive(pool, history, n=10):
    """Same plan as app.py:
    1) a guaranteed share of the round goes to statements you got wrong (share = your last round's score,
       or 40% before your first round), most worrying first
    2) the rest comes from never-attempted statements, favouring weak and less-explored topics
    3) if one side runs short, the other side fills in; already-correct statements are the last resort."""
    new_items, revise, mastered, last_ts = split_pool(pool, history)
    accuracy = last_round_accuracy(history)
    share = DEFAULT_REVIEW_SHARE if accuracy is None else accuracy
    quota = min(round(n * share), len(revise))
    session = revise[:quota]

    need = n - len(session)
    if need > 0 and new_items:
        weakness = topic_weakness(history)
        attempted_ids = {e.get("statement_id") for e in history}
        totals, seen = defaultdict(int), defaultdict(int)
        for s in pool:
            totals[s["topic"]] += 1
            if s["id"] in attempted_ids:
                seen[s["topic"]] += 1
        weights = []
        for s in new_items:
            t = s["topic"]
            topic_w = 3.0 if t not in weakness else 0.5 + weakness[t] * 6.0
            coverage_w = max(1.0 - seen[t] / totals[t], 0.1)
            weights.append(topic_w * coverage_w)
        session.extend(weighted_sample(new_items, weights, need))

    need = n - len(session)
    if need > 0:
        session.extend(revise[quota:quota + need])

    need = n - len(session)
    if need > 0 and mastered:
        weights = [1.0 + _hours_since(last_ts.get(s["id"], "")) / 24.0 for s in mastered]
        session.extend(weighted_sample(mastered, weights, need))

    random.shuffle(session)
    return session


def pick_balanced(pool, topics, n=10):
    """Plain mode: takes statements from each chosen topic in turn."""
    by_topic = {t: [s for s in pool if s["topic"] == t] for t in topics}
    for lst in by_topic.values():
        random.shuffle(lst)
    order = [t for t in topics if by_topic[t]]
    chosen = []
    while len(chosen) < n and order:
        for t in list(order):
            if len(chosen) >= n:
                break
            chosen.append(by_topic[t].pop())
            if not by_topic[t]:
                order.remove(t)
    random.shuffle(chosen)
    return chosen


# ---------- Saving results ----------

def save_round(user, round_id, items, code):
    now = datetime.now().isoformat()
    entries = []
    for s, ch in zip(items, code):
        entries.append({
            "user": user, "round_id": round_id, "statement_id": s["id"], "topic": s["topic"],
            "selected": {"T": "T", "F": "F"}.get(ch, "SKIPPED"),
            "correct_answer": "T" if s["is_true"] else "F",
            "timestamp": now,
        })
    lock_path = SAVE_FILE.with_name(SAVE_FILE.name + ".lock")
    with open(lock_path, "a+") as lock_f:
        if fcntl:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            existing = []
            if SAVE_FILE.exists():
                text = SAVE_FILE.read_text(encoding="utf-8").strip()
                existing = json.loads(text) if text else []
            existing.extend(entries)
            fd, tmp = tempfile.mkstemp(dir=str(SAVE_FILE.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as tf:
                json.dump(existing, tf, indent=2, ensure_ascii=False)
            os.replace(tmp, SAVE_FILE)
        finally:
            if fcntl:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    return sum(1 for e in entries if e["selected"] == e["correct_answer"])


def valid_code(code, items):
    return bool(re.fullmatch(r"[TFS]+", code)) and len(code) == len(items)


# ---------- Bookmarked statements ----------
# Saved as {"your name": {"statement_id": "time you starred it", ...}}

def _locked_update(path, default, mutate):
    """Opens a JSON file safely (with a lock), lets `mutate(data)` change it,
    and saves it only if mutate() says something changed (returns True)."""
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a+") as lock_f:
        if fcntl:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            data = default
            if path.exists():
                text = path.read_text(encoding="utf-8").strip()
                data = json.loads(text) if text else default
            if not isinstance(data, dict):
                data = default
            changed = mutate(data)
            if changed:
                fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as tf:
                    json.dump(data, tf, indent=2, ensure_ascii=False)
                os.replace(tmp, path)
            return changed
        finally:
            if fcntl:
                fcntl.flock(lock_f, fcntl.LOCK_UN)


def load_bookmarks(user):
    """Dict: statement_id -> time starred, for this user."""
    if not user or not BOOKMARK_FILE.exists():
        return {}
    try:
        text = BOOKMARK_FILE.read_text(encoding="utf-8").strip()
        data = json.loads(text) if text else {}
    except Exception:
        return {}
    mine = data.get(user, {}) if isinstance(data, dict) else {}
    return mine if isinstance(mine, dict) else {}


def sync_round_bookmarks(user, round_ids, marked_ids):
    """Makes the saved bookmarks match the stars in this round.
    Only statements from this round are touched, so your older bookmarks stay safe."""
    def mutate(data):
        mine = data.setdefault(user, {})
        changed = False
        for sid in round_ids:
            if sid in marked_ids and sid not in mine:
                mine[sid] = datetime.now().isoformat()
                changed = True
            elif sid not in marked_ids and sid in mine:
                del mine[sid]
                changed = True
        return changed
    return _locked_update(BOOKMARK_FILE, {}, mutate)


def remove_bookmark(user, statement_id):
    def mutate(data):
        mine = data.get(user, {})
        if isinstance(mine, dict) and statement_id in mine:
            del mine[statement_id]
            return True
        return False
    return _locked_update(BOOKMARK_FILE, {}, mutate)


# ---------- GitHub backup (uses the same secrets as app.py) ----------

def _github_cfg():
    if requests is None:
        return None
    try:
        token = st.secrets.get("github_token")
        repo = st.secrets.get("github_repo")
    except Exception:
        return None
    if not token or not repo:
        return None
    return {"headers": {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            "repo": repo}


def github_restore_if_needed():
    """If a local file is missing (e.g. after a restart), bring it back from GitHub."""
    cfg = _github_cfg()
    if not cfg:
        return
    for local_path, repo_path in BACKUP_FILES:
        if local_path.exists():
            continue
        try:
            url = f"https://api.github.com/repos/{cfg['repo']}/contents/{repo_path}"
            resp = requests.get(url, headers=cfg["headers"], timeout=10)
            if resp.status_code == 200:
                content = base64.b64decode(resp.json().get("content", "").replace("\n", "")).decode("utf-8")
                local_path.write_text(content, encoding="utf-8")
        except Exception:
            pass


def _push_file(cfg, local_path, repo_path):
    """Uploads one file to GitHub. Returns (True, "Success") or (False, "what went wrong")."""
    try:
        b64 = base64.b64encode(local_path.read_text(encoding="utf-8").encode("utf-8")).decode("ascii")
        url = f"https://api.github.com/repos/{cfg['repo']}/contents/{repo_path}"
        for _ in range(3):  # retry if another save happened at the same moment
            get_resp = requests.get(url, headers=cfg["headers"], timeout=10)
            sha = get_resp.json().get("sha") if get_resp.status_code == 200 else None
            payload = {"message": f"Quiz-gram backup {local_path.name} - {datetime.now().isoformat()}", "content": b64}
            if sha:
                payload["sha"] = sha
            put_resp = requests.put(url, headers=cfg["headers"], json=payload, timeout=10)
            if put_resp.status_code in (200, 201):
                return True, "Success"
            if put_resp.status_code != 409:
                return False, f"GitHub returned {put_resp.status_code} for {local_path.name}."
        return False, "GitHub kept reporting write conflicts. Try again in a moment."
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def github_backup():
    """Backs up your results AND your bookmarks. Returns (True, "Success") or (False, "what went wrong").
    Never crashes the page."""
    cfg = _github_cfg()
    if not cfg:
        return False, "GitHub backup is not set up (github_token / github_repo missing in secrets)."
    failures, any_file = [], False
    for local_path, repo_path in BACKUP_FILES:
        if not local_path.exists():
            continue
        any_file = True
        ok, msg = _push_file(cfg, local_path, repo_path)
        if not ok:
            failures.append(msg)
    if not any_file:
        return False, "Nothing to back up yet."
    if failures:
        return False, " ".join(failures)
    return True, "Success"


# ---------- The scrolling quiz: a small custom component ----------
# This HTML file is written into the quizgram_component folder automatically.
# It talks to Python through Streamlit's message system, which is what makes auto-save possible.

COMPONENT_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; overflow: hidden; font-family: -apple-system, "Segoe UI", Roboto, sans-serif; color: #0f172a; background: transparent; }
  body { position: relative; }
  .feed { height: 100vh; overflow-y: scroll; scroll-snap-type: y mandatory; scrollbar-width: none; }
  .feed::-webkit-scrollbar { display: none; }

  .stories { position: absolute; top: 8px; left: 6px; right: 6px; display: flex; gap: 4px; z-index: 5; }
  .seg { flex: 1; height: 4px; border-radius: 3px; background: rgba(15, 23, 42, 0.18); transition: all .2s; }
  .seg.cur { height: 6px; margin-top: -1px; background: rgba(15, 23, 42, 0.45); }
  .seg.done { background: #3b82f6; }
  .seg.ok { background: #22c55e; }
  .seg.no { background: #ef4444; }

  .card { height: 100vh; scroll-snap-align: start; display: flex; flex-direction: column;
          padding: 26px 4px 12px 4px; }
  .g0 { background: linear-gradient(160deg, #fdf2f8, #eef2ff); }
  .g1 { background: linear-gradient(160deg, #ecfeff, #f0fdf4); }
  .g2 { background: linear-gradient(160deg, #fff7ed, #fef2f2); }
  .g3 { background: linear-gradient(160deg, #f5f3ff, #eff6ff); }

  .meta { display: flex; align-items: center; justify-content: space-between; margin: 6px 4px 10px 4px; }
  .count { font-weight: 700; font-size: 14px; color: #475569; }
  .tag { background: white; color: #7c3aed; border-radius: 999px; padding: 4px 12px;
         font-size: 12px; font-weight: 600; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }

  .right { display: flex; align-items: center; gap: 8px; }
  .star { background: white; color: #94a3b8; border: none; border-radius: 999px; width: 38px; height: 32px;
          font-size: 20px; line-height: 1; cursor: pointer; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
  .star.on { color: #f59e0b; }

  .body { flex: 1; overflow-y: auto; min-height: 0; scrollbar-width: none; }
  .body::-webkit-scrollbar { display: none; }
  .post { background: white; border-radius: 18px; padding: 18px 14px; box-shadow: 0 4px 16px rgba(15, 23, 42, 0.08); }
  .text { font-size: 18px; line-height: 1.55; text-align: justify; text-justify: inter-word; }

  .fb { margin-top: 12px; padding: 12px 14px; width: 100%; border-radius: 14px; display: none; line-height: 1.5; font-size: 15px;
        text-align: justify; text-justify: inter-word; }
  .good { background: #dcfce7; color: #166534; display: block; }
  .bad { background: #fee2e2; color: #991b1b; display: block; }
  .note { background: rgba(255,255,255,0.8); color: #475569; display: block; }

  .btns { display: flex; gap: 10px; margin-top: 12px; }
  .btns button { flex: 1; padding: 15px 8px; font-size: 17px; font-weight: 700; border-radius: 14px;
                 border: none; color: white; cursor: pointer; }
  .btns .r { background: linear-gradient(135deg, #22c55e, #16a34a); }
  .btns .w { background: linear-gradient(135deg, #f87171, #dc2626); }
  .btns button.sel { box-shadow: 0 0 0 3px white, 0 0 0 6px #0f172a; }
  .btns button:disabled:not(.sel) { opacity: 0.4; }
  .btns .show { background: linear-gradient(135deg, #a855f7, #3b82f6); }
  .btns.mid { margin-top: 70px; }
  .btns.mid button { padding: 22px 8px; font-size: 19px; }
  .hint { text-align: center; color: #94a3b8; font-size: 12px; margin-top: 8px; }

  .savestat { margin-top: 10px; padding: 10px 12px; border-radius: 10px; background: white; color: #334155;
              font-size: 14px; text-align: left; }
  .code { font-size: 12px; color: #64748b; margin-top: 8px; text-align: left; word-break: break-all; }
</style>
</head>
<body>
<div class="stories" id="stories"></div>
<div class="feed" id="feed"></div>
<script>
function send(type, extra) {
  window.parent.postMessage(Object.assign({isStreamlitMessage: true, type: type}, extra || {}), "*");
}

const feed = document.getElementById('feed');
const storiesBar = document.getElementById('stories');
let currentRound = null, data = [], choices = [], segs = [];
let INSTANT = true, SHOW_TAG = true, HAPTICS = true, sent = false, lastStatus = '';
let marked = new Set(), bmSeq = 0;   // marked = ids of starred statements

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function fitHeight() {
  let h = 640;
  try {
    const top = window.frameElement.getBoundingClientRect().top;
    const ph = window.parent.innerHeight;
    h = Math.min(780, Math.max(480, ph - Math.max(top, 110) - 8));
  } catch (e) {}
  send('streamlit:setFrameHeight', {height: h});
}

function vibrate(pattern) {
  try { if (navigator.vibrate) navigator.vibrate(pattern); } catch (e) {}
}

function refresh() {
  const cur = Math.min(data.length - 1, Math.round(feed.scrollTop / Math.max(1, feed.clientHeight)));
  segs.forEach((s, i) => {
    let c = 'seg';
    if (choices[i] !== null) c += INSTANT ? (choices[i] === data[i].is_true ? ' ok' : ' no') : ' done';
    if (i === cur) c += ' cur';
    s.className = c;
  });
}
feed.addEventListener('scroll', refresh);

function sendBookmarks() {
  bmSeq++;
  const ids = data.filter(x => marked.has(x.id)).map(x => x.id);
  send('streamlit:setComponentValue', {value: {type: 'bookmarks', ids: ids, seq: bmSeq}, dataType: 'json'});
}

function applyStatus() {
  const el = document.getElementById('savestat');
  if (!el) return;
  if (lastStatus) el.textContent = lastStatus;
  else if (sent) el.textContent = '⏳ Saving your result…';
}

function build(a) {
  data = a.items || [];
  INSTANT = !!a.instant; SHOW_TAG = !!a.show_tag; HAPTICS = !!a.haptics;
  marked = new Set(a.bookmarked || []);
  choices = new Array(data.length).fill(null);
  sent = false; lastStatus = '';
  feed.innerHTML = ''; storiesBar.innerHTML = '';
  feed.scrollTop = 0;

  segs = data.map(() => {
    const s = document.createElement('div');
    s.className = 'seg';
    storiesBar.appendChild(s);
    return s;
  });

  data.forEach((s, i) => {
    const card = document.createElement('div');
    card.className = 'card g' + (i % 4);
    card.innerHTML =
      '<div class="meta"><span class="count">' + (i + 1) + ' / ' + data.length + '</span>' +
      '<span class="right"><button class="star" type="button" aria-label="Bookmark this statement">☆</button>' +
      (SHOW_TAG ? '<span class="tag">' + esc(s.topic) + '</span>' : '') + '</span></div>' +
      '<div class="body"><div class="post"><div class="text">' + esc(s.text) + '</div></div>' +
      '<div class="fb"></div></div>' +
      '<div class="btns"><button class="r">✅ Right</button><button class="w">❌ Wrong</button></div>' +
      '<div class="hint">⬆️ Swipe up for next</div>';
    const fb = card.querySelector('.fb');
    const rb = card.querySelector('.btns .r'), wb = card.querySelector('.btns .w');
    const star = card.querySelector('.star');
    function paintStar() {
      const on = marked.has(s.id);
      star.textContent = on ? '★' : '☆';
      star.classList.toggle('on', on);
    }
    paintStar();
    star.onclick = () => {
      if (marked.has(s.id)) marked.delete(s.id); else marked.add(s.id);
      paintStar();
      sendBookmarks();
    };

    function pick(c) {
      choices[i] = c;
      rb.classList.toggle('sel', c === true);
      wb.classList.toggle('sel', c === false);
      if (INSTANT) {
        const ok = (c === s.is_true);
        fb.className = 'fb ' + (ok ? 'good' : 'bad');
        fb.textContent = (ok ? '🎉 Correct! ' : '😅 Not quite. ') +
          'This statement is ' + (s.is_true ? 'RIGHT. ' : 'WRONG. ') + (s.why || '');
        rb.disabled = true; wb.disabled = true;
        if (HAPTICS && !ok) vibrate([90, 50, 90]);   // buzz only on a wrong answer
      } else {
        fb.className = 'fb note';
        fb.textContent = '✔️ Answer saved. You can change it until you finish.';
      }
      refresh();
    }
    rb.onclick = () => pick(true);
    wb.onclick = () => pick(false);
    feed.appendChild(card);
  });

  const last = document.createElement('div');
  last.className = 'card g1';
  last.innerHTML =
    '<div class="meta"><span class="count">🏁 Finish</span><span></span></div>' +
    '<div class="body"><div class="post"><div class="text">All done! Tap the button to see your result. It is saved automatically.</div></div>' +
    '<div class="btns mid" id="showwrap"><button class="show" id="show">Show my result</button></div>' +
    '<div class="fb" id="result"></div></div>';
  feed.appendChild(last);
  refresh();

  document.getElementById('show').onclick = () => {
    let score = 0; const byTopic = {}; const wrong = [];
    data.forEach((s, i) => {
      const ok = (choices[i] === s.is_true);
      byTopic[s.topic] = byTopic[s.topic] || {c: 0, t: 0};
      byTopic[s.topic].t++;
      if (ok) { score++; byTopic[s.topic].c++; } else { wrong.push(i); }
    });
    const code = choices.map(c => c === true ? 'T' : (c === false ? 'F' : 'S')).join('');
    let h = '<b style="font-size:20px;">🎯 Score: ' + score + ' / ' + data.length + '</b><br><br>';
    for (const t in byTopic) h += esc(t) + ': ' + byTopic[t].c + '/' + byTopic[t].t + '<br>';
    if (wrong.length) {
      h += '<br><b>❌ To revise:</b><br>';
      wrong.forEach(i => {
        h += '• ' + esc(data[i].text) + '<br>→ ' + (data[i].is_true ? 'RIGHT' : 'WRONG') +
             '. ' + esc(data[i].why || '') + '<br><br>';
      });
    }
    h += '<div class="savestat" id="savestat">⏳ Saving your result…</div>' +
         '<div class="code">Backup code (only needed if auto-save fails): ' + code + '</div>';
    const res = document.getElementById('result');
    res.className = 'fb good';
    res.innerHTML = h;
    document.getElementById('showwrap').style.display = 'none';   // button is not needed anymore
    if (!sent) {
      sent = true;
      send('streamlit:setComponentValue', {value: {round_id: currentRound, code: code}, dataType: 'json'});
    }
    applyStatus();
  };
}

window.addEventListener('message', function (event) {
  if (!event.data || event.data.type !== 'streamlit:render') return;
  const a = event.data.args || {};
  if (a.round_id !== currentRound) {
    currentRound = a.round_id;
    build(a);
  }
  lastStatus = a.save_status || '';
  applyStatus();
  fitHeight();
});

send('streamlit:componentReady', {apiVersion: 1});
</script>
</body>
</html>
"""


LOADER_CODE = '''"""
Helper for Quiz-gram (created automatically by pages/1_Quiz-gram.py -- no need to edit it).
Streamlit's declare_component() must be called from a normal importable module,
not from a script inside pages/ (otherwise: RuntimeError: module is None).
"""

import streamlit.components.v1 as components


def make_component(path):
    return components.declare_component("quizgram", path=str(path))
'''


def _ensure_component_files():
    """Creates (or refreshes) the component page and the small loader module in the root folder."""
    COMP_DIR.mkdir(exist_ok=True)
    index_file = COMP_DIR / "index.html"
    if not index_file.exists() or index_file.read_text(encoding="utf-8") != COMPONENT_HTML:
        index_file.write_text(COMPONENT_HTML, encoding="utf-8")

    loader_file = ROOT / "qg_loader.py"
    if not loader_file.exists() or loader_file.read_text(encoding="utf-8") != LOADER_CODE:
        loader_file.write_text(LOADER_CODE, encoding="utf-8")
    importlib.invalidate_caches()


_COMP_ERROR_TEXT = ""
try:
    _ensure_component_files()
    from qg_loader import make_component
    quizgram = make_component(COMP_DIR)
except Exception as _comp_error:
    quizgram = None
    _COMP_ERROR_TEXT = f"{type(_comp_error).__name__}: {_comp_error}"


# ---------- Page ----------

st.markdown(
    """
    <style>
    .block-container { padding-top: 4rem !important; padding-bottom: 1rem !important;
                       padding-left: 0.4rem !important; padding-right: 0.4rem !important; }
    /* Green buttons (Start 10 statements, Home) */
    button[data-testid="stBaseButton-primary"], button[kind="primary"] {
        background-color: #16a34a !important; border-color: #16a34a !important; color: white !important; }
    button[data-testid="stBaseButton-primary"]:hover, button[kind="primary"]:hover {
        background-color: #15803d !important; border-color: #15803d !important; color: white !important; }
    button[data-testid="stBaseButton-primary"]:active, button[kind="primary"]:active {
        background-color: #166534 !important; border-color: #166534 !important; color: white !important; }
    /* Green selection icons (ticked checkbox, chosen radio circle) */
    label[data-baseweb="checkbox"]:has(input:checked) > span:first-child {
        background-color: #16a34a !important; border-color: #16a34a !important; }
    label[data-baseweb="radio"]:has(input:checked) > div:first-child {
        background-color: #16a34a !important; border-color: #16a34a !important; }
    .qg-title { font-size: 1.9rem; font-weight: 800; margin: 0 0 0.5rem 0;
                line-height: 1.4; text-align: center; }
    .qg-grad { display: inline-block; padding: 0 0.1em 0.12em 0.1em;
               background: linear-gradient(90deg, #f43f5e, #a855f7, #3b82f6);
               -webkit-background-clip: text; background-clip: text;
               -webkit-text-fill-color: transparent; color: transparent; }
    </style>
    """,
    unsafe_allow_html=True,
)

if quizgram is None:
    st.error("Could not set up the quiz component: " + _COMP_ERROR_TEXT)
    st.stop()

# ---- Login: the SAME login screen (profiles + PIN) as the main app ----
# The main app's login function is reused, so a profile created or chosen here also works there,
# and the other way round. Your login is remembered while you move between the pages.
if not st.session_state.get("current_user"):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        import app as main_app
    except Exception as e:
        st.error(f"Could not load the main app's login ({type(e).__name__}: {e}).")
        st.stop()
    main_app.get_current_user()   # shows the login screen and stops until you are logged in

user = st.session_state["current_user"]

st.markdown(
    '<div class="qg-title"><span class="qg-grad">Quiz-gram</span></div>',
    unsafe_allow_html=True,
)

# Bring results back from GitHub once per visit, if the local file went missing after a restart
if "_sc_restored" not in st.session_state:
    github_restore_if_needed()
    st.session_state["_sc_restored"] = True

pool = load_statements()
if not pool:
    st.warning("No statements found in the data folder yet, so these are 4 sample statements.")
    pool = SAMPLE_STATEMENTS

# ---- A round is running ----
rnd = st.session_state.get("sc_round")
if rnd:
    rid = re.sub(r"[^0-9A-Za-z]", "", rnd["round_id"])
    saved_key, status_key = f"sc_saved_{rid}", f"sc_status_{rid}"

    result = quizgram(
        items=rnd["items"], instant=rnd["instant"], show_tag=rnd["show_tag"], haptics=rnd["haptics"],
        round_id=rnd["round_id"], save_status=st.session_state.get(status_key, ""),
        bookmarked=list(load_bookmarks(user).keys()),
        key=f"qg_{rid}", default=None,
    )

    # Star taps: the quiz sends the full list of starred statements of this round
    if isinstance(result, dict) and result.get("type") == "bookmarks":
        round_ids = [s["id"] for s in rnd["items"]]
        starred = {x for x in result.get("ids", []) if x in round_ids}
        if user:
            if sync_round_bookmarks(user, round_ids, starred):
                st.session_state["sc_bm_dirty"] = True   # needs a GitHub backup later
        else:
            st.caption("⚠️ Bookmarks are not saved. Open the main app and choose your profile first.")

    # Auto-save: runs once, right after you tap "Show my result"
    if (isinstance(result, dict) and result.get("round_id") == rnd["round_id"]
            and not st.session_state.get(saved_key)):
        code = str(result.get("code", "")).strip().upper()
        if not user:
            status = "⚠️ Not saved. Open the main app and choose your profile first."
        elif not valid_code(code, rnd["items"]):
            status = "⚠️ Could not read the result. Use the backup code below the quiz."
        else:
            score = save_round(user, rnd["round_id"], rnd["items"], code)
            ok, msg = github_backup()
            if ok:
                status = f"✅ Saved ({score}/{len(rnd['items'])}) and backed up to GitHub."
            else:
                status = f"✅ Saved ({score}/{len(rnd['items'])}). ⚠️ GitHub backup: {msg}"
        st.session_state[saved_key] = True
        st.session_state[status_key] = status
        st.rerun()

    if st.session_state.get(status_key):
        st.caption(st.session_state[status_key])

    if not st.session_state.get(saved_key):
        with st.expander("Auto-save not working? Paste the backup code"):
            manual = st.text_input("Backup code from the last card", key=f"sc_manual_{rid}", max_chars=50).strip().upper()
            if st.button("Save with code", key=f"sc_manual_btn_{rid}", use_container_width=True):
                if not user:
                    st.error("Open the main app and choose your profile first.")
                elif not valid_code(manual, rnd["items"]):
                    st.error(f"The code should be {len(rnd['items'])} letters (T, F or S).")
                else:
                    score = save_round(user, rnd["round_id"], rnd["items"], manual)
                    ok, msg = github_backup()
                    st.session_state[saved_key] = True
                    st.session_state[status_key] = (
                        f"✅ Saved ({score}/{len(rnd['items'])})" + (" and backed up." if ok else f". ⚠️ GitHub backup: {msg}")
                    )
                    st.rerun()

    if st.button("🏠 Home", type="primary", use_container_width=True):
        if st.session_state.pop("sc_bm_dirty", False):
            github_backup()   # make sure new bookmarks are backed up too
        st.session_state.pop("sc_round", None)   # no round running = Quiz-gram home (start screen)
        st.rerun()
    st.stop()

# ---- Setup screen ----
counts = {}
for s in pool:
    counts[s["topic"]] = counts.get(s["topic"], 0) + 1
topics = sorted(counts)

mode = st.radio("Topics", ["One topic", "Mixed topics"], horizontal=True)
if mode == "One topic":
    chosen_topics = [st.selectbox("Topic", topics, format_func=lambda t: f"{t} ({counts[t]})")]
else:
    picked = st.multiselect("Pick topics (leave empty for all)", topics, format_func=lambda t: f"{t} ({counts[t]})")
    chosen_topics = picked or topics

adaptive = st.checkbox(
    "Prioritize wrong and not-yet-attempted statements (adaptive)", value=True,
    help="Reserves part of each round for statements you got wrong earlier, and fills the rest with "
         "statements you have not seen yet, favouring your weaker topics. Turn off for plain mixing.",
)
haptics = True   # vibration on a wrong answer is always on (works on most Android phones)

selected_pool = [s for s in pool if s["topic"] in chosen_topics]
available = len(selected_pool)
history = load_history(user)

if available:
    if adaptive and history:
        n_new, n_rev, n_mas, _ = split_pool(selected_pool, history)
        st.caption(f"{available} statements: {len(n_new)} new · {len(n_rev)} to revise · {len(n_mas)} answered correctly.")
        acc = last_round_accuracy(history)
        if acc is not None:
            st.caption(f"📊 Your last round: {round(acc * 100)}%. About {round(acc * 100)}% of this round is reserved for revision (if enough wrong ones exist).")
    else:
        st.caption(f"{available} statements available in your selection.")
    if adaptive and not user:
        st.caption("ℹ️ Open the main app and choose your profile, so your history can guide each round.")
else:
    st.caption("No statements in your selection.")

if st.button("▶️ Start 10 statements", type="primary", use_container_width=True, disabled=available == 0):
    if adaptive:
        items = pick_adaptive(selected_pool, history, 10)
    else:
        items = pick_balanced(pool, chosen_topics, 10)
    st.session_state.sc_round = {
        "items": items,
        "instant": True,
        "show_tag": True,
        "haptics": haptics,
        "round_id": datetime.now().isoformat(),
    }
    st.rerun()

# ---- My bookmarked statements ----
my_bookmarks = load_bookmarks(user)
if my_bookmarks:
    by_id = {s["id"]: s for s in pool}
    with st.expander(f"⭐ My bookmarked statements ({len(my_bookmarks)})"):
        for sid in sorted(my_bookmarks, key=lambda k: my_bookmarks[k], reverse=True):
            s = by_id.get(sid)
            with st.container(border=True):
                if s:
                    st.caption(s["topic"])
                    st.write(s["text"])
                    st.markdown(("✅ **RIGHT**" if s["is_true"] else "❌ **WRONG**")
                                + (f" — {s['why']}" if s.get("why") else ""))
                else:
                    st.caption(f"{sid} — this statement is no longer in the data files.")
                if st.button("Remove bookmark", key=f"sc_rm_{sid}", use_container_width=True):
                    remove_bookmark(user, sid)
                    github_backup()
                    st.rerun()
