// 選擇權拼圖：學生身分、成績同步、重玩後的覆蓋選擇。
// 由 options-puzzle_2.html / options-puzzle-en_2.html 在主程式之前載入。
(function () {
  const KEY = 'optpuzzle-session';
  let sess = null;
  try { sess = JSON.parse(sessionStorage.getItem(KEY) || 'null'); } catch (e) {}
  if (!sess || !sess.sid) { location.replace('/options-puzzle'); return; }

  const EN = (document.documentElement.lang || '').startsWith('en');
  const T = EN ? {
    cont: 'Continuing progress', replay: 'Replay (fresh start)', orig: 'Saved score',
    saving: 'Saving…', saved: 'Saved ✓', saveFail: 'Save failed — will retry on next solve',
    notSaved: 'Not saved yet', finish: 'Finish & save', done: 'Done', logout: 'Switch user',
    mTitle: 'Save this attempt?', mOrig: 'Saved score', mNow: 'This attempt',
    overwrite: 'Overwrite saved score', keep: 'Keep saved score', back: 'Keep playing',
    mNote: 'Overwriting replaces your saved score with this attempt, even if it is lower.',
    resOver: 'Saved. Your score is now', resKeep: 'Kept your saved score:', toEntry: 'Back to start',
    leave: 'This attempt has not been saved yet. Leave anyway?',
  } : {
    cont: '繼續上次進度', replay: '重新挑戰（從零開始）', orig: '原成績',
    saving: '儲存中…', saved: '已儲存 ✓', saveFail: '儲存失敗，下次過關會再試',
    notSaved: '本次尚未儲存', finish: '結束並儲存', done: '完成', logout: '換人 / 登出',
    mTitle: '要儲存這次的成績嗎？', mOrig: '原成績', mNow: '本次成績',
    overwrite: '覆蓋原成績', keep: '保留原成績', back: '繼續玩',
    mNote: '覆蓋會以本次成績取代原成績（即使本次比較低）。',
    resOver: '已儲存，目前成績', resKeep: '已保留原成績：', toEntry: '回到入口',
    leave: '本次成績尚未儲存，確定要離開嗎？',
  };

  const replay = sess.mode === 'replay' && !!sess.record;
  window.OPT_INIT_BEST = replay ? {} : ((sess.record && sess.record.best) || {});

  let cur = { best: Object.assign({}, window.OPT_INIT_BEST), score: 0, max: (sess.record && sess.record.max_score) || 0, pts: [] };
  if (!replay && sess.record) cur.score = sess.record.score || 0;
  let dirty = false, finished = false;

  function post(body) {
    return fetch('/api/optpuzzle/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ dept: sess.dept, sid: sess.sid, name: sess.name, lang: EN ? 'en' : 'zh' }, body)),
    }).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });
  }

  function storeRecord(rec) {
    sess.record = rec;
    try { sessionStorage.setItem(KEY, JSON.stringify(sess)); } catch (e) {}
  }

  window.optOnScore = function (best, total, max, pts) {
    cur = { best: Object.assign({}, best), score: total, max: max, pts: pts || [] };
    render();
    if (replay) { dirty = true; setStatus(T.notSaved); return; }
    setStatus(T.saving);
    post({ best: cur.best, pts: cur.pts, max_score: max, mode: 'auto' })
      .then(res => { storeRecord(res.record); setStatus(T.saved); })
      .catch(() => setStatus(T.saveFail));
  };

  window.addEventListener('beforeunload', e => {
    if (replay && dirty && !finished) { e.preventDefault(); e.returnValue = T.leave; return T.leave; }
  });

  // ── UI ──
  const css = `
  #opsBar{background:#1f2430;color:#f6f1e7;font-size:13px;padding:8px 16px;display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;justify-content:space-between}
  #opsBar .who b{font-weight:700}
  #opsBar .pill{background:rgba(255,255,255,.12);border-radius:999px;padding:2px 10px;margin-left:6px}
  #opsBar .st{opacity:.75;margin-left:6px}
  #opsBar button{font:inherit;border:0;border-radius:999px;padding:5px 13px;cursor:pointer;margin-left:6px}
  #opsBar .pri{background:#f0b429;color:#1f2430;font-weight:700}
  #opsBar .sec{background:transparent;color:#f6f1e7;border:1px solid rgba(255,255,255,.3)}
  #opsModal{position:fixed;inset:0;background:rgba(20,22,30,.55);display:none;align-items:center;justify-content:center;z-index:1000;padding:16px}
  #opsModal.show{display:flex}
  #opsModal .box{background:#fffdf8;color:#1f2430;border-radius:16px;padding:22px;max-width:420px;width:100%;box-shadow:0 20px 50px rgba(0,0,0,.3)}
  #opsModal h3{margin:0 0 14px;font-size:18px}
  #opsModal .cmp{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}
  #opsModal .cmp div{border:1px solid #e3dccd;border-radius:12px;padding:10px;text-align:center}
  #opsModal .cmp .k{font-size:12px;color:#6b6f7a}
  #opsModal .cmp .v{font-size:24px;font-weight:900}
  #opsModal .note{font-size:12px;color:#6b6f7a;margin:0 0 14px}
  #opsModal .acts{display:flex;flex-direction:column;gap:8px}
  #opsModal button{font:inherit;border-radius:10px;padding:10px;cursor:pointer;border:1.5px solid #e3dccd;background:#fff}
  #opsModal button.pri{background:#1f2430;color:#fff;border-color:#1f2430;font-weight:700}`;

  function el(html) { const d = document.createElement('div'); d.innerHTML = html.trim(); return d.firstChild; }
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  function setStatus(t) { const s = document.getElementById('opsSt'); if (s) s.textContent = t; }

  function maxScore() {
    try { if (typeof MAX_TOTAL !== 'undefined') return MAX_TOTAL; } catch (e) {}
    return cur.max || 0;
  }

  function render() {
    const bar = document.getElementById('opsBar'); if (!bar) return;
    const rec = sess.record, mx = maxScore();
    bar.querySelector('.mode').textContent = replay ? T.replay : T.cont;
    const o = bar.querySelector('.orig');
    o.style.display = replay ? '' : 'none';
    if (replay) o.textContent = `${T.orig} ${rec.score} / ${rec.max_score || mx}`;
  }

  function goEntry() { finished = true; location.href = '/options-puzzle'; }

  function openModal() {
    const rec = sess.record, mx = maxScore();
    const m = document.getElementById('opsModal');
    m.querySelector('.box').innerHTML = `
      <h3>${T.mTitle}</h3>
      <div class="cmp">
        <div><div class="k">${T.mOrig}</div><div class="v">${rec.score}</div><div class="k">/ ${rec.max_score || mx}</div></div>
        <div><div class="k">${T.mNow}</div><div class="v">${cur.score}</div><div class="k">/ ${mx}</div></div>
      </div>
      <p class="note">${T.mNote}</p>
      <div class="acts">
        <button class="pri" data-a="overwrite">${T.overwrite}</button>
        <button data-a="keep">${T.keep}</button>
        <button data-a="back">${T.back}</button>
      </div>`;
    m.classList.add('show');
    m.querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
      const a = b.dataset.a;
      if (a === 'back') { m.classList.remove('show'); return; }
      m.querySelectorAll('button').forEach(x => x.disabled = true);
      post({ best: cur.best, pts: cur.pts, max_score: mx, mode: a })
        .then(res => {
          storeRecord(res.record); finished = true; dirty = false;
          m.querySelector('.box').innerHTML = `
            <h3>${a === 'overwrite' ? T.resOver : T.resKeep} ${res.record.score} / ${res.record.max_score || mx}</h3>
            <div class="acts"><button class="pri" data-a="entry">${T.toEntry}</button></div>`;
          m.querySelector('[data-a=entry]').addEventListener('click', goEntry);
        })
        .catch(() => { m.querySelectorAll('button').forEach(x => x.disabled = false); alert(T.saveFail); });
    }));
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.head.appendChild(el(`<style>${css}</style>`));
    const bar = el(`<div id="opsBar">
      <div class="who">👤 ${esc(sess.dept)}　<b>${esc(sess.sid)}</b>　${esc(sess.name)}
        <span class="pill mode"></span><span class="pill orig"></span><span class="st" id="opsSt"></span></div>
      <div><button class="pri" id="opsFinish">${replay ? T.finish : T.done}</button><button class="sec" id="opsOut">${T.logout}</button></div>
    </div>`);
    document.body.insertBefore(bar, document.body.firstChild);
    document.body.appendChild(el(`<div id="opsModal"><div class="box"></div></div>`));
    document.getElementById('opsFinish').addEventListener('click', () => replay ? openModal() : goEntry());
    document.getElementById('opsOut').addEventListener('click', () => {
      if (replay && dirty && !confirm(T.leave)) return;
      finished = true;
      try { sessionStorage.removeItem(KEY); } catch (e) {}
      location.href = '/options-puzzle';
    });
    render();
  });
})();
