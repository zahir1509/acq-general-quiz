/* Shared client runtime: event stream with a polling safety net, a server-clock
   estimate, JSON helpers and toasts.

   Network posture (one Wi-Fi router, 50-100 phones):
   - ONE long-lived event stream per device; frames are tiny and identical for
     every phone. Nothing a phone types ever rides the stream.
   - If the stream goes quiet (the server pings every 10s) a cheap poll runs;
     if it stays quiet the stream is rebuilt after a random delay, so 100
     phones that lost Wi-Fi together do not all reconnect in the same instant.
   - The device keeps an estimate of the SERVER clock, so countdowns agree on
     every phone and a late-delivered edit can be proven to predate a lock.
*/

(function (global) {
  'use strict';

  function el(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function toast(msg, kind, ms) {
    var host = el('toast');
    if (!host) { host = document.createElement('div'); host.id = 'toast'; document.body.appendChild(host); }
    var n = document.createElement('div');
    n.className = 'toast ' + (kind || '');
    n.textContent = msg;
    host.appendChild(n);
    setTimeout(function () { n.remove(); }, ms || (kind === 'err' ? 4200 : 2000));
  }

  // ------------------------------------------------------------------ clock
  var samples = [];
  var offset = 0;                       // serverSeconds - localSeconds

  function noteServerTime(serverT) {
    if (typeof serverT !== 'number') return;
    samples.push(serverT - Date.now() / 1000);
    if (samples.length > 6) samples.shift();
    // a delayed message under-reports the offset, so the largest sample is the truest
    offset = Math.max.apply(null, samples);
  }

  function now() { return Date.now() / 1000 + offset; }

  function mmss(sec) {
    sec = Math.max(0, Math.ceil(sec));
    var m = Math.floor(sec / 60), s = sec % 60;
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  // ------------------------------------------------------------------- http
  function request(method, url, body, token) {
    var opts = { method: method, headers: {}, cache: 'no-store' };
    if (body !== undefined && body !== null) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    if (token) opts.headers['Authorization'] = 'Bearer ' + token;
    return fetch(url, opts).then(function (r) {
      return r.text().then(function (t) {
        var data;
        try { data = t ? JSON.parse(t) : {}; } catch (e) { data = { raw: t }; }
        if (data && typeof data.t === 'number') noteServerTime(data.t);
        if (data && typeof data.now === 'number') noteServerTime(data.now);
        if (!r.ok) {
          var err = new Error(data.error || ('HTTP ' + r.status));
          err.status = r.status;
          err.data = data;
          throw err;
        }
        return data;
      });
    });
  }

  function get(url, token) { return request('GET', url, null, token); }
  function post(url, body, token) { return request('POST', url, body || {}, token); }

  // ------------------------------------------------------------ connection
  function Connection(opts) {
    this.role = opts.role;
    this.token = opts.token || '';
    this.onState = opts.onState || function () {};
    this.onStatus = opts.onStatus || function () {};
    this.onAuthFail = opts.onAuthFail || function () {};
    this.es = null;
    this.lastMsg = 0;
    this.seq = -1;
    this.up = null;
    this.stopped = false;
    this.polling = false;
  }

  Connection.prototype.start = function () {
    var self = this;
    this.connect();
    this.timer = setInterval(function () { self.watchdog(); }, 3000);
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) self.watchdog(true);
    });
    window.addEventListener('online', function () { self.reconnectSoon(300); });
    return this;
  };

  Connection.prototype.url = function () {
    var u = '/api/events?role=' + encodeURIComponent(this.role);
    if (this.token) u += '&t=' + encodeURIComponent(this.token);
    return u;
  };

  Connection.prototype.connect = function () {
    var self = this;
    try { if (this.es) this.es.close(); } catch (e) {}
    var es = new EventSource(this.url());
    this.es = es;
    es.addEventListener('state', function (ev) {
      self.heard();
      var data;
      try { data = JSON.parse(ev.data); } catch (e) { return; }
      self.deliver(data);
    });
    es.addEventListener('ping', function (ev) {
      self.heard();
      try { noteServerTime(JSON.parse(ev.data).t); } catch (e) {}
    });
    es.onopen = function () { self.heard(); };
    es.onerror = function () {
      self.setStatus(false);
      // CLOSED means the browser gave up (401/503): retry ourselves, after a random delay
      if (es.readyState === 2) self.reconnectSoon(2000 + Math.random() * 4000);
    };
  };

  Connection.prototype.heard = function () { this.lastMsg = Date.now(); this.setStatus(true); };

  Connection.prototype.reconnectSoon = function (ms) {
    var self = this;
    if (this.stopped || this.pending) return;
    this.pending = setTimeout(function () { self.pending = null; self.connect(); self.poll(); }, ms);
  };

  Connection.prototype.deliver = function (data) {
    if (data.seq !== undefined && data.seq <= this.seq) return;   // stale or duplicate frame
    if (data.seq !== undefined) this.seq = data.seq;
    this.onState(data);
  };

  Connection.prototype.poll = function () {
    if (this.stopped || this.polling) return;
    var self = this;
    this.polling = true;
    request('GET', '/api/pub?role=' + encodeURIComponent(this.role) + '&v=' + this.seq,
            null, this.token).then(function (d) {
      self.polling = false;
      self.heard();
      if (d.frame) self.deliver(d.frame);
    }).catch(function (e) {
      self.polling = false;
      if (e.status === 401) self.onAuthFail();
      else self.setStatus(false);
    });
  };

  Connection.prototype.watchdog = function (force) {
    var age = Date.now() - this.lastMsg;
    if (force || age > 15000) this.poll();
    if (age > 30000) this.reconnectSoon(Math.random() * 3000);
  };

  Connection.prototype.setStatus = function (up) {
    if (this.up === up) return;
    this.up = up;
    this.onStatus(up);
    var b = el('offline');
    if (b) b.classList.toggle('hidden', up);
  };

  Connection.prototype.stop = function () {
    this.stopped = true;
    clearInterval(this.timer);
    try { this.es.close(); } catch (e) {}
  };

  // ------------------------------------------------------------ misc
  function deviceId() {
    var k = 'wiz_device_id';
    var v = localStorage.getItem(k);
    if (!v) {
      v = (Math.random().toString(36).slice(2) + Date.now().toString(36)).slice(0, 24);
      localStorage.setItem(k, v);
    }
    return v;
  }

  function fmtPoints(p) {
    if (p === null || p === undefined || p === '') return '';
    var n = Number(p);
    return (n > 0 ? '+' : '') + (Math.round(n * 100) / 100);
  }

  // 'image' | 'audio' | 'video', from the file extension of a media URL.
  function mediaType(url) {
    var path = String(url).split('?')[0].toLowerCase();
    var ext = path.slice(path.lastIndexOf('.') + 1);
    if (/^(mp3|m4a|aac|wav|ogg|opus|flac)$/.test(ext)) return 'audio';
    if (/^(mp4|m4v|mov|webm)$/.test(ext)) return 'video';
    return 'image';
  }

  var api = {
    el: el, esc: esc, toast: toast, request: request, get: get, post: post,
    Connection: Connection, deviceId: deviceId, fmtPoints: fmtPoints,
    mediaType: mediaType, now: now, mmss: mmss, noteServerTime: noteServerTime
  };
  global.WIZ = api;
  global.PNB = api;
})(window);
