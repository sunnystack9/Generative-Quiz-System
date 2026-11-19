"""
Scroll Quiz -- 10 statements, one at a time, Reels-style up/down scrolling.
Put this file at:  pages/1_scroll_quiz.py  (next to app.py).
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
    st.set_page_config(page_title="Scroll Quiz", layout="centered")
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
  body { margin: 0; font-family: sans-serif; color: #0f172a; }
  .feed { height: 100vh; overflow-y: scroll; scroll-snap-type: y mandatory; }
  .card { height: 100vh; scroll-snap-align: start; box-sizing: border-box;
          display: flex; flex-direction: column; justify-content: center;
          padding: 22px; background: #f8fafc; border-bottom: 1px solid #e2e8f0; }
  .card.last { justify-content: flex-start; overflow-y: auto; }
  .count { color: #64748b; font-size: 14px; margin-bottom: 8px; }
  .tag { display: inline-block; align-self: flex-start; background: #e0f2fe; color: #0369a1;
         border-radius: 999px; padding: 3px 12px; font-size: 13px; margin-bottom: 12px; }
  .text { font-size: 20px; line-height: 1.5; margin-bottom: 22px; }
  .btns { display: flex; gap: 12px; }
  .btns button { flex: 1; padding: 16px; font-size: 18px; border-radius: 12px;
                 border: 2px solid #cbd5e1; background: white; color: #0f172a; }
  .btns button.sel { border-color: #3b82f6; background: #eff6ff; }
  .fb { margin-top: 16px; padding: 12px; border-radius: 10px; display: none; line-height: 1.5; }
  .good { background: #dcfce7; color: #166534; display: block; }
  .bad { background: #fee2e2; color: #991b1b; display: block; }
  .note { background: #e2e8f0; color: #334155; display: block; }
  .hint { margin-top: 18px; text-align: center; color: #94a3b8; font-size: 13px; }
  .copy { margin-top: 8px; padding: 10px 14px; font-size: 15px; border-radius: 10px;
          border: 1px solid #86efac; background: white; color: #166534; }
</style>
<div class="feed" id="feed"></div>
<script>
const data = __DATA__;
const INSTANT = __INSTANT__;
const SHOW_TAG = __SHOWTAG__;
const feed = document.getElementById('feed');
const choices = new Array(data.length).fill(null);

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

data.forEach((s, i) => {
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML =
    '<div class="count">Statement ' + (i + 1) + ' of ' + data.length + '</div>' +
    (SHOW_TAG ? '<div class="tag">' + esc(s.topic) + '</div>' : '') +
    '<div class="text">' + esc(s.text) + '</div>' +
    '<div class="btns"><button class="r">✅ Right</button><button class="w">❌ Wrong</button></div>' +
    '<div class="fb"></div>' +
    '<div class="hint">⬆️ Scroll up for next</div>';
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
  }
  rb.onclick = () => pick(true);
  wb.onclick = () => pick(false);
  feed.appendChild(card);
});

const last = document.createElement('div');
last.className = 'card last';
last.innerHTML = '<div class="text">🏁 All done!</div>' +
  '<div class="btns"><button id="show">Show my result</button></div>' +
  '<div class="fb" id="result"></div>';
feed.appendChild(last);

document.getElementById('show').onclick = () => {
  let score = 0; const byTopic = {}; const wrong = [];
  data.forEach((s, i) => {
    const ok = (choices[i] === s.is_true);
    byTopic[s.topic] = byTopic[s.topic] || {c: 0, t: 0};
    byTopic[s.topic].t++;
    if (ok) { score++; byTopic[s.topic].c++; } else { wrong.push(i); }
  });
  const code = choices.map(c => c === true ? 'T' : (c === false ? 'F' : 'S')).join('');
  let h = '<b>🎯 Score: ' + score + ' / ' + data.length + '</b><br><br>';
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
    components.html(html, height=620, scrolling=False)


# ---------- Page ----------

st.title("📜 Scroll Quiz")

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
