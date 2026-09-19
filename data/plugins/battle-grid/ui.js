/* 战斗格子。
 *
 * 位置是守秘人用 <state> 摆的，引擎存着，这里只负责画。
 * 想换成更好看的版本，另写一个插件占 battle 这个坑位就行，
 * 数据从 DSH.call('battle_state') 拿，格式见 docs/插件编写格式.md。
 */
(function () {
  'use strict';

  var PC = '#8fc7ff';       // 调查员：冷蓝
  var NPC = '#e08a8a';      // NPC：砖红
  var OFF = '#6a7386';

  var off = DSH.body.querySelector('#bgOff');
  var on = DSH.body.querySelector('#bgOn');
  var gridBox = DSH.body.querySelector('#bgGrid');
  var initBox = DSH.body.querySelector('#bgInit');
  var chaseBox = DSH.body.querySelector('#bgChaseBox');
  var trackBox = DSH.body.querySelector('#bgChase');

  DSH.body.querySelector('.bg-main').style.cssText =
    'display:flex;gap:12px;align-items:flex-start';
  DSH.body.querySelector('.bg-side').style.cssText = 'min-width:132px';
  DSH.body.querySelectorAll('.bg-title').forEach(function (n) {
    n.style.cssText = 'color:#98a1b4;font-size:11.5px;margin-bottom:5px';
  });
  initBox.style.cssText = 'display:flex;flex-direction:column;gap:3px';
  chaseBox.style.cssText = 'margin-top:10px';
  trackBox.style.cssText = 'display:flex;gap:4px;align-items:center;flex-wrap:wrap';
  off.style.cssText = 'color:#6a7386;font-size:12px;line-height:1.7';

  var portraits = {};       // 名字 -> data URL，有立绘就画立绘

  function loadPortraits() {
    DSH.call('portraits').then(function (r) {
      var items = (r && r.items) || {};
      var keys = Object.keys(items);
      if (!keys.length) return;
      var jobs = keys.map(function (k) {
        return DSH.call('portrait_get', k).then(function (p) {
          if (!p || !p.ok) return;
          // pc_余快 / npc_石泽，把前缀去掉就是棋子的名字
          var name = k.replace(/^(pc|npc|player)_/, '');
          portraits[name] = p.data;
        }).catch(function () { /* 单张读不出来不影响别的 */ });
      });
      Promise.all(jobs).then(function () { pull(); });
    }).catch(function () { /* 没有立绘目录也不影响 */ });
  }

  function draw(b) {
    if (!b || !b.active) {
      off.style.display = 'block';
      on.style.display = 'none';
      return;
    }
    off.style.display = 'none';
    on.style.display = 'block';

    var cell = Math.max(14, Math.min(30, Math.floor(300 / Math.max(b.w, b.h))));
    gridBox.style.cssText =
      'display:grid;gap:2px;grid-template-columns:repeat(' + b.w + ',' + cell + 'px)';

    // 先把棋子按格子归位，一次遍历，别每格去 find
    var at = {};
    (b.tokens || []).forEach(function (t) {
      if (t.x > 0 && t.y > 0) at[t.x + ',' + t.y] = t;
    });

    gridBox.innerHTML = '';
    for (var y = 1; y <= b.h; y++) {
      for (var x = 1; x <= b.w; x++) {
        var c = DSH.el('div');
        var t = at[x + ',' + y];
        var isPc = t && t.kind === 'pc';
        c.style.cssText =
          'width:' + cell + 'px;height:' + cell + 'px;border-radius:3px;' +
          'background:' + (t ? (isPc ? PC : NPC) : '#1e2330') + ';' +
          'border:1px solid ' + (t ? 'rgba(255,255,255,.25)' : '#262c3a') + ';' +
          'display:flex;align-items:center;justify-content:center;' +
          'font-size:' + Math.max(9, cell - 14) + 'px;color:#0d0f14;' +
          'font-weight:700;overflow:hidden';
        if (t) {
          var pic = portraits[t.name];
          if (pic) {
            c.style.backgroundImage = 'url(' + pic + ')';
            c.style.backgroundSize = 'cover';
            c.style.backgroundPosition = 'center';
          } else {
            c.textContent = String(t.name || '?').slice(0, 2);
          }
          c.title = t.name + '（' + (t.kind === 'pc' ? '调查员' : 'NPC') + '）'
            + ' 第' + x + '列 第' + y + '行'
            + (t.note ? '\n' + t.note : '');
        } else {
          c.title = '第' + x + '列 第' + y + '行';
        }
        gridBox.appendChild(c);
      }
    }

    // 先攻
    initBox.innerHTML = '';
    (b.initiative || []).forEach(function (it, i) {
      var row = DSH.el('div');
      row.style.cssText = 'display:flex;align-items:center;gap:5px;font-size:11.5px';
      var n = DSH.el('span', null, String(i + 1) + '.');
      n.style.cssText = 'color:#6a7386;width:15px';
      row.appendChild(n);
      var dot = DSH.el('span');
      dot.style.cssText = 'width:7px;height:7px;border-radius:50%;background:'
        + (it.kind === 'pc' ? PC : NPC);
      row.appendChild(dot);
      row.appendChild(DSH.el('span', null, it.name));
      var d = DSH.el('span', null, 'DEX ' + it.dex);
      d.style.cssText = 'color:#6a7386;margin-left:auto';
      row.appendChild(d);
      initBox.appendChild(row);
    });
    if (!(b.initiative || []).length) {
      initBox.appendChild(DSH.el('div', null, '（还没有棋子）')).style.color = OFF;
    }

    // 追逐轨道
    var ch = b.chase || {};
    if (!ch.active || !ch.len) {
      chaseBox.style.display = 'none';
      return;
    }
    chaseBox.style.display = 'block';
    var bySlot = {};
    Object.keys(ch.positions || {}).forEach(function (name) {
      var n = ch.positions[name];
      (bySlot[n] = bySlot[n] || []).push(name);
    });
    trackBox.innerHTML = '';
    for (var i = 0; i < ch.len; i++) {
      var slot = DSH.el('div');
      var names = bySlot[i] || [];
      slot.style.cssText =
        'min-width:34px;height:30px;border-radius:5px;border:1px solid #262c3a;' +
        'background:' + (names.length ? '#2b4a80' : '#171b24') + ';' +
        'display:flex;align-items:center;justify-content:center;' +
        'font-size:10px;color:#dfe3ec;padding:0 3px;overflow:hidden';
      slot.textContent = names.length ? names.join(' ').slice(0, 6) : i;
      slot.title = names.length ? ('第 ' + i + ' 格：' + names.join('、'))
                                : ('第 ' + i + ' 格');
      trackBox.appendChild(slot);
    }
  }

  function pull() {
    DSH.call('battle_state').then(function (b) {
      try { draw(b); } catch (e) { DSH.debug('画格子出错', e); }
    }).catch(function (e) { DSH.debug('取格子状态失败', e); });
  }

  // 引擎摆子时会推 battle 事件，收到就重画
  DSH.on('battle', function (ev) {
    var b = ev && ev.meta && ev.meta.battle;
    if (b) { try { draw(b); } catch (e) { DSH.debug(e); } }
    else pull();
  });
  DSH.on('scene', pull);

  pull();
  loadPortraits();
  DSH.debug('ready');
})();
