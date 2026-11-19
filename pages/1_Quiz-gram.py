"""
Quiz-gram -- 10 statements, one at a time, Instagram-style up/down scrolling.
Put this file at:  pages/1_Quiz-gram.py  (next to app.py).
"""

import json
import os
import random
import re
import tempfile
from datetime import datetime
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

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


def pick_round(pool, topics, n=10):
    """Balanced pick: takes statements from each chosen topic in turn."""
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


# ---------- The scrolling quiz (HTML + JavaScript) ----------

HTML_TEMPLATE = r"""
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; overflow: hidden; font-family: -apple-system, "Segoe UI", Roboto, sans-serif; color: #0f172a; }
  body { position: relative; }
  .feed { height: 100vh; overflow-y: scroll; scroll-snap-type: y mandatory; scrollbar-width: none; }
  .feed::-webkit-scrollbar { display: none; }

  /* Instagram-story style progress bars */
  .stories { position: absolute; top: 8px; left: 14px; right: 14px; display: flex; gap: 4px; z-index: 5; }
  .seg { flex: 1; height: 4px; border-radius: 3px; background: rgba(15, 23, 42, 0.18); transition: all .2s; }
  .seg.cur { height: 6px; margin-top: -1px; background: rgba(15, 23, 42, 0.45); }
  .seg.done { background: #3b82f6; }
  .seg.ok { background: #22c55e; }
  .seg.no { background: #ef4444; }

  .card { height: 100vh; scroll-snap-align: start; display: flex; flex-direction: column;
          padding: 26px 14px 12px 14px; }
  .g0 { background: linear-gradient(160deg, #fdf2f8, #eef2ff); }
  .g1 { background: linear-gradient(160deg, #ecfeff, #f0fdf4); }
  .g2 { background: linear-gradient(160deg, #fff7ed, #fef2f2); }
  .g3 { background: linear-gradient(160deg, #f5f3ff, #eff6ff); }

  .meta { display: flex; align-items: center; justify-content: space-between; margin: 6px 4px 10px 4px; }
  .count { font-weight: 700; font-size: 14px; color: #475569; }
  .tag { background: white; color: #7c3aed; border-radius: 999px; padding: 4px 12px;
         font-size: 12px; font-weight: 600; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }

  .body { flex: 1; overflow-y: auto; min-height: 0; scrollbar-width: none; }
  .body::-webkit-scrollbar { display: none; }
  .post { background: white; border-radius: 18px; padding: 18px; box-shadow: 0 4px 16px rgba(15, 23, 42, 0.08); }
  .text { font-size: 18px; line-height: 1.55; }

  .fb { margin-top: 12px; padding: 12px 14px; border-radius: 14px; display: none; line-height: 1.5; font-size: 15px; }
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
  .hint { text-align: center; color: #94a3b8; font-size: 12px; margin-top: 8px; }

  .last .body { display: block; }
  .copy { margin-top: 8px; padding: 10px 14px; font-size: 15px; border-radius: 10px;
          border: 1px solid #86efac; background: white; color: #166534; cursor: pointer; }
</style>
<div class="stories" id="stories"></div>
<div class="feed" id="feed"></div>
<script>
const data = __DATA__;
const INSTANT = __INSTANT__;
const SHOW_TAG = __SHOWTAG__;
const feed = document.getElementById('feed');
const storiesBar = document.getElementById('stories');
const choices = new Array(data.length).fill(null);

// Try to make the quiz fill the phone screen (falls back to the default height if blocked)
try {
  const fe = window.frameElement;
  const ph = window.parent.innerHeight;
  const top = fe.getBoundingClientRect().top;
  const h = Math.min(780, Math.max(480, ph - Math.max(top, 110) - 8));
  fe.style.height = h + 'px';
  fe.height = h;
} catch (e) {}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

const segs = data.map(() => {
  const s = document.createElement('div');
  s.className = 'seg';
  storiesBar.appendChild(s);
  return s;
});

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

data.forEach((s, i) => {
  const card = document.createElement('div');
  card.className = 'card g' + (i % 4);
  card.innerHTML =
    '<div class="meta"><span class="count">' + (i + 1) + ' / ' + data.length + '</span>' +
    (SHOW_TAG ? '<span class="tag">' + esc(s.topic) + '</span>' : '<span></span>') + '</div>' +
    '<div class="body"><div class="post"><div class="text">' + esc(s.text) + '</div></div>' +
    '<div class="fb"></div></div>' +
    '<div class="btns"><button class="r">✅ Right</button><button class="w">❌ Wrong</button></div>' +
    '<div class="hint">⬆️ Swipe up for next</div>';
  const fb = card.querySelector('.fb');
  const btns = card.querySelectorAll('button');
  const rb = btns[0], wb = btns[1];

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
last.className = 'card last g1';
last.innerHTML =
  '<div class="meta"><span class="count">🏁 Finish</span><span></span></div>' +
  '<div class="body"><div class="post"><div class="text">All done! Tap the button to see how you did.</div></div>' +
  '<div class="fb" id="result"></div></div>' +
  '<div class="btns"><button class="show" id="show">Show my result</button></div>';
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
  h += '<hr>💾 To save this round, copy this code and paste it in the box below the quiz:<br>' +
       '<b style="font-size:20px; letter-spacing:2px;">' + code + '</b><br>' +
       '<button class="copy" id="copy">📋 Copy code</button>';
  const res = document.getElementById('result');
  res.className = 'fb good';
  res.innerHTML = h;
  document.getElementById('copy').onclick = async function () {
    const btn = this;
    let ok = false;
    try { await navigator.clipboard.writeText(code); ok = true; }
    catch (e) {
      try {
        const ta = document.createElement('textarea');
        ta.value = code; ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.focus(); ta.select();
        ok = document.execCommand('copy'); document.body.removeChild(ta);
      } catch (e2) { ok = false; }
    }
    btn.textContent = ok ? '✅ Copied!' : '⚠️ Copy failed, type the code by hand';
  };
};
</script>
"""


