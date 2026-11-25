// 選擇權拼圖：學生身分、成績同步、本輪最高紀錄。
// 由 options-puzzle_2.html / options-puzzle-en_2.html 在主程式之前載入。
//   繼續上次進度：每過一關自動存。
//   重新挑戰：從零開始，按「結束並儲存」後，本輪成績改成這一次的成績；本輪最高由伺服器保留較高的。
(function () {
  const KEY = 'optpuzzle-session';
  let sess = null;
  try { sess = JSON.parse(sessionStorage.getItem(KEY) || 'null'); } catch (e) {}
  if (!sess || !sess.sid) { location.replace('/options-puzzle'); return; }

  const EN = (document.documentElement.lang || '').startsWith('en');
  const T = EN ? {
    cont: 'Continuing progress', replay: 'Replay (fresh start)', high: 'Session best',
    saving: 'Saving…', saved: 'Saved ✓', saveFail: 'Save failed — will retry on next solve',
    notSaved: 'Not saved yet', finish: 'Finish & save', done: 'Done', logout: 'Switch user',
    mTitle: 'Save this attempt?', mHigh: 'Session best', mNow: 'This attempt',
    save: 'Save and finish', back: 'Keep playing', discard: 'Leave without saving',
    mNote: 'Saving makes this attempt your score for this session, even if it is lower. Your session best is kept either way.',
    resSaved: 'Saved. This session\'s score:', toEntry: 'Back to start',
    newHigh: (p, n) => `🎉 New session best! ${p} → ${n}`,
    passHigh: h => `🎉 You have passed your session best (${h}). Press "Finish & save" to record it.`,
    leave: 'This attempt has not been saved yet. Leave anyway?',
  } : {
    cont: '繼續上次進度', replay: '重新挑戰（從零開始）', high: '本輪最高',
    saving: '儲存中…', saved: '已儲存 ✓', saveFail: '儲存失敗，下次過關會再試',
    notSaved: '本次尚未儲存', finish: '結束並儲存', done: '完成', logout: '換人 / 登出',
    mTitle: '要儲存這次的成績嗎？', mHigh: '本輪最高', mNow: '本次成績',
    save: '儲存並結束', back: '繼續玩', discard: '不儲存，直接離開',
    mNote: '儲存後，本輪成績會改成這一次的成績（即使比較低）；本輪最高會保留比較高的那次。',
    resSaved: '已儲存，本輪成績', toEntry: '回到入口',
    newHigh: (p, n) => `🎉 刷新本輪最高紀錄！${p} → ${n}`,
    passHigh: h => `🎉 已經超過本輪最高（${h}）！按「結束並儲存」就會記成新紀錄。`,
    leave: '本次成績尚未儲存，確定要離開嗎？',
  };

  const replay = sess.mode === 'replay' && !!sess.record;
  window.OPT_INIT_BEST = replay ? {} : ((sess.record && sess.record.best) || {});

  let cur = { best: Object.assign({}, window.OPT_INIT_BEST), score: 0, max: (sess.record && sess.record.max_score) || 0, pts: [] };
  if (!replay && sess.record) cur.score = sess.record.score || 0;
  let dirty = false, finished = false, cheered = false;
  const highOf = rec => rec ? (rec.high_score != null ? rec.high_score : rec.score) || 0 : null;

  function post(body) {
    return fetch('/api/optpuzzle/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ dept: sess.dept, sid: sess.sid, name: sess.name, lang: EN ? 'en' : 'zh', round: (sess.round && sess.round.id) || '' }, body)),
    }).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });
  }

  function storeRecord(rec) {
    sess.record = rec;
    try { sessionStorage.setItem(KEY, JSON.stringify(sess)); } catch (e) {}
    render();
  }

  // 每次遊玩只跳一次大的提示，之後成績列上的最高分會繼續更新
  function cheer(text) {
    if (cheered) return;
    cheered = true;
    const t = document.getElementById('opsToast'); if (!t) return;
    t.textContent = text; t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 5000);
  }

  window.optOnScore = function (best, total, max, pts) {
    cur = { best: Object.assign({}, best), score: total, max: max, pts: pts || [] };
    render();
    if (replay) {
      dirty = true; setStatus(T.notSaved);
      const h = highOf(sess.record);
      if (h > 0 && total > h) cheer(T.passHigh(h));
      return;
    }
    setStatus(T.saving);
    post({ best: cur.best, pts: cur.pts, max_score: max, mode: 'auto' })
      .then(res => {
        storeRecord(res.record); setStatus(T.saved);
        if (res.new_high && res.prev_high > 0) cheer(T.newHigh(res.prev_high, res.record.high_score));
      })
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
  #opsToast{position:fixed;left:50%;top:16px;transform:translate(-50%,-140%);background:#f0b429;color:#1f2430;font-weight:700;font-size:15px;padding:10px 18px;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.25);z-index:1001;transition:transform .35s ease;max-width:calc(100% - 32px);text-align:center}
  #opsToast.show{transform:translate(-50%,0)}
  #opsModal{position:fixed;inset:0;background:rgba(20,22,30,.55);display:none;align-items:center;justify-content:center;z-index:1000;padding:16px}
  #opsModal.show{display:flex}
  #opsModal .box{background:#fffdf8;color:#1f2430;border-radius:16px;padding:22px;max-width:420px;width:100%;box-shadow:0 20px 50px rgba(0,0,0,.3)}
  #opsModal h3{margin:0 0 14px;font-size:18px}
  #opsModal .cmp{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}
  #opsModal .cmp div{border:1px solid #e3dccd;border-radius:12px;padding:10px;text-align:center}
  #opsModal .cmp .k{font-size:12px;color:#6b6f7a}
  #opsModal .cmp .v{font-size:24px;font-weight:900}
  #opsModal .hi{background:#fff6dd;border:1px solid #f0dfa6;border-radius:10px;padding:8px 12px;font-weight:700;margin:0 0 12px}
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
    bar.querySelector('.mode').textContent = replay ? T.replay : T.cont;
    const o = bar.querySelector('.orig'), h = highOf(sess.record);
    o.style.display = h == null ? 'none' : '';
    if (h != null) o.textContent = `🏆 ${T.high} ${h} / ${maxScore()}`;
  }

  function goEntry() { finished = true; location.href = '/options-puzzle'; }
  // 遊戲頁最後一關的「完成」按鈕也呼叫這個
  window.optFinish = () => replay ? openModal() : goEntry();

  function openModal() {
    const mx = maxScore(), h = highOf(sess.record);
    const m = document.getElementById('opsModal');
    m.querySelector('.box').innerHTML = `
      <h3>${T.mTitle}</h3>
      <div class="cmp">
        <div><div class="k">${T.mHigh}</div><div class="v">${h}</div><div class="k">/ ${mx}</div></div>
        <div><div class="k">${T.mNow}</div><div class="v">${cur.score}</div><div class="k">/ ${mx}</div></div>
      </div>
      ${cur.score > h ? `<p class="hi">${esc(T.newHigh(h, cur.score))}</p>` : ''}
      <p class="note">${T.mNote}</p>
      <div class="acts">
        <button class="pri" data-a="save">${T.save}</button>
        <button data-a="back">${T.back}</button>
        <button data-a="discard">${T.discard}</button>
      </div>`;
    m.classList.add('show');
    m.querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
      const a = b.dataset.a;
      if (a === 'back') { m.classList.remove('show'); return; }
      if (a === 'discard') { if (!dirty || confirm(T.leave)) goEntry(); return; }
      m.querySelectorAll('button').forEach(x => x.disabled = true);
      post({ best: cur.best, pts: cur.pts, max_score: mx, mode: 'replay' })
        .then(res => {
          storeRecord(res.record); finished = true; dirty = false;
          const r = res.record;
          m.querySelector('.box').innerHTML = `
            <h3>${T.resSaved} ${r.score} / ${r.max_score || mx}</h3>
            ${res.new_high ? `<p class="hi">${esc(T.newHigh(res.prev_high, r.high_score))}</p>` : `<p class="note">🏆 ${T.high} ${r.high_score} / ${r.max_score || mx}</p>`}
            <div class="acts"><button class="pri" data-a="entry">${T.toEntry}</button></div>`;
          m.querySelector('[data-a=entry]').addEventListener('click', goEntry);
        })
        .catch(() => { m.querySelectorAll('button').forEach(x => x.disabled = false); alert(T.saveFail); });
    }));
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.head.appendChild(el(`<style>${css}</style>`));
    const bar = el(`<div id="opsBar">
      <div class="who">${sess.round ? '📋 ' + esc(sess.round.name) + '　' : ''}👤 ${esc(sess.dept)}　<b>${esc(sess.sid)}</b>　${esc(sess.name)}
        <span class="pill mode"></span><span class="pill orig"></span><span class="st" id="opsSt"></span></div>
      <div><button class="pri" id="opsFinish">${replay ? T.finish : T.done}</button><button class="sec" id="opsOut">${T.logout}</button></div>
    </div>`);
    document.body.insertBefore(bar, document.body.firstChild);
    document.body.appendChild(el(`<div id="opsToast"></div>`));
    document.body.appendChild(el(`<div id="opsModal"><div class="box"></div></div>`));
    document.getElementById('opsFinish').addEventListener('click', window.optFinish);
    document.getElementById('opsOut').addEventListener('click', () => {
      if (replay && dirty && !confirm(T.leave)) return;
      finished = true;
      try { sessionStorage.removeItem(KEY); } catch (e) {}
      location.href = '/options-puzzle';
    });
    render();
  });
})();
