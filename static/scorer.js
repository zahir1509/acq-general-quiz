(function () {
  'use strict';
  var W = window.WIZ, el = W.el, esc = W.esc;
  var token = localStorage.getItem('wiz_admin_token') || '';
  var F = null;                                   // latest scorer frame
  var D = { teams: null, questions: null, config: null, scoreboard: null };
  var seen = {};                                  // data versions already fetched
  var tab = 'run';
  var conn = null;
  var PHASES = ['LOBBY', 'PASS1', 'PASS2', 'PASS3', 'LOCKED', 'REVEAL'];
  var LABEL = { LOBBY: 'LOBBY', PASS1: 'PASS 1', PASS2: 'PASS 2', PASS3: 'PASS 3', LOCKED: 'LOCKED', REVEAL: 'REVEAL' };

  function A(method, url, body) { return W.request(method, url, body, token); }
  function act(url, body, msg) {
    return A('POST', url, body).then(function (r) { if (msg) W.toast(msg, 'ok'); return r; })
      .catch(function (e) { W.toast(e.message, 'err'); throw e; });
  }
  function later(fn, ms) { var t; return function () { clearTimeout(t); t = setTimeout(fn, ms); }; }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function num(v) { var n = parseFloat(v); return isNaN(n) ? null : n; }
  function pts(x) { return Math.round(x * 100) / 100; }

  // ----------------------------------------------------------------- login
  function login() {
    W.post('/api/admin/login', { pin: el('pin').value.trim() }).then(function (r) {
      token = r.token; localStorage.setItem('wiz_admin_token', token); boot();
    }).catch(function (e) { W.toast(e.message, 'err'); });
  }
  function logout() {
    token = ''; localStorage.removeItem('wiz_admin_token');
    if (conn) conn.stop();
    location.reload();
  }
  el('loginBtn').addEventListener('click', login);
  el('pin').addEventListener('keydown', function (e) { if (e.key === 'Enter') login(); });

  function boot() {
    el('loginView').classList.add('hidden');
    el('app').classList.remove('hidden');
    var base = '?t=' + encodeURIComponent(token);
    el('csvLink').href = '/export.csv' + base;
    el('mxLink').href = '/export-matrix.csv' + base;
    el('jsonLink').href = '/export.json' + base;
    el('cardsLink').href = '/cards' + base;
    el('lnkWelcome').href = '/welcome'; el('lnkScreen').href = '/screen'; el('lnkBoard').href = '/board';
    conn = new W.Connection({ role: 'scorer', token: token, onState: onFrame, onAuthFail: logout }).start();
    fetchTeams(); fetchQuestions(); fetchConfig();
  }

  // ------------------------------------------------------------------ tabs
  function showTab(name) {
    tab = name;
    $all('#nav button').forEach(function (b) { b.classList.toggle('on', b.getAttribute('data-tab') === name); });
    $all('main > section').forEach(function (s) { s.classList.toggle('hidden', s.id !== 'tab-' + name); });
    refreshActive(true);
    renderHealth();
  }
  el('nav').addEventListener('click', function (e) {
    var b = e.target.closest('button'); if (b) showTab(b.getAttribute('data-tab'));
  });
  document.addEventListener('click', function (e) {
    var j = e.target.closest('[data-jump]');
    if (j) { e.preventDefault(); showTab(j.getAttribute('data-jump')); }
  });

  // ---------------------------------------------------------------- frames
  function onFrame(f) {
    if (f.type === 'ping') return;
    F = f;
    renderHeader(); renderRun(); renderHealth();
    refreshActive(false);
  }

  function renderHeader() {
    var f = F;
    el('chipPhase').textContent = LABEL[f.phase] || f.phase;
    el('chipPhase').className = 'chip ' + (f.phase === 'LOBBY' ? 'amber' : f.phase === 'LOCKED' ? 'red' : 'green');
    var c = f.counts;
    el('chipTeams').textContent = c.online + ' online · ' + c.joined + '/' + c.teams + ' joined';
    var n = f.unreviewed.answers, b = el('navRv');
    b.textContent = n; b.classList.toggle('hidden', !n);
  }

  // Refetch only what the visible tab needs, and only when its data version moved.
  function refreshActive(force) {
    if (!F) return;
    var v = F.ver;
    if (tab === 'scores' && (force || seen.s !== v.s)) fetchScores();
    if (tab === 'review' && (force || seen.rv !== v.a + '.' + v.g + '.' + v.q)) fetchReview();
    if (tab === 'teams' && (force || seen.t !== v.t)) fetchTeams();
    if (tab === 'questions' && (force || seen.q !== v.q)) fetchQuestions();
    if (tab === 'setup' && (force || seen.c !== v.c)) fetchConfig();
    if (tab === 'log' && force) fetchLog();
    if (tab === 'run') {
      if (seen.t !== v.t) fetchTeams();
      if (seen.q !== v.q) fetchQuestions();
    }
    if (tab !== 'setup' && seen.c !== v.c) fetchConfig();
  }

  // ------------------------------------------------------------------- RUN
  var timerSecs = null, runKey = '', lastPhase = null;

  function stepper(f) {
    var cur = PHASES.indexOf(f.phase);
    el('stepper').innerHTML = PHASES.map(function (p, i) {
      return '<button data-a="phase" data-v="' + p + '" class="' + (i === cur ? 'on' : i < cur ? 'done' : '') + '">' +
        LABEL[p] + '</button>';
    }).join('');
  }

  function avOf(c) {
    return (c && c.images || []).filter(function (u) { return W.mediaType(u) !== 'image'; }).length;
  }

  function mediaBar(c) {
    if (!avOf(c)) return '';
    return '<div class="mediabar"><span class="small muted">PROJECTOR MEDIA:</span>' +
      '<button class="btn-sm btn-green" data-a="media" data-v="play">&#9654; PLAY</button>' +
      '<button class="btn-sm" data-a="media" data-v="pause">&#10074;&#10074; PAUSE</button>' +
      '<button class="btn-sm" data-a="media" data-v="restart">&#8634; RESTART</button>' +
      '<button class="btn-sm" data-a="media" data-v="seek" data-n="-10">&minus;10s</button>' +
      '<button class="btn-sm" data-a="media" data-v="seek" data-n="10">+10s</button>' +
      '<button class="btn-sm" data-a="media" data-v="volume" data-n="0.3">VOL LOW</button>' +
      '<button class="btn-sm" data-a="media" data-v="volume" data-n="1">VOL FULL</button>' +
      '<button class="btn-sm" data-a="media" data-v="mute">MUTE</button>' +
      '<button class="btn-sm" data-a="media" data-v="unmute">UNMUTE</button></div>';
  }

  function questionCard(f) {
    var c = f.current;
    if (!c) return '<p class="muted">No question selected.</p>';
    var imgs = (c.images || []).filter(function (u) { return W.mediaType(u) === 'image'; })
      .map(function (u) { return '<img src="' + esc(u) + '" alt="">'; }).join('');
    var av = (c.images || []).filter(function (u) { return W.mediaType(u) !== 'image'; }).length;
    return '<div class="qtext">' + esc(c.text) + '</div>' +
      (imgs ? '<div class="thumbs mb">' + imgs + '</div>' : '') +
      (av ? '<div class="small muted mb">' + av + ' audio/video file(s) on the projector</div>' : '') +
      '<div class="qans">Answer: <b>' + esc(c.official_answer || '—') + '</b>' +
      ((c.alt_answers || []).length ? ' <span class="muted">also: ' + c.alt_answers.map(esc).join(' · ') + '</span>' : '') +
      (c.notes ? '<div class="qnote">' + esc(c.notes) + '</div>' : '') + '</div>';
  }

  function timerBlock(f, label, defSecs) {
    if (timerSecs == null) timerSecs = defSecs;
    return '<div class="controls">' +
      '<div class="inl"><input id="tSecs" type="number" min="5" value="' + timerSecs + '"><span class="small muted">sec</span></div>' +
      '<button data-a="timer" data-v="start" class="btn-green">START ' + label + '</button>' +
      '<button data-a="timer" data-v="extend" data-n="15">+15s</button>' +
      '<button data-a="timer" data-v="extend" data-n="30">+30s</button>' +
      '<button data-a="timer" data-v="clear" class="btn-ghost">CLEAR</button></div>';
  }

  function runHtml(f) {
    var cfg = D.config || {}, c = f.counts, h = '';
    if (f.phase === 'LOBBY') {
      return '<h2>Lobby</h2><p><b>' + c.joined + '</b> of ' + c.teams + ' teams have joined, ' + c.online + ' online now.</p>' +
        '<p class="small muted mb">Put the welcome screen on the projector while teams join.</p>' +
        '<button data-a="phase" data-v="PASS1" class="btn-primary btn-xl btn-block">START PASS 1</button>';
    }
    if (f.phase === 'PASS1' || f.phase === 'PASS2') {
      var last = f.pos >= f.total;
      var nextBtn = last
        ? '<button data-a="phase" data-v="' + (f.phase === 'PASS1' ? 'PASS2' : 'PASS3') + '" class="btn-primary btn-xl">END PASS ' +
          f.phase.slice(4) + ' &rarr; ' + (f.phase === 'PASS1' ? 'PASS 2' : 'PASS 3') + '</button>'
        : '<button data-a="step" data-v="1" class="btn-primary btn-xl">NEXT &#9654;</button>';
      var opts = (D.questions || []).map(function (q) {
        return '<option value="' + q.number + '"' + (f.current && q.number === f.current.n ? ' selected' : '') + '>Q' + q.number + '</option>';
      }).join('');
      h += '<div class="qhead"><span class="chip blue">PASS ' + f.phase.slice(4) + '</span>' +
        '<span class="qbig">' + f.pos + '<span class="muted" style="font-size:20px"> / ' + f.total + '</span></span>' +
        '<span class="spacer"></span><span id="runTimer" class="bigtimer" style="font-size:30px"></span></div>' +
        questionCard(f) + mediaBar(f.current) +
        '<div class="controls"><button data-a="step" data-v="-1" class="btn-xl"' + (f.pos <= 1 ? ' disabled' : '') + '>&#9664; PREV</button>' +
        nextBtn + '<select id="jump" style="flex:0 0 110px">' + opts + '</select></div>' +
        timerBlock(f, 'TIMER', num(cfg.q_timer_seconds) || 30) +
        '<div class="small muted"><b id="runAnswered">' + c.answered + '</b> of ' + c.joined + ' joined teams have answered this question. ' +
        'The timer is a visual aid in passes 1 and 2; nothing is cut off.</div>';
      return h;
    }
    if (f.phase === 'PASS3') {
      if (!f.deadline) {
        return '<h2>Pass 3 — ready</h2><p class="small muted mb">Phones show &ldquo;about to begin&rdquo; and the projector shows the instructions. ' +
          'Start the timer when you are ready; teams can then open any question.</p>' +
          timerBlock(f, 'PASS 3 TIMER', num(cfg.pass3_seconds) || 120);
      }
      return '<h2>Pass 3 — running</h2><div id="runTimer" class="bigtimer"></div>' +
        '<div class="controls"><button data-a="timer" data-v="extend" data-n="30">+30s</button>' +
        '<button data-a="timer" data-v="extend" data-n="60">+60s</button>' +
        '<button data-a="phase" data-v="LOCKED" class="btn-red btn-xl">END NOW &amp; LOCK</button></div>' +
        '<div class="small muted">The server locks answers at 0:00. An edit typed before the lock that arrives up to 10 s late is still accepted.</div>';
    }
    if (f.phase === 'LOCKED') {
      return '<h2>Answers locked</h2><p class="small muted mb">Review answers while the room waits, or go straight to the reveal.</p>' +
        '<div class="controls"><button class="btn-primary btn-xl" data-jump="review">GO TO REVIEW (' + f.unreviewed.answers + ' to check)</button>' +
        '<button data-a="reopen" class="btn-xl">REOPEN PASS 3 (+60s)</button>' +
        '<button data-a="phase" data-v="REVEAL" class="btn-green btn-xl">START REVEAL</button></div>';
    }
    var qn = f.reveal_q, rq = (D.questions || []).filter(function (q) { return q.number === qn; })[0];
    return '<h2>Reveal</h2>' +
      (f.unreviewed.answers ? '<div class="warnbox">' + f.unreviewed.answers + ' answers are still unreviewed and count as 0.</div>' : '') +
      (rq ? '<div class="qtext">Q' + rq.number + '. ' + esc(rq.text) + '</div><div class="qans">Answer: <b>' + esc(rq.official_answer) + '</b></div>' : '') +
      '<div class="controls"><button data-a="reveal" data-v="prev" class="btn-xl">&#9664; PREV</button>' +
      '<button data-a="reveal" data-v="next" class="btn-primary btn-xl">NEXT &#9654;</button>' +
      '<button data-a="reveal" data-v="view:' + (f.reveal_view === 'list' ? 'single' : 'list') + '" class="btn-xl">' +
      (f.reveal_view === 'list' ? 'ONE AT A TIME' : 'LIST VIEW (6 per page)') + '</button></div>' +
      '<div class="small muted">Open the <a href="/board" target="_blank">scoreboard</a> on the projector when you are done.</div>';
  }

  function renderRun() {
    var f = F;
    var missing = !f.counts.teams ? 'No teams yet. ' : '';
    missing += !f.total ? 'No questions yet.' : '';
    el('noContent').classList.toggle('hidden', !missing || f.phase !== 'LOBBY');
    el('noContentMsg').textContent = missing;
    stepper(f);
    if (f.phase !== lastPhase) { lastPhase = f.phase; timerSecs = null; }
    var key = [f.phase, f.pos, f.reveal_q, f.reveal_view, f.deadline ? 1 : 0, f.total, f.current && f.current.n,
               f.unreviewed.answers > 0 ? 1 : 0, (D.questions || []).length, (D.config || {}).q_timer_seconds].join('|');
    if (key !== runKey && !(document.activeElement && document.activeElement.id === 'tSecs')) {
      runKey = key;
      el('runCtl').innerHTML = runHtml(f);
    }
    var ra = el('runAnswered'); if (ra) ra.textContent = f.counts.answered;
    tickTimer(); renderTiles(f);
  }

  function renderTiles(f) {
    var names = {};
    (D.teams || []).forEach(function (t) { names[t.number] = t.name; });
    var showAns = f.phase === 'PASS1' || f.phase === 'PASS2';
    el('legendAns').textContent = showAns ? 'filled = answered this question'
      : 'small number = questions answered';
    el('tilesSummary').textContent = f.counts.online + ' online';
    el('tiles').innerHTML = f.tiles.map(function (t) {
      var cls = 'tile' + (t[1] ? ' online' : t[4] ? ' joined' : '');
      if (showAns && t[2]) cls += ' ans';
      if (!showAns && f.total && t[3] >= f.total) cls += ' ans';
      return '<div class="' + cls + '" title="' + esc(names[t[0]] || '') + '">' + t[0] +
        (!showAns && f.phase !== 'LOBBY' ? '<small>' + t[3] + '</small>' : '') + '</div>';
    }).join('');
  }

  function tickTimer() {
    var t = el('runTimer');
    if (!t || !F) return;
    if (!F.deadline) { t.textContent = ''; return; }
    var left = F.deadline - W.now();
    t.textContent = W.mmss(left);
    t.className = t.className.replace(/\blow\b/, '').trim() + (left < 15 ? ' low' : '');
  }
  setInterval(tickTimer, 250);

  var PHASE_IDX = {};
  PHASES.forEach(function (p, i) { PHASE_IDX[p] = i; });
  function setPhase(p) {
    var from = PHASE_IDX[F.phase], to = PHASE_IDX[p];
    if (to < from && !confirm('Go BACK to ' + LABEL[p] + '?')) return;
    if (to - from > 1 && !confirm('Skip ahead to ' + LABEL[p] + '?')) return;
    if (F.phase === 'PASS3' && F.deadline && p === 'LOCKED' && !confirm('End Pass 3 now? Teams will be locked out.')) return;
    act('/api/admin/phase', { phase: p });
  }

  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-a]');
    if (!b) return;
    var a = b.getAttribute('data-a'), v = b.getAttribute('data-v'), n = b.getAttribute('data-n');
    if (a === 'phase') setPhase(v);
    else if (a === 'step') act('/api/admin/step', { delta: Number(v) });
    else if (a === 'timer') {
      var secs = el('tSecs') ? num(el('tSecs').value) : null;
      if (v === 'start') act('/api/admin/timer', { action: 'start', seconds: secs });
      else act('/api/admin/timer', { action: v, seconds: num(n) });
    } else if (a === 'media') act('/api/admin/media', { action: v, value: n == null ? 0 : Number(n) });
    else if (a === 'reveal') {
      if (v.indexOf('view:') === 0) act('/api/admin/reveal', { action: 'view', view: v.slice(5) });
      else act('/api/admin/reveal', { action: v });
    } else if (a === 'reopen') {
      act('/api/admin/phase', { phase: 'PASS3' }).then(function () {
        act('/api/admin/timer', { action: 'start', seconds: 60 });
      });
    }
  });
  document.addEventListener('change', function (e) {
    if (e.target.id === 'jump') act('/api/admin/step', { goto: Number(e.target.value) });
  });
  document.addEventListener('input', function (e) {
    if (e.target.id === 'tSecs') timerSecs = num(e.target.value);
  });

  el('genTeams').addEventListener('click', function () { generate(num(el('teamCountIn').value)); });
  el('genTeams2').addEventListener('click', function () { generate(num(el('genCount').value)); });
  function generate(count) {
    if (!count) return;
    var existing = (D.teams || []).length;
    if (existing && !confirm('This replaces the ' + existing + ' existing teams and clears all answers. Continue?')) return;
    act('/api/admin/teams/generate', { count: count, replace: !!existing }, 'Teams created').then(fetchTeams);
  }

  // ---------------------------------------------------------------- REVIEW
  var rv = { q: null, data: null, summary: [], focus: 0, visible: [] };

  function fetchSummary() {
    return A('GET', '/api/admin/review/summary').then(function (r) { rv.summary = r.summary; renderRvList(); });
  }
  function fetchReview() {
    var v = F.ver; seen.rv = v.a + '.' + v.g + '.' + v.q;
    fetchSummary().then(function () {
      if (rv.q == null && rv.summary.length) {
        var first = rv.summary.filter(function (s) { return s.unreviewed_clusters; })[0] || rv.summary[0];
        rv.q = first.number;
      }
      if (rv.q != null) loadReview(rv.q);
    });
  }
  function loadReview(n) {
    rv.q = n;
    return A('GET', '/api/admin/review?q=' + n).then(function (r) { rv.data = r; renderRv(); renderRvList(); });
  }

  function renderRvList() {
    var tot = 0, ans = 0;
    rv.summary.forEach(function (s) { tot += s.unreviewed_clusters; ans += s.unreviewed_answers; });
    el('rvProgress').textContent = tot ? tot + ' distinct answers (' + ans + ' team answers) still to review' : 'Everything reviewed ✓';
    el('rvList').innerHTML = rv.summary.map(function (s) {
      return '<div class="qrow' + (s.number === rv.q ? ' cur' : '') + '" data-q="' + s.number + '"><b>Q' + s.number + '</b>' +
        '<span class="muted small">' + s.answers + ' answers</span>' +
        '<span class="badge ' + (s.unreviewed_clusters ? 'todo' : 'done') + '">' +
        (s.unreviewed_clusters ? s.unreviewed_clusters + ' to review' : '✓') + '</span></div>';
    }).join('');
  }
  el('rvList').addEventListener('click', function (e) {
    var r = e.target.closest('.qrow'); if (r) { rv.focus = 0; loadReview(Number(r.getAttribute('data-q'))); }
  });
  el('rvNext').addEventListener('click', function () {
    var list = rv.summary, i = list.findIndex(function (s) { return s.number === rv.q; });
    for (var k = 1; k <= list.length; k++) {
      var s = list[(i + k) % list.length];
      if (s.unreviewed_clusters) { rv.focus = 0; return loadReview(s.number); }
    }
    W.toast('Nothing left to review', 'ok');
  });
  el('rvAllWrong').addEventListener('click', function () {
    if (!confirm('Mark EVERY unreviewed answer on EVERY question as wrong?')) return;
    act('/api/admin/grade/rest_wrong', { q: 'all' }, 'Done').then(fetchReview);
  });
  el('rvQWrong').addEventListener('click', function () {
    if (rv.q == null) return;
    act('/api/admin/grade/rest_wrong', { q: rv.q }, 'Marked wrong').then(function () { loadReview(rv.q); fetchSummary(); });
  });
  el('rvShowAuto').addEventListener('change', renderRv);
  el('rvFilter').addEventListener('input', renderRv);

  function clusterHtml(c, i, points) {
    var st = c.fraction === null ? '' : c.fraction >= 1 ? 'full' : c.fraction > 0 ? 'part' : 'zero';
    var variants = c.variants.length > 1
      ? ' <span class="muted small">(' + c.variants.map(function (v) { return esc(v[0]) + (v[1] > 1 ? '×' + v[1] : ''); }).join(', ') + ')</span>' : '';
    var meta = c.teams.map(function (t) { return 'T' + t; }).join(' ');
    var tag = c.fraction === null ? '' :
      '<span class="chip ' + (c.fraction >= 1 ? 'green' : c.fraction > 0 ? 'amber' : 'red') + '">' +
      (c.source === 'AUTO' ? 'auto ' : '') + pts(c.fraction * points) + ' pts</span>';
    var sug = c.suggest ? '<div class="sug">&#9888; ' + (c.suggest.kind === 'contains' ? 'contains' : 'close to') +
      ' &ldquo;' + esc(c.suggest.against) + '&rdquo; (' + Math.round(c.suggest.score * 100) + '%)</div>' : '';
    return '<div class="cl ' + st + (i === rv.focus ? ' sel' : '') + '" data-i="' + i + '" data-norm="' + esc(c.norm) +
      '" data-label="' + esc(c.label) + '"><div class="main"><div class="lab"><span class="count">×' + c.count + '</span>' +
      esc(c.label) + variants + '</div><div class="meta">' + meta + '</div>' + sug + '</div>' + tag +
      '<div class="btns"><button class="btn-green" data-g="1" title="Correct">&#10003;</button>' +
      '<button class="btn-amber" data-g="0.5" title="Half">&frac12;</button>' +
      '<button class="btn-red" data-g="0" title="Wrong">&#10007;</button>' +
      '<input data-custom type="number" step="0.25" min="-1" max="1" placeholder="0–1" title="Any fraction of the points">' +
      (c.fraction !== null ? '<button class="btn-ghost" data-g="clear" title="Clear decision">&#8634;</button>' : '') +
      '</div></div>';
  }

  function renderRv() {
    var d = rv.data; if (!d) return;
    var q = d.question, s = d.stats;
    var left = d.clusters.filter(function (c) { return c.fraction === null; }).length;
    el('rvHead').innerHTML = '<div class="qhead"><span class="qbig">Q' + q.number + '</span>' +
      '<span class="chip">' + q.points + ' pts</span>' +
      '<span class="chip ' + (left ? 'amber' : 'green') + '">' + left + ' to review</span></div>' +
      '<div class="qtext">' + esc(q.text) + '</div><div class="qans">Official: <b>' + esc(q.official_answer) + '</b>' +
      (q.alt_answers.length ? ' <span class="muted">also: ' + q.alt_answers.map(esc).join(' · ') + '</span>' : '') + '</div>';
    var showAuto = el('rvShowAuto').checked, filt = el('rvFilter').value.trim().toLowerCase();
    rv.visible = d.clusters.filter(function (c) {
      if (filt && c.label.toLowerCase().indexOf(filt) < 0) return false;
      return c.fraction === null || showAuto || !!filt;
    });
    if (rv.focus >= rv.visible.length) rv.focus = Math.max(0, rv.visible.length - 1);
    var hidden = d.clusters.length - rv.visible.length;
    el('rvClusters').innerHTML = rv.visible.map(function (c, i) { return clusterHtml(c, i, q.points); }).join('') ||
      '<div class="muted center" style="padding:20px">' + (d.clusters.length ? 'Nothing to review here ✓' : 'No answers yet') + '</div>';
    el('rvSilent').textContent = (hidden > 0 && !showAuto ? hidden + ' answers already decided (auto or by you) are hidden. ' : '') +
      (d.silent.length ? 'No answer from: ' + d.silent.map(function (n) { return 'T' + n; }).join(' ') : '');
    var sel = el('rvClusters').querySelector('.sel');
    if (sel && sel.scrollIntoView) sel.scrollIntoView({ block: 'nearest' });
  }

  function grade(c, fraction) {
    var body = { q: rv.q, norm: c.norm, label: c.label, fraction: fraction === null ? 'clear' : fraction };
    // optimistic: the row updates now, the server confirms
    c.fraction = fraction; c.source = fraction === null ? null : 'MANUAL';
    var hides = !el('rvShowAuto').checked && !el('rvFilter').value.trim();
    if (fraction !== null && !hides) {
      var nxt = rv.visible.findIndex(function (x, i) { return i > rv.focus && x.fraction === null; });
      if (nxt >= 0) rv.focus = nxt;
    }
    renderRv();
    act('/api/admin/grade', body).then(function () { refreshSummarySoon(); }).catch(function () { loadReview(rv.q); });
  }
  var refreshSummarySoon = later(fetchSummary, 500);

  el('rvClusters').addEventListener('click', function (e) {
    var row = e.target.closest('.cl'); if (!row) return;
    var c = rv.visible[Number(row.getAttribute('data-i'))];
    rv.focus = Number(row.getAttribute('data-i'));
    var b = e.target.closest('[data-g]');
    if (!b) return renderRv();
    var g = b.getAttribute('data-g');
    if (g === 'clear') grade(c, null);
    else grade(c, Number(g));
  });
  el('rvClusters').addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' || !e.target.hasAttribute('data-custom')) return;
    var row = e.target.closest('.cl'), v = num(e.target.value);
    if (v !== null) grade(rv.visible[Number(row.getAttribute('data-i'))], v);
  });
  document.addEventListener('keydown', function (e) {
    if (tab !== 'review' || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.ctrlKey || e.metaKey) return;
    var c = rv.visible[rv.focus], k = e.key.toLowerCase();
    if (k === 'arrowdown') { rv.focus = Math.min(rv.visible.length - 1, rv.focus + 1); renderRv(); e.preventDefault(); }
    else if (k === 'arrowup') { rv.focus = Math.max(0, rv.focus - 1); renderRv(); e.preventDefault(); }
    else if (c && k === 'c') grade(c, 1);
    else if (c && k === 'h') grade(c, 0.5);
    else if (c && k === 'w') grade(c, 0);
    else if (c && k === 'u') grade(c, null);
  });

  // ---------------------------------------------------------------- SCORES
  function fetchScores() {
    seen.s = F.ver.s;
    A('GET', '/api/admin/scoreboard').then(function (r) { D.scoreboard = r.scoreboard; renderScores(); });
  }
  function renderScores() {
    var f = el('scFilter').value.trim().toLowerCase();
    el('scBody').innerHTML = (D.scoreboard || []).filter(function (r) {
      return !f || r.name.toLowerCase().indexOf(f) >= 0 || String(r.number) === f;
    }).map(function (r) {
      return '<tr class="sbrow" data-id="' + r.id + '"><td class="num">' + r.rank + '</td><td><b>' + esc(r.name) +
        '</b> <span class="muted mono">T' + r.number + '</span></td><td class="right num"><b>' + r.score +
        '</b></td><td class="right num">' + r.answered + '</td><td class="right num">' + r.correct + '</td></tr>';
    }).join('');
  }
  el('scFilter').addEventListener('input', renderScores);
  el('scBody').addEventListener('click', function (e) {
    var r = e.target.closest('.sbrow'); if (!r) return;
    A('GET', '/api/admin/team?id=' + r.getAttribute('data-id')).then(function (s) {
      el('sheetTitle').textContent = 'Team ' + s.team.number + ' — ' + s.team.name;
      el('sheetBody').innerHTML = '<table><thead><tr><th>Q</th><th>Answer</th><th>Official</th><th>Notes</th><th class="right">Pts</th></tr></thead><tbody>' +
        s.rows.map(function (x) {
          return '<tr><td class="num">' + x.q + '</td><td>' + esc(x.answer) + '</td><td class="muted">' + esc(x.official) +
            '</td><td class="muted small">' + esc(x.hint) + '</td><td class="right num">' +
            (x.fraction === null && x.answer ? '<span class="chip amber">?</span>' : x.points) + '</td></tr>';
        }).join('') + '</tbody></table>' +
        '<h2 class="mt">Every change typed (dispute evidence)</h2>' +
        s.history.map(function (h) {
          return '<div class="logline">' + new Date(h.ts * 1000).toLocaleTimeString() + ' · ' + h.phase + ' · Q' + h.q +
            ' · ' + esc(h.answer) + (h.hint ? '  [notes: ' + esc(h.hint) + ']' : '') + '</div>';
        }).join('');
      el('sheetDlg').showModal();
    });
  });
  el('sheetClose').addEventListener('click', function () { el('sheetDlg').close(); });
  el('adjBtn').addEventListener('click', function () {
    var t = (D.teams || []).filter(function (x) { return x.number === num(el('adjTeam').value); })[0];
    if (!t) return W.toast('No such team', 'err');
    act('/api/admin/adjust', { team_id: t.id, points: num(el('adjPts').value), note: el('adjNote').value }, 'Applied')
      .then(function () { el('adjPts').value = ''; el('adjNote').value = ''; });
  });

  // ----------------------------------------------------------------- TEAMS
  function fetchTeams() {
    if (F) seen.t = F.ver.t;
    return A('GET', '/api/admin/teams').then(function (r) { D.teams = r.teams; renderTeams(); if (F) renderTiles(F); });
  }
  function renderTeams() {
    var f = el('teamFilter').value.trim().toLowerCase(), list = D.teams || [];
    el('teamCount').textContent = list.length;
    var rows = list.filter(function (t) { return !f || t.name.toLowerCase().indexOf(f) >= 0 || String(t.number) === f; });
    el('teamList').innerHTML = '<table><thead><tr><th>#</th><th>Name</th><th>PIN</th><th></th></tr></thead><tbody>' +
      rows.map(function (t) {
        return '<tr data-id="' + t.id + '"><td class="num"><b>' + t.number + '</b></td>' +
          '<td><input data-name value="' + esc(t.name) + '" style="padding:6px 8px"></td>' +
          '<td class="mono">' + esc(t.pin) + '</td><td class="right" style="white-space:nowrap">' +
          '<span class="chip ' + (t.online ? 'green' : t.joined ? 'amber' : '') + '">' + (t.online ? 'online' : t.joined ? 'joined' : '—') + '</span> ' +
          '<button class="btn-sm" data-reset title="New PIN, signs the phone out">RESET</button> ' +
          '<button class="btn-sm ' + (t.active ? '' : 'btn-red') + '" data-active="' + (t.active ? 0 : 1) + '">' +
          (t.active ? 'REMOVE' : 'RESTORE') + '</button></td></tr>';
      }).join('') + '</tbody></table>';
  }
  el('teamFilter').addEventListener('input', renderTeams);
  el('teamList').addEventListener('change', function (e) {
    if (!e.target.hasAttribute('data-name')) return;
    act('/api/admin/teams/update', { id: Number(e.target.closest('tr').getAttribute('data-id')), name: e.target.value }, 'Renamed');
  });
  el('teamList').addEventListener('click', function (e) {
    var tr = e.target.closest('tr'); if (!tr) return;
    var id = Number(tr.getAttribute('data-id'));
    if (e.target.hasAttribute('data-reset')) {
      if (confirm('Issue a new PIN and sign this team\'s phone out?')) act('/api/admin/teams/reset', { id: id }, 'Reset').then(fetchTeams);
    } else if (e.target.hasAttribute('data-active')) {
      act('/api/admin/teams/update', { id: id, active: e.target.getAttribute('data-active') === '1' }).then(fetchTeams);
    }
  });
  el('addTeam').addEventListener('click', function () {
    act('/api/admin/teams', { number: num(el('tNum').value), name: el('tName').value }, 'Added').then(function () {
      el('tNum').value = ''; el('tName').value = ''; fetchTeams();
    });
  });
  el('bulkRename').addEventListener('click', function () {
    act('/api/admin/teams/rename', { names: el('bulkTeams').value.split('\n') }, 'Renamed').then(fetchTeams);
  });

  // ------------------------------------------------------------- QUESTIONS
  function fetchQuestions() {
    if (F) seen.q = F.ver.q;
    return A('GET', '/api/admin/questions').then(function (r) {
      D.questions = r.questions; renderQuestions(); runKey = '';
      if (F) renderRun();
    });
  }
  function renderQuestions() {
    var list = D.questions || [];
    el('qCount').textContent = list.length;
    el('qList').innerHTML = list.map(function (q) {
      var big = (q.media_bytes || []).some(function (b) { return b > 300000; });
      return '<div class="qrow" data-id="' + q.id + '"><b>Q' + q.number + '</b><span class="small" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' +
        esc(q.text) + '</span><span class="small muted">' + esc(q.official_answer) + '</span>' +
        (q.images.length ? '<span class="badge' + (big ? ' todo' : '') + '" title="' + (big ? 'large media: slow to send to phones' : '') + '">' + q.images.length + ' media</span>' : '') + '</div>';
    }).join('');
  }
  el('qList').addEventListener('click', function (e) {
    var r = e.target.closest('.qrow'); if (!r) return;
    var q = D.questions.filter(function (x) { return x.id === Number(r.getAttribute('data-id')); })[0];
    el('qId').value = q.id; el('qNum').value = q.number; el('qPoints').value = q.points == null ? '' : q.points;
    el('qText').value = q.text; el('qAns').value = q.official_answer; el('qAlt').value = q.alt_answers.join('\n');
    el('qMedia').value = q.images.join(', '); el('qNote').value = q.notes;
    el('qFormTitle').textContent = 'Edit question ' + q.number;
  });
  function clearQForm() {
    ['qId', 'qNum', 'qPoints', 'qText', 'qAns', 'qAlt', 'qMedia', 'qNote'].forEach(function (i) { el(i).value = ''; });
    el('qFormTitle').textContent = 'Add question';
  }
  el('qClear').addEventListener('click', clearQForm);
  el('qSave').addEventListener('click', function () {
    var body = {
      number: num(el('qNum').value), text: el('qText').value, official_answer: el('qAns').value,
      alt_answers: el('qAlt').value, images: el('qMedia').value.split(',').map(function (s) { return s.trim(); }).filter(Boolean),
      notes: el('qNote').value, points: el('qPoints').value === '' ? null : num(el('qPoints').value)
    };
    var id = el('qId').value;
    if (body.number === null) delete body.number;
    act(id ? '/api/admin/questions/update' : '/api/admin/questions', id ? Object.assign({ id: Number(id) }, body) : body, 'Saved')
      .then(function () { clearQForm(); fetchQuestions(); });
  });
  el('csvFile').addEventListener('change', function (e) {
    var file = e.target.files[0]; if (!file) return;
    var r = new FileReader(); r.onload = function () { el('csvText').value = r.result; }; r.readAsText(file);
  });
  el('csvImport').addEventListener('click', function () {
    act('/api/admin/questions/import', { csv: el('csvText').value, replace: el('csvReplace').checked }).then(function (r) {
      W.toast('Imported ' + r.added + ' questions' + (r.skipped.length ? ' (' + r.skipped.length + ' skipped)' : ''), r.skipped.length ? 'err' : 'ok', 4000);
      if (r.skipped.length) alert(r.skipped.join('\n'));
      el('csvText').value = ''; fetchQuestions();
    });
  });

  // ----------------------------------------------------------------- SETUP
  function fetchConfig() {
    if (F) seen.c = F.ver.c;
    A('GET', '/api/admin/config').then(function (r) {
      var c = D.config = r.config;
      if (el('tab-setup').contains(document.activeElement)) return;   // never overwrite a form being edited
      el('cName').value = c.quiz_name; el('cPts').value = c.default_points; el('cP3').value = c.pass3_seconds;
      el('cQT').value = c.q_timer_seconds; el('cShowQ').value = c.show_question_on_phone;
      el('cMedia').value = c.send_media_to_phones; el('cResults').value = c.results_to_phones;
      el('cSingle').value = c.strict_single_device;
      el('cSsid').value = c.wifi_ssid || ''; el('cSsid').placeholder = c.detected_ssid || 'network name';
      el('cWpw').value = c.wifi_password || '';
    });
    if (F) {
      el('joinUrl').textContent = F.join_url; el('scorerUrl').textContent = F.scorer_url;
      el('qrbox').innerHTML = '<img alt="" src="/qr.svg?url=' + encodeURIComponent(F.join_url) + '">';
    }
  }
  el('saveCfg').addEventListener('click', function () {
    act('/api/admin/config', {
      quiz_name: el('cName').value, default_points: el('cPts').value, pass3_seconds: el('cP3').value,
      q_timer_seconds: el('cQT').value, show_question_on_phone: el('cShowQ').value,
      send_media_to_phones: el('cMedia').value, results_to_phones: el('cResults').value,
      strict_single_device: el('cSingle').value, wifi_ssid: el('cSsid').value, wifi_password: el('cWpw').value
    }, 'Settings saved').then(function () { timerSecs = null; fetchConfig(); });
  });
  el('pinSave').addEventListener('click', function () {
    act('/api/admin/change_pin', { current_pin: el('pinCur').value, new_pin: el('pinNew').value }, 'PIN changed').then(function (r) {
      token = r.token; localStorage.setItem('wiz_admin_token', token); el('pinCur').value = el('pinNew').value = '';
    });
  });
  el('clearBtn').addEventListener('click', function () {
    if (!confirm('Remove ALL answers and marking decisions and return to the lobby?')) return;
    var pin = prompt('Scorer PIN to confirm:'); if (!pin) return;
    act('/api/admin/clear', { pin: pin }, 'Cleared').then(function () { rv.q = null; });
  });
  el('masterBtn').addEventListener('click', function () {
    var pin = prompt('MASTER RESET removes every team, question and answer.\nScorer PIN:'); if (!pin) return;
    var phrase = prompt('Type RESET EVERYTHING to confirm:'); if (!phrase) return;
    act('/api/admin/master_reset', { pin: pin, confirm: phrase }, 'Everything cleared').then(function () { rv.q = null; fetchTeams(); fetchQuestions(); });
  });
  function renderHealth() {
    if (tab !== 'setup' || !F) return;
    var s = F.server, d = s.db, c = s.sse;
    el('health').innerHTML =
      'phones connected : ' + c.team + ' &nbsp; projector/board : ' + (c.screen + c.board) + ' &nbsp; scorers : ' + c.scorer + '<br>' +
      'db commits : ' + d.commits + ' for ' + d.jobs + ' writes (largest batch ' + d.max_batch + ')<br>' +
      'csv backups : ' + s.backup.writes + (s.backup.last_error ? ' <span class="neg">ERROR</span>' : ' ok') + '<br>' +
      'uptime : ' + Math.round(s.uptime / 60) + ' min';
  }

  // ------------------------------------------------------------------- LOG
  function fetchLog() {
    A('GET', '/api/admin/audit').then(function (r) {
      el('auditLog').innerHTML = r.audit.map(function (a) {
        return '<div class="logline"><span class="muted">' + new Date(a.ts * 1000).toLocaleTimeString() + '</span> ' +
          esc(a.actor) + ' · <b>' + esc(a.action) + '</b> ' + esc(a.detail) + '</div>';
      }).join('');
    });
  }

  if (token) {
    A('GET', '/api/admin/config').then(boot).catch(function () { logout(); });
  }
})();