def render_quiz(rnd):
    payload = json.dumps(rnd["items"], ensure_ascii=False).replace("</", "<\\/")
    html = (HTML_TEMPLATE
            .replace("__DATA__", payload)
            .replace("__INSTANT__", "true" if rnd["instant"] else "false")
            .replace("__SHOWTAG__", "true" if rnd["show_tag"] else "false"))
    components.html(html, height=640, scrolling=False)


# ---------- Page ----------

# Less empty space at the top + a compact gradient title
st.markdown(
    """
    <style>
    .block-container { padding-top: 2.6rem !important; padding-bottom: 1rem !important; }
    .qg-title { font-size: 1.8rem; font-weight: 800; margin: 0 0 0.3rem 0; line-height: 1.2; }
    .qg-grad { background: linear-gradient(90deg, #f43f5e, #a855f7, #3b82f6);
               -webkit-background-clip: text; background-clip: text;
               -webkit-text-fill-color: transparent; color: transparent; }
    </style>
    <div class="qg-title">📸 <span class="qg-grad">Quiz-gram</span></div>
    """,
    unsafe_allow_html=True,
)

user = st.session_state.get("current_user") or st.query_params.get("user", "")

pool = load_statements()
if not pool:
    st.warning("No statements found in the data folder yet, so these are 4 sample statements.")
    pool = SAMPLE_STATEMENTS

# ---- A round is running ----
rnd = st.session_state.get("sc_round")
if rnd:
    render_quiz(rnd)

    st.markdown("**💾 Save this round**")
    if not user:
        st.info("Open the main app and choose your profile first, then come back here, to save results.")
    elif st.session_state.get(f"sc_saved_{rnd['round_id']}"):
        st.success("Saved ✅")
    else:
        code = st.text_input(
            "Paste your result code here (from the last card)",
            key=f"sc_code_{rnd['round_id']}", max_chars=50,
        ).strip().upper()
        if st.button("Save result", use_container_width=True):
            if not re.fullmatch(r"[TFS]+", code) or len(code) != len(rnd["items"]):
                st.error(f"The code should be {len(rnd['items'])} letters (T, F or S). Copy it again from the last card.")
            else:
                score = save_round(user, rnd["round_id"], rnd["items"], code)
                st.session_state[f"sc_saved_{rnd['round_id']}"] = True
                st.success(f"Saved! Score: {score} / {len(rnd['items'])}")

    if st.button("🔄 New round", type="primary", use_container_width=True):
        st.session_state.pop("sc_round", None)
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

feedback = st.radio("Feedback", ["Instant (after each tap)", "Only at the end"], horizontal=True)
show_tag = st.checkbox("Show topic tag on each statement", value=(mode == "One topic"),
                       help="Turn off for a harder, more exam-like test.")

available = sum(counts[t] for t in chosen_topics)
st.caption(f"{available} statements available in your selection.")

if st.button("▶️ Start 10 statements", type="primary", use_container_width=True, disabled=available == 0):
    items = pick_round(pool, chosen_topics, 10)
    st.session_state.sc_round = {
        "items": items,
        "instant": feedback.startswith("Instant"),
        "show_tag": show_tag,
        "round_id": datetime.now().isoformat(),
    }
    st.rerun()
