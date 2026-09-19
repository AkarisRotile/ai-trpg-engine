/* 思维链查看器。
 *
 * 它订阅引擎的 debug 事件。引擎那边只有把 options.debug_stream 打开
 * 才会走流式并推事件（这个插件在 plugin.json 里用 engine_options 要求了）。
 *
 * 每一格显示一个座位的三个阶段：
 *   1. 正在写（绿的追加文本，每半秒来一次快照）
 *   2. 原文（模型这一轮完整写了什么，包含它自己的标签）
 *   3. 引擎改成了什么（机械清洗之后真正进到桌上的文字）
 */
(function () {
  'use strict';

  var panes = {};          // seat -> {wrap, live, raw, diff, meta}
  var paused = false;
  var count = 0;

  var panesBox = DSH.body.querySelector('#tvPanes');
  var pauseBox = DSH.body.querySelector('#tvPause');
  var countBox = DSH.body.querySelector('#tvCount');

  pauseBox.addEventListener('change', function () {
    paused = pauseBox.checked;
  });

  DSH.body.querySelector('.tv-hint').style.cssText =
    'color:#6a7386;font-size:11.5px;line-height:1.6;margin:6px 0 10px';
  DSH.body.querySelector('.tv-bar').style.cssText =
    'display:flex;align-items:center;gap:10px;font-size:12px;color:#98a1b4';
  panesBox.style.cssText = 'display:flex;flex-direction:column;gap:9px';

  function paneFor(seat) {
    if (panes[seat]) return panes[seat];

    var wrap = DSH.el('div');
    wrap.style.cssText =
      'background:#171b24;border:1px solid #262c3a;border-radius:8px;padding:8px 10px';

    var head = DSH.el('div');
    head.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:5px';
    head.appendChild(DSH.el('b', null, seat || '?'));
    var meta = DSH.el('span');
    meta.style.cssText = 'color:#6a7386;font-size:11px';
    head.appendChild(meta);
    wrap.appendChild(head);

    var live = DSH.el('div');
    live.style.cssText =
      'color:#7fd39a;font-family:Consolas,monospace;font-size:11.5px;' +
      'white-space:pre-wrap;word-break:break-word;max-height:150px;overflow:auto';
    wrap.appendChild(live);

    var diff = DSH.el('div');
    diff.style.cssText =
      'color:#6a7386;font-size:11px;margin-top:6px;white-space:pre-wrap;' +
      'word-break:break-word;max-height:110px;overflow:auto;display:none';
    wrap.appendChild(diff);

    var rawBox = DSH.el('details');
    rawBox.style.cssText = 'margin-top:6px';
    var sum = DSH.el('summary');
    sum.style.cssText = 'cursor:pointer;color:#6a7386;font-size:11px';
    sum.textContent = '看模型完整原文（含它自己写的标签）';
    rawBox.appendChild(sum);
    var raw = DSH.el('pre');
    raw.style.cssText =
      'margin:5px 0 0;padding:6px 8px;background:#0d0f14;border-radius:6px;' +
      'color:#dfe3ec;font-size:11px;white-space:pre-wrap;word-break:break-word;' +
      'max-height:220px;overflow:auto';
    rawBox.appendChild(raw);
    wrap.appendChild(rawBox);

    panesBox.appendChild(wrap);
    panes[seat] = { wrap: wrap, live: live, raw: raw, diff: diff, meta: meta };
    return panes[seat];
  }

  /** 只显示被引擎改掉的那些地方。按行比，省得整段刷一遍。 */
  function renderDiff(p, before, after) {
    var a = String(before || '').split('\n');
    var b = String(after || '').split('\n');
    var out = [];
    var n = Math.max(a.length, b.length);
    for (var i = 0; i < n; i++) {
      if (a[i] === b[i]) continue;
      if (a[i] !== undefined) out.push('− ' + a[i]);
      if (b[i] !== undefined) out.push('+ ' + b[i]);
    }
    if (!out.length) {
      p.diff.style.display = 'none';
      return;
    }
    p.diff.style.display = 'block';
    p.diff.textContent = '引擎改了这些：\n' + out.join('\n');
  }

  DSH.on('debug', function (ev) {
    if (paused) return;
    var m = ev.meta || {};
    var p = paneFor(m.seat || ev.name || '?');
    count++;
    countBox.textContent = '已收到 ' + count + ' 段';

    var bits = [];
    if (m.phase) bits.push(m.phase);
    if (m.kind) bits.push(m.kind);
    if (m.final) {
      bits.push('完成');
      if (m.latency_ms) bits.push(m.latency_ms + 'ms');
      if (m.completion_tokens) bits.push('出 ' + m.completion_tokens + ' tok');
      if (m.cached_tokens) bits.push('缓存 ' + m.cached_tokens + ' tok');
      if (m.changed) bits.push('⚠ 被引擎改过');
    } else {
      bits.push('正在写…');
    }
    p.meta.textContent = bits.join(' · ');

    p.live.textContent = ev.text || '';
    p.live.scrollTop = p.live.scrollHeight;

    if (m.final) {
      p.raw.textContent = m.text || '';
      renderDiff(p, m.text, m.cleaned);
    }
  });

  DSH.debug('ready');
})();
