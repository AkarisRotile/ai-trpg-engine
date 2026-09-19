/* ══════════════════════════════════════════════════════════════════════
   AI 跑团引擎 · 前端
   通过 window.pywebview.api.* 调用 Python 引擎；事件用轮询取。
   ══════════════════════════════════════════════════════════════════════ */

'use strict';

const KEEP = '__KEEP__';
const POLL_MS = 400;
const LOG_CAP = 1600;

const EVENT_META = {
  narr:     { label: '守秘人', icon: '📜', filter: 'narr' },
  act:      { label: '行动',   icon: '🎭', filter: 'act' },
  ooc:      { label: '桌边',   icon: '💬', filter: 'ooc' },
  think:    { label: '内心',   icon: '💭', filter: 'think' },
  secret:   { label: '幕后',   icon: '🔒', filter: 'secret' },
  dice:     { label: '骰子',   icon: '🎲', filter: 'dice' },
  secret_dice: { label: '暗骰', icon: '🔒', filter: 'secret_dice' },
  recall:   { label: '回忆',   icon: '💭', filter: 'recall' },
  system:   { label: '系统',   icon: '·',  filter: 'system' },
  scene:    { label: '场景',   icon: '📍', filter: 'scene' },
  chargen:  { label: '车卡',   icon: '📋', filter: 'chargen' },
  brief:    { label: '简报',   icon: '📣', filter: 'brief' },
  study:    { label: 'KP功课', icon: '📝', filter: 'study' },
  study_report: { label: '通读报告', icon: '📖', filter: 'study_report' },
  study_talk:   { label: '研读室', icon: '☕', filter: 'study_talk' },
  audit:    { label: '审卡',   icon: '🔍', filter: 'audit' },
  reveal:   { label: '解禁',   icon: '🔓', filter: 'reveal' },
  pitch:    { label: '车卡意向', icon: '💡', filter: 'pitch' },
  director: { label: '导演',   icon: '🎬', filter: 'director' },
};

const DEFAULT_FILTERS = {
  narr: true, act: true, ooc: true, think: true,
  secret: true, dice: true, secret_dice: true, recall: true,
  system: true, scene: true,
  chargen: true, brief: true, pitch: true, study: true, audit: true,
  reveal: true, director: true,
  study_report: true, study_talk: true,
};

const S = {
  cfg: null,
  providers: {},
  modules: [],
  sessions: [],
  players: [],
  selectedModule: '',
  state: null,
  events: [],
  filters: { ...DEFAULT_FILTERS },
  activeSeat: null,
  rightTab: 'sheet',
  busy: false,
  dirtyKeys: new Set(),
};

/* ══════════════════════════ 桥接 ══════════════════════════ */

function bridgeReady() {
  return !!(window.pywebview && window.pywebview.api);
}

// 界面自己崩了也要留下记录：打包成 exe 之后没有控制台，
// 报错提示条一关就没了，用户说"报错了"的时候谁也查不到。
let _errSent = 0;
function reportClientError(message, detail) {
  try {
    if (!bridgeReady() || _errSent > 24) return;
    _errSent += 1;
    const fn = window.pywebview.api.log_client_error;
    if (typeof fn === 'function') fn(String(message || ''), String(detail || ''));
  } catch (e) { /* 记不上就算了，绝不能因为记日志再崩一次 */ }
}
window.addEventListener('error', (e) => {
  reportClientError(e.message || 'error',
    `${(e.filename || '')}:${e.lineno || 0}:${e.colno || 0}\n${(e.error && e.error.stack) || ''}`);
});
window.addEventListener('unhandledrejection', (e) => {
  const r = e.reason;
  reportClientError((r && r.message) || String(r || 'unhandledrejection'),
    (r && r.stack) || '');
});

// 界面上每个会等的操作都说清楚在等什么。
// 以前点了「模组」八秒没反应，看着就是坏了——那其实是引擎在读 23 MB 的模组正文。
const CALL_LABELS = {
  bootstrap: '载入中',
  module_detail: '读模组',
  study_room: '守秘人在通读模组',
  study_ask: '守秘人在想',
  list_studies: '看研读记录',
  forget_study: '清掉研读记录',
  prepare: '准备开局',
  start: '守秘人开场',
  step: '推进一轮',
  run_auto: '自动推进',
  finish: '散场',
  new_session: '建新局',
  load_session: '载入存档',
  resume_last: '接回上一局',
  list_sessions: '读存档列表',
  save_now: '存档',
  export_markdown: '导出',
  fetch_models: '拉取模型列表',
  probe: '测试连接',
  probe_all: '测试全部连接',
  probe_pdf: '看这个 PDF',
  ocr_pdf: '识别扫描件（可能要一两分钟）',
  get_roster: '读名册',
  save_roster: '存名册',
  assign_roles: '换站位',
  rotate_keeper: '轮换守秘人',
  refreshModelsLazy: '载入中',
  player_cards: '读玩家卡',
  player_card: '读玩家卡',
  save_player_card: '存玩家卡',
  search_rules: '翻规则书',
  open_player_folder: '开文件夹',
  open_seat_folder: '开文件夹',
  open_modules_folder: '开文件夹',
};

let _busySeq = 0;
let _busyTimer = null;
let _busySince = 0;

function busyOff(seq) {
  if (seq !== _busySeq) return;      // 已经有更新的调用了，别关错
  clearTimeout(_busyTimer);
  $('busy').classList.remove('on');
}

// 这几个人家自己每隔几百毫秒调一次，而且都是瞬间返回的。
// 让它们参与进度条只会打架：长调用的显示会被它们的计时器冲掉。
const NO_BUSY = new Set(['poll_events', 'state', 'busy', 'wait_idle',
                         'recent_errors', 'dice_log', 'transcript',
                         'list_sessions', 'player_cards', 'glossary_terms',
                         'module_study', 'study_state', 'vision_settings']);

async function call(method, ...args) {
  if (!bridgeReady()) throw new Error('界面桥接还没就绪，请稍等一秒再试。');
  const fn = window.pywebview.api[method];
  if (typeof fn !== 'function') throw new Error(`引擎没有提供方法 ${method}`);
  if (NO_BUSY.has(method)) return await fn(...args);

  const seq = ++_busySeq;
  const label = CALL_LABELS[method] || '处理中';
  // 慢过 400 毫秒才显示。瞬间返回的调用（比如已经预热过的 bootstrap）
  // 要是也闪一下进度条，反而更吵。
  clearTimeout(_busyTimer);
  _busyTimer = setTimeout(() => {
    if (seq !== _busySeq) return;
    $('busyText').textContent = label;
    $('busy').classList.add('on');
    _busySince = Date.now();
  }, 400);

  try {
    return await fn(...args);
  } finally {
    busyOff(seq);
  }
}

/* ══════════════════════════ 小工具 ══════════════════════════ */

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
};
const esc = (s) => String(s == null ? '' : s);

function toast(msg, kind = '') {
  const t = el('div', 'toast ' + kind, msg);
  $('toasts').appendChild(t);
  setTimeout(() => {
    t.style.opacity = '0';
    setTimeout(() => t.remove(), 250);
  }, kind === 'err' ? 8000 : 3600);
}

function openModal(id) { $(id).classList.add('open'); }
function closeModal(id) { $(id).classList.remove('open'); }
function closeAllModals() {
  document.querySelectorAll('.modal.open').forEach((m) => m.classList.remove('open'));
}

/* ══════════════════════════ 日志渲染 ══════════════════════════ */

function logEl() { return $('log'); }

function atBottom() {
  const l = logEl();
  return l.scrollHeight - l.scrollTop - l.clientHeight < 90;
}

function addEntry(evt) {
  const type = evt.type || 'system';
  if (!S.filters[type]) return;

  const meta = EVENT_META[type] || { label: type, icon: '·' };
  const node = el('div', 'entry ' + type);
  node.dataset.type = type;
  if (evt.seat_id) node.dataset.seat = evt.seat_id;

  // 玩家的条目统一用引擎给的 label：车卡阶段是网名（坐在这儿的是玩家本人），
  // 车出角色之后是「角色名（网名）」。守秘人那边不动——它的条目写「守秘人」更有味道。
  // 变量刻意叫 whoName：下面几个分支里 who 是那个 <div class="who"> 元素，
  // 撞名字的后果是页面上显示 [object HTMLDivElement]（踩过一次）。
  let whoName = evt.name || '';
  const seat = ((S.state && S.state.seats) || []).find((s) => s.seat_id === evt.seat_id);
  if (seat && seat.kind === 'PL' && seat.label && seat.has_character) whoName = seat.label;

  if (type === 'scene') {
    node.textContent = `—— ${esc(evt.text)} ——`;
  } else if (type === 'system') {
    const b = el('div', 'body', esc(evt.text));
    node.appendChild(b);
  } else if (type === 'chargen' || type === 'brief' || type === 'study'
             || type === 'reveal' || type === 'audit' || type === 'study_report') {
    const who = el('div', 'who');
    const label = {
      chargen: '的角色卡', brief: '给全桌的赛前简报（不含剧透）',
      study: '开局前做的功课', reveal: '模组解禁 · 全桌公开',
      study_report: '的通读报告 · 只给你看',
      audit: (evt.meta && evt.meta.title) || '审卡',
    }[type];
    who.appendChild(el('span', null, `${meta.icon} ${esc(whoName)} ${label}`));
    node.appendChild(who);
    if (type === 'audit' && evt.meta && evt.meta.rows) {
      const tbl = el('div', 'audit-rows');
      evt.meta.rows.forEach((r) => {
        const row = el('div', 'audit-row ' + (r.verdict === '质疑' ? 'bad'
          : r.verdict === '拿掉' ? 'strip' : 'ok'));
        row.appendChild(el('span', 'v', r.verdict));
        row.appendChild(el('span', 's', r.seat));
        row.appendChild(el('span', 'i', r.item));
        row.appendChild(el('span', 'n', r.note));
        tbl.appendChild(row);
      });
      node.appendChild(tbl);
    }
    if (type === 'study' && evt.meta && (evt.meta.spine_ids || []).length) {
      node.appendChild(el('div', 'hint',
        '大纲核对通过：' + evt.meta.spine_ids.join(' → ')));
    }
    if (type === 'study_report') {
      // 通读报告很长，日志里默认折叠，要细看就点开（或者去「📖 研读室」里看全文）
      const d = el('details');
      d.appendChild(el('summary', null, '展开全文'));
      d.appendChild(el('pre', null, esc(evt.text)));
      node.appendChild(d);
    } else {
      node.appendChild(el('pre', null, esc(evt.text)));
    }
    if (type === 'study' && evt.meta) {
      if (evt.meta.expansion) {
        const d = el('details');
        d.appendChild(el('summary', null, '他要加的东西'));
        d.appendChild(el('pre', null, esc(evt.meta.expansion)));
        node.appendChild(d);
      }
      if (evt.meta.eggs) {
        const d = el('details');
        d.appendChild(el('summary', null, '他埋的彩蛋（跑完才揭晓）'));
        d.appendChild(el('pre', null, esc(evt.meta.eggs)));
        node.appendChild(d);
      }
    }
    const warns = (evt.meta && evt.meta.warnings) || [];
    if (warns.length) node.appendChild(el('div', 'warns', '规则校验：' + warns.join('；')));
    if (type === 'brief' && evt.meta && evt.meta.secret_terms) {
      node.appendChild(el('div', 'hint',
        `已用 ${evt.meta.secret_terms} 个秘密词做过剧透检查。`));
    }
  } else {
    const who = el('div', 'who');
    const nameLabel = whoName ? `${esc(whoName)}` : '';
    const seatTag = type === 'ooc' ? '桌边' : meta.label;
    who.appendChild(el('span', null, `${meta.icon} ${nameLabel}`));
    who.appendChild(el('span', 'tag', seatTag));
    if (type === 'secret_dice') {
      const t = el('span', 'tag', '暗骰 · 玩家看不到');
      t.style.color = '#b98ce0';
      who.appendChild(t);
    }
    if (type === 'recall') who.appendChild(el('span', 'tag', '联想召回'));
    if ((evt.meta || {}).anchor) who.appendChild(el('span', 'tag', '入戏锚定'));
    if ((evt.meta || {}).repaired) who.appendChild(el('span', 'tag', '已重写'));
    if ((evt.meta || {}).retro) who.appendChild(el('span', 'tag', '散场'));
    node.appendChild(who);

    if (type === 'think' || type === 'secret') {
      const d = el('details');
      if (type === 'secret') d.open = false;
      const sum = el('summary', null, type === 'secret' ? '展开幕后状态' : '展开内心');
      d.appendChild(sum);
      d.appendChild(el('div', 'body', esc(evt.text)));
      node.appendChild(d);
    } else {
      node.appendChild(el('div', 'body', esc(evt.text)));
    }
  }

  const l = logEl();
  const stick = atBottom();
  l.appendChild(node);
  while (l.childElementCount > LOG_CAP) l.removeChild(l.firstChild);
  if (stick) l.scrollTop = l.scrollHeight;
}

function rerenderFilters() {
  document.querySelectorAll('#log .entry').forEach((n) => {
    n.classList.toggle('hidden', !S.filters[n.dataset.type]);
  });
}

function renderFilters() {
  const box = $('filters');
  box.innerHTML = '';
  for (const [key, meta] of Object.entries(EVENT_META)) {
    const lab = el('label');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = !!S.filters[key];
    cb.onchange = () => { S.filters[key] = cb.checked; rerenderFilters(); };
    lab.appendChild(cb);
    lab.appendChild(el('span', null, `${meta.icon} ${meta.label}`));
    box.appendChild(lab);
  }
}

/* ══════════════════════════ 席位列表 ══════════════════════════ */

function renderSeats() {
  const box = $('seatList');
  box.innerHTML = '';
  const seats = (S.state && S.state.seats) || [];
  if (!seats.length) {
    box.appendChild(el('div', 'empty', '还没有席位。打开「设置」添加。'));
    return;
  }
  seats.forEach((s) => {
    const n = el('div', 'seat ' + (s.kind === 'KP' ? 'kp' : 'pl'));
    if (s.ready) n.classList.add('ready');
    if (S.activeSeat === s.seat_id) n.classList.add('active');

    const name = el('div', 'name');
    name.appendChild(el('span', null, s.kind === 'KP' ? '🔒' : '🎭'));
    // label 由引擎给：车卡前是网名，车出角色后是「角色名（网名）」
    name.appendChild(el('span', null, esc(s.label || s.display_name || s.seat_id)));
    if (s.kind === 'KP') name.appendChild(el('span', 'pill', 'KP'));
    name.appendChild(el('span', 'pill ' + (s.set_up ? 'ok' : 'err'),
      s.set_up ? '已配' : '缺配置'));
    n.appendChild(name);

    const sub = [];
    if (s.has_character && s.player_name) sub.push('玩家 ' + s.player_name);
    if (!s.has_character && s.kind === 'PL') sub.push('还没车卡');
    if (s.occupation) sub.push(s.occupation);
    sub.push(s.provider + ' / ' + s.model);
    n.appendChild(el('div', 'sub', sub.join(' · ')));

    if (s.attrs && Object.keys(s.attrs).length) {
      n.appendChild(el('div', 'stat',
        `HP ${s.attrs.HP}/${s.attrs.MAXHP}  SAN ${s.attrs.SAN}  MP ${s.attrs.MP}  技能 ${(s.skills || []).length}`));
    }
    // 车卡过程说明。引擎缩放过的部分用中性文字讲清楚，不拿修复前的违规当红字报警。
    // 引擎的契约：warnings 只放修完之后仍然成立的话（比如"还剩 12 点没用"），
    // violations 是修复前的清单，界面不许把它当错误。
    const notes = [];
    const fixes = Array.isArray(s.chargen_repairs) ? s.chargen_repairs : [];
    if (fixes.length) {
      const pts = (s.chargen_spent != null && s.chargen_budget != null)
        ? `技能点 ${s.chargen_spent}/${s.chargen_budget}`
        : '技能点';
      notes.push(`${pts}，引擎缩放了 ${fixes.length} 处`);
    }
    (s.warnings || []).forEach((w) => notes.push(w));
    if (notes.length) {
      // 只有车卡根本没成（PL 还没卡）才算真错，其余都是说明。
      const bad = s.kind === 'PL' && !s.has_character;
      const line = el('div', bad ? 'warns' : 'notes', notes.join('；'));
      if (fixes.length) line.title = fixes.join('\n');
      n.appendChild(line);
    }

    n.onclick = () => { S.activeSeat = s.seat_id; renderSeats(); renderRight(); };
    box.appendChild(n);
  });
}

/* ══════════════════════════ 右栏 ══════════════════════════ */

async function renderRight() {
  const body = $('rightBody');
  body.innerHTML = '';
  syncRightTabs();

  // 插件挂上来的标签页：把它的节点搬进右栏。
  // 注意这里是**移动**节点，不是重新渲染，插件的状态不会丢。
  if (S.rightTab && S.rightTab.startsWith('plugin:')) {
    const pid = S.rightTab.slice(7);
    const rec = PLUGIN_OPEN.get(pid);
    const p = PLUGINS.find((x) => x.id === pid);
    if (!rec || !p || !p.enabled) {
      body.appendChild(el('div', 'empty', '这个插件没开着。'));
      return;
    }
    body.appendChild(rec.node);
    return;
  }

  if (!S.activeSeat) {
    body.appendChild(el('div', 'empty', '选择左侧一个席位查看详情'));
    return;
  }
  const seat = ((S.state && S.state.seats) || []).find((s) => s.seat_id === S.activeSeat);
  if (!seat) { body.appendChild(el('div', 'empty', '找不到该席位')); return; }

  if (S.rightTab === 'sheet') return renderSheet(body, seat);
  if (S.rightTab === 'memory') return renderMemory(body, seat);
  if (S.rightTab === 'player') return renderPlayerTab(body, seat);
  if (S.rightTab === 'dice') return renderDice(body, seat);
  if (S.rightTab === 'cost') return renderCost(body, seat);
}

/** 右栏标签栏：内置那五个 + 每个开着的 panel 插件一个。 */
function syncRightTabs() {
  const bar = $('rightTabs');
  if (!bar) return;
  bar.querySelectorAll('.tab.plugin-tab').forEach((n) => n.remove());
  PLUGINS.filter((p) => p.enabled && !p.error && p.surface === 'panel').forEach((p) => {
    const t = el('div', 'tab plugin-tab', '🧩 ' + (p.name || p.id));
    t.dataset.tab = 'plugin:' + p.id;
    if (S.rightTab === 'plugin:' + p.id) t.classList.add('active');
    bar.appendChild(t);
  });
}

async function renderDice(box, seat) {
  const r = await call('dice_log');
  if (!r.ok) { box.appendChild(el('div', 'empty', r.message || '还没有会话')); return; }
  const a = r.audit || {};

  const kv = el('dl', 'kv');
  [['掷骰次数', a.total || 0],
   ['随机源', a.entropy || '—'],
   ['分布', Object.entries(a.by_kind || {}).map(([k, v]) => `${k}×${v}`).join('  ') || '—']
  ].forEach(([k, v]) => {
    kv.appendChild(el('dt', null, k));
    kv.appendChild(el('dd', null, String(v)));
  });
  box.appendChild(kv);

  const note = el('div', 'hint');
  note.innerHTML = a.fair
    ? '随机数来自操作系统熵池，<b>模型碰不到随机源</b>——它只能提交"掷什么"的申请，' +
      '拿不到也改不了出目。下面每一条都记着原始点数，可以逐条核对。'
    : '⚠ 当前用的是固定种子（只在自动化测试里会这样），不适合正式跑团。';
  box.appendChild(note);

  box.appendChild(el('div', 'sub-title', `最近 ${(r.recent || []).length} 次`));
  const list = el('div', 'chronicle');
  (r.recent || []).slice().reverse().forEach((d) => {
    const it = el('div', 'item');
    it.appendChild(el('span', 'm', `[#${d.seq}·第${d.turn}轮·${d.actor}] `));
    it.appendChild(el('span', null, esc(d.summary)));
    list.appendChild(it);
  });
  if (!(r.recent || []).length) list.appendChild(el('div', 'hint', '还没掷过。'));
  box.appendChild(list);

  // 手动骰子台：用的是同一个内核，所以也是真随机
  box.appendChild(el('div', 'sub-title', '手动掷骰'));
  const row = el('div', 'row-actions');
  const inp = el('input');
  inp.type = 'text';
  inp.value = '1d100';
  inp.style.width = '110px';
  const btn = el('button', 'btn tiny', '掷');
  const out = el('div', 'hint', '表达式例如 3d6 / 1d8+2 / 2d6+1d4');
  btn.onclick = async () => {
    const res = await call('roll_now', inp.value || '1d100');
    if (res.ok) out.textContent = res.summary;
    else toast(res.message || '掷骰失败', 'err');
  };
  inp.onkeydown = (e) => { if (e.key === 'Enter') btn.click(); };
  row.appendChild(inp);
  row.appendChild(btn);
  box.appendChild(row);
  box.appendChild(out);
}

function renderSheet(box, seat) {
  const a = seat.attrs || {};
  const head = el('div');
  head.appendChild(el('div', 'sub-title', '调查员'));
  const kv = el('dl', 'kv');
  const rows = [
    ['名字', seat.display_name || '—'],
    ['职业', seat.occupation || '—'],
    ['玩家', seat.player_name || '—'],
    ['模型', `${seat.provider} / ${seat.model}`],
  ];
  rows.forEach(([k, v]) => {
    kv.appendChild(el('dt', null, k));
    kv.appendChild(el('dd', null, esc(v)));
  });
  head.appendChild(kv);
  box.appendChild(head);

  if (!Object.keys(a).length) {
    box.appendChild(el('div', 'empty', '还没有角色卡。先点「① 车卡」。'));
    return;
  }

  const attrs = el('div', 'attrs');
  const order = [
    ['STR', '力量'], ['CON', '体质'], ['DEX', '敏捷'], ['APP', '外貌'],
    ['POW', '意志'], ['SIZ', '体型'], ['INT', '智力'], ['EDU', '教育'], ['LUCK', '幸运'],
  ];
  order.forEach(([k, cn]) => {
    const d = el('div', 'attr');
    d.appendChild(el('div', 'k', cn));
    d.appendChild(el('div', 'v', a[k] != null ? a[k] : '—'));
    attrs.appendChild(d);
  });
  [['HP', '生命', 'hp'], ['SAN', '理智', 'san'], ['MP', '魔法点', 'mp']].forEach(([k, cn, cls]) => {
    const d = el('div', 'attr ' + cls);
    d.appendChild(el('div', 'k', cn));
    d.appendChild(el('div', 'v', `${a[k] != null ? a[k] : '—'}/${a['MAX' + k] != null ? a['MAX' + k] : (k === 'SAN' ? 99 : '—')}`));
    attrs.appendChild(d);
  });
  box.appendChild(attrs);

  box.appendChild(el('div', 'sub-title', '技能'));
  const sk = el('div', 'skill-list');
  (seat.skills || []).forEach((s) => {
    const d = el('span', 'skill' + (s.value >= 60 ? ' hi' : ''), `${s.name} ${s.value}`);
    sk.appendChild(d);
  });
  if (!(seat.skills || []).length) sk.appendChild(el('span', 'hint', '—'));
  box.appendChild(sk);

  if ((seat.inventory || []).length) {
    box.appendChild(el('div', 'sub-title', '随身物品'));
    const inv = el('div', 'chronicle');
    seat.inventory.forEach((i) => inv.appendChild(el('div', 'item', esc(i))));
    box.appendChild(inv);
  }
  if (seat.backstory) {
    box.appendChild(el('div', 'sub-title', '背景'));
    box.appendChild(el('div', 'hint', esc(seat.backstory)));
  }

  if (seat.player_folder) {
    box.appendChild(el('div', 'sub-title', '这个人的文件夹'));
    box.appendChild(el('div', 'hint', seat.player_folder));
    if (seat.sheet_path) {
      box.appendChild(el('div', 'hint', '角色卡：' + seat.sheet_path));
    }
    const bar = el('div', 'row-actions');
    const b = el('button', 'btn tiny', '📂 打开文件夹');
    b.onclick = async () => {
      const r = await call('open_seat_folder', seat.seat_id);
      toast(r.ok ? '已打开：' + r.path : r.message, r.ok ? 'ok' : 'err');
    };
    bar.appendChild(b);
    box.appendChild(bar);
    box.appendChild(el('div', 'hint',
      '里面是：player.yaml（跨周目记忆）、memory\\（每局的记忆树）、' +
      'sheets\\（用 COC7 空白卡填出来的 Excel 角色卡）。'));
  }
}

const NODE_MARK = { confirmed: '✓', suspected: '?', open: '…', discarded: '×' };
const NODE_CN = { confirmed: '已确认', suspected: '在猜', open: '还没弄明白', discarded: '已排除' };

function renderTree(box, nodes) {
  if (!nodes || !nodes.length) {
    box.appendChild(el('div', 'hint',
      '思路树还没长出来。跑两轮，AI 就会把「确认了什么、在猜什么、还没弄明白什么」挂上来。'));
    return;
  }
  const wrap = el('div', 'tree');
  nodes.forEach((n) => {
    const d = el('div', 'tnode t-' + (n.state || 'open'));
    d.style.paddingLeft = (4 + (n.depth || 0) * 15) + 'px';
    d.appendChild(el('span', 'mark', NODE_MARK[n.state] || '·'));
    d.appendChild(el('span', 'txt', esc(n.text)));
    if (n.note) d.appendChild(el('span', 'note', '（' + esc(n.note) + '）'));
    if (n.source) d.appendChild(el('span', 'src', '⟨' + esc(n.source) + '⟩'));
    d.title = NODE_CN[n.state] || n.state;
    wrap.appendChild(d);
  });
  box.appendChild(wrap);
  const legend = el('div', 'hint');
  legend.textContent = '✓ 已确认　? 在猜　… 还没弄明白　× 已排除';
  box.appendChild(legend);
}

async function renderMemory(box, seat) {
  const mem = await call('seat_memory', seat.seat_id);
  if (!mem.ok) { box.appendChild(el('div', 'empty', mem.message || '还没有会话')); return; }
  const sit = mem.situation || {};

  if (sit.current_location) {
    box.appendChild(el('div', 'sub-title', '当前处境'));
    box.appendChild(el('div', 'hint', '位置：' + esc(sit.current_location)));
    if (sit.current_objective) box.appendChild(el('div', 'hint', '目标：' + esc(sit.current_objective)));
  }
  if ((sit.conditions || []).length) {
    box.appendChild(el('div', 'sub-title', '状态'));
    box.appendChild(el('div', 'hint', sit.conditions.join('；')));
  }
  const imp = sit.party_impressions || {};

  // ★ 认知树放在最前面——它才是"这个人现在在想什么"
  box.appendChild(el('div', 'sub-title',
    `思路（${(mem.tree || []).length} 个节点）`));
  renderTree(box, mem.tree || []);

  if (Object.keys(imp).length) {
    box.appendChild(el('div', 'sub-title', '对同伴的印象'));
    const c = el('div', 'chronicle');
    Object.entries(imp).forEach(([k, v]) => c.appendChild(el('div', 'item', `${k}：${v}`)));
    box.appendChild(c);
  }

  box.appendChild(el('div', 'sub-title', `编年史（${(mem.chronicle || []).length} 条）`));
  const ch = el('div', 'chronicle');
  (mem.chronicle || []).slice().reverse().forEach((e) => {
    const d = el('div', 'item t-' + (e.kind || 'event'));
    d.appendChild(el('span', 'm', `[第${e.turn}轮·${e.kind}] `));
    d.appendChild(el('span', null, esc(e.text)));
    ch.appendChild(d);
  });
  if (!(mem.chronicle || []).length) ch.appendChild(el('div', 'hint', '还没有沉淀。跑几轮就有了。'));
  box.appendChild(ch);
}

async function renderPlayerTab(box, seat) {
  box.appendChild(el('div', 'sub-title', '跨周目玩家档案'));
  box.appendChild(el('div', 'hint',
    '角色随模组结束就没了，但玩家会带着上一局的梗走进下一局。'));
  const r = await call('player_card_for_seat', seat.seat_id);
  if (!r.ok) { box.appendChild(el('div', 'empty', r.message || '还没有档案')); return; }
  box.appendChild(playerCardView(r.card));
}

function playerCardView(card) {
  const wrap = el('div');
  if (card.folder) {
    wrap.appendChild(el('div', 'sub-title', '文件夹'));
    wrap.appendChild(el('div', 'hint', card.folder));
    const bar = el('div', 'row-actions');
    const b = el('button', 'btn tiny', '📂 打开');
    b.onclick = async () => {
      const r = await call('open_player_folder', card.player_id);
      toast(r.ok ? '已打开：' + r.path : r.message, r.ok ? 'ok' : 'err');
    };
    bar.appendChild(b);
    wrap.appendChild(bar);
  }
  const kv = el('dl', 'kv');
  const st = card.stats || {};
  [['玩家', card.display_name], ['跑过', `${st.sessions_played || 0} 局`],
   ['角色', `${(card.characters || []).length} 个`],
   ['死亡', `${st.deaths || 0} 次`]].forEach(([k, v]) => {
    kv.appendChild(el('dt', null, k));
    kv.appendChild(el('dd', null, esc(v)));
  });
  wrap.appendChild(kv);

  if (card.table_voice) {
    wrap.appendChild(el('div', 'sub-title', '说话的样子'));
    wrap.appendChild(el('div', 'hint', esc(card.table_voice)));
  }
  if ((card.quirks || []).length) {
    wrap.appendChild(el('div', 'sub-title', '习惯'));
    wrap.appendChild(el('div', 'hint', card.quirks.map(esc).join('；')));
  }
  if ((card.memes || []).length) {
    wrap.appendChild(el('div', 'sub-title', `老梗（${card.memes.length}）`));
    card.memes.forEach((m) => {
      const d = el('div', 'meme');
      d.appendChild(el('span', null, '「' + esc(m.text) + '」'));
      if (m.origin) d.appendChild(el('span', 'k', m.origin));
      wrap.appendChild(d);
    });
  }
  if (card.relationships && Object.keys(card.relationships).length) {
    wrap.appendChild(el('div', 'sub-title', '对同桌人的看法'));
    const c = el('div', 'chronicle');
    Object.entries(card.relationships).forEach(([k, v]) =>
      c.appendChild(el('div', 'item', `${k}：${v}`)));
    wrap.appendChild(c);
  }
  if ((card.reflections || []).length) {
    wrap.appendChild(el('div', 'sub-title', '复盘'));
    const c = el('div', 'chronicle');
    card.reflections.slice().reverse().forEach((r) =>
      c.appendChild(el('div', 'item', esc(r))));
    wrap.appendChild(c);
  }
  if ((card.characters || []).length) {
    wrap.appendChild(el('div', 'sub-title', '生涯'));
    const c = el('div', 'chronicle');
    card.characters.slice().reverse().forEach((ch) => {
      c.appendChild(el('div', 'item',
        `${ch.name}（${ch.occupation}）· ${ch.module || '—'} · ${ch.fate || ''}`));
    });
    wrap.appendChild(c);
  }
  if ((card.sheets || []).length) {
    wrap.appendChild(el('div', 'sub-title', `角色卡文件（${card.sheets.length}）`));
    const c = el('div', 'chronicle');
    card.sheets.forEach((n) => c.appendChild(el('div', 'item', esc(n))));
    wrap.appendChild(c);
  }
  if ((card.memories || []).length) {
    wrap.appendChild(el('div', 'sub-title', `记忆文件（${card.memories.length}）`));
    const c = el('div', 'chronicle');
    card.memories.forEach((n) => c.appendChild(el('div', 'item', esc(n))));
    wrap.appendChild(c);
  }
  return wrap;
}

function renderCost(box, seat) {
  const tk = S.state && S.state.tokens;
  const ctx = S.state && S.state.context;
  if (!tk) { box.appendChild(el('div', 'empty', '还没有用量数据。')); return; }

  const rows = tk.seats || [];
  rows.forEach((r) => {
    const d = el('div', 'meme');
    d.appendChild(el('span', null, `${r.display_name}（${r.kind}）`));
    d.appendChild(el('span', 'k',
      `入 ${r.prompt_tokens.toLocaleString()} · 出 ${r.completion_tokens.toLocaleString()}` +
      (r.cached_tokens ? ` · 缓存 ${r.cached_tokens.toLocaleString()}` : '')));
    box.appendChild(d);
  });

  const total = el('div', 'hint');
  total.innerHTML = `合计输入 <b>${(tk.total_prompt || 0).toLocaleString()}</b> tokens，
    输出 <b>${(tk.total_completion || 0).toLocaleString()}</b>，
    其中缓存命中 <b>${(tk.total_cached || 0).toLocaleString()}</b>。<br>
    按当前单价估算：<b>${tk.cost_estimate} ${tk.currency || '元'}</b>`;
  box.appendChild(total);

  if (ctx && (ctx.rows || []).length) {
    box.appendChild(el('div', 'sub-title', '上下文体积（每轮要重发的量）'));
    ctx.rows.forEach((r) => {
      const d = el('div', 'item');
      d.textContent = `${r.name}：system ${r.system_chars} 字 + 历史 ${r.history_chars} 字（${r.history_msgs} 条）`;
      box.appendChild(d);
    });
    box.appendChild(el('div', 'hint',
      `当前规则详细度：${ctx.rules_detail}。历史是最大的开销项，` +
      `调小「保留历史轮数」最省钱。`));
  }
}

/* ══════════════════════════ 状态轮询 ══════════════════════════ */

async function refreshState() {
  try {
    const st = await call('state');
    const wasBusy = S.busy;
    S.state = st;
    S.busy = st.busy;

    $('roundNum').textContent = st.round != null ? st.round : 0;
    $('jobHint').textContent = st.busy ? '引擎正在工作…' : '';
    const studyMode = st.mode === 'study';
    $('sessionLabel').textContent = st.has_session
      ? (studyMode
        ? `研读室 · ${st.module_title || '自由跑团'}`
        : `${st.session_id} · ${st.module_title || '自由跑团'} · 第 ${st.round} 轮`)
      : '尚未建立会话';
    $('sceneBar').textContent = studyMode
      ? `📖 研读室：桌上只有你和守秘人 ·《${st.module_title || ''}》`
      : (st.scene_id ? `当前场景：${sceneTitle(st.scene_id)}` : '尚未开局');
    document.body.classList.toggle('study-mode', studyMode);
    const cb = $('clockBadge');
    if (cb) {
      cb.textContent = st.clock_short ? `🕰 ${st.clock_short}` : '';
      cb.title = st.clock || '桌面上现在是什么时候';
    }

    const btns = ['btnPrepare', 'btnStart', 'btnAuto', 'btnStep', 'btnStop', 'btnFinish'];
    btns.forEach((id) => { const b = $(id); if (b) b.disabled = !!st.busy; });

    renderSeats();
    renderCurrentGame();
    if (S.rightTab === 'cost' || S.rightTab === 'dice') renderRight();
    if (wasBusy && !st.busy) toast('引擎空闲了', 'ok');
  } catch (e) {
    $('jobHint').textContent = '状态读取失败：' + e.message;
  }
}

function sceneTitle(id) {
  const sc = (S.state && S.state.scenes) || [];
  const hit = sc.find((x) => x.id === id);
  return hit ? hit.title : id;
}

async function pollEvents() {
  try {
    const evts = await call('poll_events');
    if (evts && evts.length) {
      // 「debug」是给插件看的旁路事件，不进主流水（不然会刷屏）
      const shown = evts.filter((e) => e.type !== 'debug');
      if (shown.length) {
        shown.forEach(addEntry);
        if (shown.some((e) => e.type === 'chargen' || e.type === 'scene')) {
          refreshState();
        }
      }
      dispatchToPlugins(evts);
    }
  } catch (e) { /* 桥接未就绪时静默 */ }
}

/* ══════════════════════════ 设置弹窗 ══════════════════════════ */

const OPTION_DEFS = [
  { key: 'rules_detail', label: '规则注入详细度', type: 'select',
    options: [['lean', '精简（推荐·最省 token）'],
              ['standard', '标准（常驻战斗+理智）'],
              ['full', '完整（再加恢复与自备规则）']],
    hint: '规则默认只常驻判定要点，术语在出现时才补一句话定义。' },
  { key: 'history_keep', label: '保留历史轮数', type: 'number', min: 2, max: 40,
    hint: '★ 最大的成本项。调小最省钱，但人设连续性会变弱。' },
  { key: 'memory_topk', label: '记忆检索条数', type: 'number', min: 3, max: 30,
    hint: '每轮从编年史里挑多少条注入。' },
  { key: 'max_rounds', label: '回合上限', type: 'number', min: 1, max: 500,
    hint: '自动推进到此为止，防费用失控。' },
  { key: 'turn_delay_ms', label: '回合间隔(ms)', type: 'number', min: 0, max: 5000 },
  { key: 'linter_retry', label: '元层重写次数', type: 'number', min: 0, max: 3,
    hint: '输出跑偏成助手腔时，静默重写几次。' },
  { key: 'table_talk', label: '桌边点名对话', type: 'bool',
    hint: '回合之间插一轮纯对话，让「A 喊人 / B 说我角色不知道」这种来回真的发生。' },
  { key: 'table_talk_exchanges', label: '桌边来回次数', type: 'number', min: 0, max: 4,
    hint: '0 = 关闭（最省 token），2 = 默认。' },
  { key: 'parallel_pl', label: '多玩家并行生成', type: 'bool' },
  { key: 'retrospective', label: '散场后复盘（跨周目记忆）', type: 'bool',
    hint: '关掉的话，AI 下局就记不住这一局了。' },
  { key: 'combat_by_dex', label: '战斗按 DEX 顺序', type: 'bool' },
];

function providerOf(id) { return S.providers[id] || {}; }

/** 按座位 id 找回**当前在文档里**的模型输入框。
 *  面板重渲染之后，之前抓到的节点就是孤儿了，得重新查。 */
function findSeatModelField(seatId) {
  const box = document.querySelector(
    `#seatEditors .seat-editor[data-seat-id="${seatId}"]`);
  const input = box && box.querySelector('[data-k="model"]');
  if (!input) return {};
  return { box, input, field: input.parentElement };
}

/** 测试用：把真正的 fetch_models 结果铺到某个座位上（绕开网络）。
 *  刻意是同步的：evaluate_js 拿不到 Promise 的值，异步函数会测不出来。 */
function applyModelsToSeat(seatId, models) {
  const f = findSeatModelField(seatId);
  if (!f.input) return 0;
  installModelList(f.field, f.input, models);
  return f.box.querySelectorAll('.model-pick').length;
}

function buildSeatEditor(seat, idx) {
  const box = el('div', 'seat-editor ' + (seat.kind === 'KP' ? 'kp' : 'pl'));
  // 记住这个盒子属于哪个座位：拉完模型之后要按 id 重新找回**当前**的输入框
  // （saveConfig 会整块重渲染，之前抓到手的节点就成孤儿了）
  box.dataset.seatId = seat.seat_id;
  const head = el('div', 'head');
  head.appendChild(el('b', null,
    (seat.kind === 'KP' ? '🔒 守秘人' : `🎭 玩家 ${idx}`) + `　${seat.seat_id}`));
  const en = el('input');
  en.type = 'checkbox'; en.checked = seat.enabled !== false;
  en.dataset.k = 'enabled'; en.dataset.seat = seat.seat_id;
  const enLab = el('label', 'inline-field');
  enLab.appendChild(en); enLab.appendChild(el('span', null, '启用'));
  head.appendChild(enLab);
  head.appendChild(el('span', 'spacer'));
  const testBtn = el('button', 'btn tiny', '测试连接');
  testBtn.onclick = async () => {
    testBtn.disabled = true; testBtn.textContent = '测试中…';
    try {
      await saveConfig(true);
      const r = await call('probe', seat.seat_id);
      toast(`${seat.display_name}：${r.message}`, r.ok ? 'ok' : 'err');
    } catch (e) { toast('测试失败：' + e.message, 'err'); }
    testBtn.disabled = false; testBtn.textContent = '测试连接';
  };
  head.appendChild(testBtn);
  if (seat.kind === 'PL') {
    const del = el('button', 'btn tiny danger', '删除');
    del.onclick = async () => {
      if (!confirm(`删除席位 ${seat.seat_id}？`)) return;
      S.cfg = await call('remove_pl_seat', seat.seat_id);
      renderSettings();
    };
    head.appendChild(del);
  }
  box.appendChild(head);

  const g = el('div', 'grid-2');

  const mk = (label, key, value, opts = {}) => {
    const f = el('div', 'field' + (opts.span2 ? ' span-2' : ''));
    f.appendChild(el('span', null, label));
    let inp;
    if (opts.type === 'select') {
      inp = el('select');
      (opts.options || []).forEach(([v, t]) => {
        const o = el('option', null, t); o.value = v;
        if (v === value) o.selected = true;
        inp.appendChild(o);
      });
    } else {
      inp = el('input');
      inp.type = opts.type || 'text';
      if (opts.min != null) inp.min = opts.min;
      if (opts.max != null) inp.max = opts.max;
      if (opts.step != null) inp.step = opts.step;
      inp.value = value == null ? '' : value;
      if (opts.placeholder) inp.placeholder = opts.placeholder;
    }
    inp.dataset.k = key;
    inp.dataset.seat = seat.seat_id;
    inp.dataset.kind = seat.kind;
    inp.oninput = () => { if (key === 'api_key') S.dirtyKeys.add(seat.seat_id); };
    inp.onchange = () => { if (key === 'api_key') S.dirtyKeys.add(seat.seat_id); };
    f.appendChild(inp);
    g.appendChild(f);
    return inp;
  };

  // 提供方
  const provSel = mk('接口', 'provider', seat.provider || 'deepseek', {
    type: 'select',
    options: Object.entries(S.providers).map(([k, v]) => [k, v.label]),
  });
  provSel.onchange = () => {
    const p = providerOf(provSel.value);
    const ed = box;
    const baseInput = ed.querySelector('[data-k="base_url"]');
    const modelInput = ed.querySelector('[data-k="model"]');
    if (baseInput && p.base_url) baseInput.value = p.base_url;
    if (modelInput && modelInput.tagName === 'INPUT' && (p.models || []).length) {
      modelInput.value = p.models[0];
    }
    updateModelList(box, provSel.value);
    toast(`已切到「${p.label}」`);
  };

  mk('Base URL', 'base_url', seat.base_url || '', {
    span2: true, placeholder: 'https://api.deepseek.com/v1',
  });
  const modelInput = mk('模型', 'model', seat.model || '', { placeholder: 'deepseek-chat' });
  // 模型名最容易填错，给一个"拉列表"的按钮，点着选
  const modelField = modelInput.parentElement;
  // 上次拉到过的先摆上（不用联网），拉取按钮再刷新一遍
  const cached = cachedModels(seat.base_url || '');
  if (cached.length) installModelList(modelField, modelInput, cached);
  const fetchBtn = el('button', 'btn tiny', '🔍 拉取模型列表');
  fetchBtn.style.marginTop = '4px';
  fetchBtn.onclick = async () => {
    fetchBtn.disabled = true;
    fetchBtn.textContent = '拉取中…';
    try {
      await saveConfig(true);        // 先把刚填的地址和 Key 存下去，服务端才知道用哪个
      const r = await call('fetch_models', seat.seat_id);
      if (!r.ok) { toast(r.message, 'err'); return; }
      // ★ 上面那句 saveConfig 会调 renderSettings() 把整个面板重渲染一遍，
      //   手里这个 modelField / modelInput 已经变成不在文档里的孤儿节点了——
      //   往它上面挂东西当然什么都看不见。所以按座位 id 重新取一次活的。
      const live = findSeatModelField(seat.seat_id);
      installModelList(live.field || modelField, live.input || modelInput, r.models);
      toast(`拉到 ${r.count} 个模型，点下面的名字就能选`, 'ok');
    } catch (e) {
      reportClientError('拉取模型列表失败', e && e.stack);
      toast('拉取失败：' + e.message, 'err');
    } finally {
      fetchBtn.disabled = false;
      fetchBtn.textContent = '🔍 拉取模型列表';
    }
  };
  modelField.appendChild(fetchBtn);
  mk('API Key' + (seat.api_key_set ? '（已保存，留空即不修改）' : ''), 'api_key',
    seat.api_key || '', { type: 'password', span2: true, placeholder: 'sk-...' });

  const p = providerOf(seat.provider || 'deepseek');
  if (p.hint) {
    const h = el('div', 'hint');
    h.style.gridColumn = 'span 2';
    h.textContent = p.hint;
    g.appendChild(h);
  }

  if (seat.kind === 'PL') {
    const prof = seat.profile || {};
    mk('玩家名', 'player_name', prof.player_name || '', { placeholder: '老周' });
    mk('演员风格', 'playstyle', prof.playstyle || '', { placeholder: '慎重解密流' });
    const v = mk('桌边说话的样子', 'table_voice', prof.table_voice || '',
      { span2: true, placeholder: '爱吐槽、说话短、掷骰前先算概率' });
    v.dataset.deep = 'profile';
    // 桌边话量。只调说多少，说话规矩哪一档都一样。
    const lv = String(prof.table_energy || 3);
    mk('桌边话量', 'table_energy', lv, {
      type: 'select',
      options: [
        ['1', '1 很少说话'],
        ['2', '2 偏安静'],
        ['3', '3 正常'],
        ['4', '4 话偏多'],
        ['5', '5 话最多'],
      ],
    });
  }

  const temp = mk('temperature', 'temperature', seat.temperature != null ? seat.temperature : 0.85,
    { type: 'number', min: 0, max: 2, step: 0.05 });
  const mt = mk('max_tokens', 'max_tokens', seat.max_tokens || 1400,
    { type: 'number', min: 128, max: 8192, step: 64 });

  box.appendChild(g);
  return box;
}

/** 从配置里读某个端点缓存过的模型名。 */
function cachedModels(baseUrl) {
  const key = (baseUrl || '').trim().replace(/\/+$/, '').toLowerCase()
    .replace(/\/chat\/completions$/, '');
  const cache = (S.cfg && S.cfg.ui && S.cfg.ui.model_cache) || {};
  return cache[key] || [];
}

/** 把拉到的模型名就地铺成可点的按钮。
 *
 * 原来只用 <datalist>：Chromium 里它不会点一下就弹（要聚焦再按方向键、
 * 或者手打几个字），WebView2 里更不听话——用户点了「拉取模型列表」，
 * 除了一个 toast 什么都没发生。所以直接铺一排按钮出来，点了就填进去。
 * datalist 仍然留着，想手打的时候还能自动补全。
 */
function installModelList(field, input, models) {
  if (!field || !input) return;
  field.querySelectorAll('.model-picks').forEach((n) => n.remove());

  const box = el('div', 'model-picks');
  box.dataset.models = String((models || []).length);
  (models || []).forEach((m) => {
    const b = el('button', 'model-pick' + (input.value === m ? ' on' : ''), m);
    b.type = 'button';
    b.onclick = (e) => {
      e.preventDefault();
      input.value = m;
      box.querySelectorAll('.model-pick').forEach((x) => x.classList.remove('on'));
      b.classList.add('on');
    };
    box.appendChild(b);
  });
  field.appendChild(box);

  let list = field.querySelector('datalist');
  if (!list) {
    list = el('datalist');
    list.id = 'models-' + Math.random().toString(36).slice(2, 8);
    field.appendChild(list);
    input.setAttribute('list', list.id);
  }
  list.innerHTML = '';
  (models || []).forEach((m) => {
    const o = el('option');
    o.value = m;
    list.appendChild(o);
  });
  input.placeholder = `共 ${(models || []).length} 个，点下面的名字就能选`;
}

function updateModelList(box, providerId) {
  const p = providerOf(providerId) || {};
  const input = box.querySelector('[data-k="model"]');
  if (!input) return;
  let list = box.querySelector('datalist[data-models]');
  if (!list) {
    list = el('datalist');
    list.setAttribute('data-models', '1');
    list.id = 'models-' + Math.random().toString(36).slice(2, 8);
    box.appendChild(list);
    input.setAttribute('list', list.id);
  }
  list.innerHTML = '';
  (p.models || []).forEach((m) => {
    const o = el('option'); o.value = m; list.appendChild(o);
  });
}

function renderSettings() {
  const cfg = S.cfg;
  S.providers = cfg._providers || {};
  $('modulesPath').textContent = (cfg._paths && cfg._paths.modules_root) || 'data/modules';

  const kpSeats = (cfg.seats || []).filter((s) => s.kind === 'KP');
  const plSeats = (cfg.seats || []).filter((s) => s.kind === 'PL');
  $('plCount').value = plSeats.length;

  const box = $('seatEditors');
  box.innerHTML = '';
  kpSeats.forEach((s) => box.appendChild(buildSeatEditor(s, 0)));
  plSeats.forEach((s, i) => box.appendChild(buildSeatEditor(s, i + 1)));

  // 全局选项
  const ob = $('optionEditors');
  ob.innerHTML = '';
  const opts = (cfg.options || {});
  OPTION_DEFS.forEach((d) => {
    const f = el('div', 'field');
    f.appendChild(el('span', null, d.label));
    let inp;
    if (d.type === 'bool') {
      inp = el('input'); inp.type = 'checkbox'; inp.checked = !!opts[d.key];
    } else if (d.type === 'select') {
      inp = el('select');
      (d.options || []).forEach(([v, t]) => {
        const o = el('option', null, t); o.value = v;
        if (opts[d.key] === v) o.selected = true;
        inp.appendChild(o);
      });
    } else {
      inp = el('input'); inp.type = 'number';
      inp.min = d.min; inp.max = d.max;
      inp.value = opts[d.key] != null ? opts[d.key] : '';
    }
    inp.dataset.opt = d.key;
    inp.dataset.type = d.type;
    f.appendChild(inp);
    if (d.hint) f.appendChild(el('div', 'hint', d.hint));
    ob.appendChild(f);
  });

  // 单价
  const pr = cfg.pricing || {};
  [['input', '输入 (元/百万 token)'], ['input_cached', '缓存命中输入'],
   ['output', '输出']].forEach(([k, label]) => {
    const f = el('div', 'field');
    f.appendChild(el('span', null, label));
    const inp = el('input');
    inp.type = 'number'; inp.step = 0.01; inp.min = 0;
    inp.value = pr[k] != null ? pr[k] : '';
    inp.dataset.price = k;
    f.appendChild(inp);
    ob.appendChild(f);
  });

  $('costHint').innerHTML =
    '单价只用于界面上的费用估算，不影响任何实际计费。' +
    'DeepSeek 官方当前价格可到 platform.deepseek.com 查看后自行填写。';
}

function collectConfig() {
  const cfg = JSON.parse(JSON.stringify(S.cfg));
  const seats = [];

  // ★ 必须限定在设置面板里（#seatEditors）。
  //   「名册与站位」用的是同一套 .seat-editor 样式，一旦打开过，
  //   那些节点就一直留在 DOM 里；这里要是用全局选择器，
  //   名册那边的盒子没有 [data-k="provider"]，取 .dataset 会直接抛异常——
  //   于是「保存设置」和「拉取模型列表」一起失效，填好的 Key 也存不进去。
  document.querySelectorAll('#seatEditors .seat-editor').forEach((box) => {
    const provBox = box.querySelector('[data-k="provider"]');
    if (!provBox) return;
    const orig = (S.cfg.seats || []).find(
      (x) => x.seat_id === provBox.dataset.seat);
    if (!orig) return;
    const seat = JSON.parse(JSON.stringify(orig));
    box.querySelectorAll('[data-k]').forEach((inp) => {
      const k = inp.dataset.k;
      let v;
      if (inp.type === 'checkbox') v = inp.checked;
      else if (inp.type === 'number') v = inp.value === '' ? null : Number(inp.value);
      else v = inp.value;
      if (k === 'provider' || k === 'base_url' || k === 'model' ||
          k === 'temperature' || k === 'max_tokens' || k === 'enabled') {
        seat[k] = v;
      } else if (k === 'api_key') {
        const touched = S.dirtyKeys.has(seat.seat_id);
        if (!touched) seat.api_key = KEEP;
        else seat.api_key = v;
      } else {
        seat.profile = seat.profile || {};
        seat.profile[k] = v;
      }
    });
    if (typeof seat.temperature === 'number') seat.temperature = Number(seat.temperature);
    if (typeof seat.max_tokens === 'number') seat.max_tokens = Number(seat.max_tokens);
    seats.push(seat);
  });
  cfg.seats = seats;

  cfg.options = cfg.options || {};
  document.querySelectorAll('[data-opt]').forEach((inp) => {
    const k = inp.dataset.opt;
    if (inp.dataset.type === 'bool') cfg.options[k] = inp.checked;
    else if (inp.dataset.type === 'number') cfg.options[k] = Number(inp.value);
    else cfg.options[k] = inp.value;
  });

  cfg.pricing = cfg.pricing || {};
  document.querySelectorAll('[data-price]').forEach((inp) => {
    cfg.pricing[inp.dataset.price] = Number(inp.value);
  });

  delete cfg._providers;
  delete cfg._paths;
  return cfg;
}

async function saveConfig(silent) {
  const cfg = collectConfig();
  const saved = await call('save_config', cfg);
  S.cfg = saved;
  S.dirtyKeys.clear();
  if (!silent) toast('设置已保存', 'ok');
  renderSettings();
  await refreshState();
  return saved;
}

/* ══════════════════════════ 出错记录 ══════════════════════════ */

async function refreshErrors() {
  try {
    const r = await call('recent_errors', 40);
    $('errLogPath').textContent = r.path || '';
    const body = (r.tail || '').trim();
    $('errLogBody').textContent = body || '（干净，没出过错）';
  } catch (e) {
    $('errLogBody').textContent = '读不到出错记录：' + e.message;
  }
}

/* ══════════════════════════ 名册与站位 ══════════════════════════ */

let ROSTER = { entries: [], keeper: '', players: [] };

const RIGOR_LABEL = {
  1: '全凭感觉', 2: '不太看规则', 3: '记得七七八八', 4: '比较熟', 5: '人形规则书',
};

async function refreshRoster() {
  ROSTER = await call('get_roster');
  renderRoster();
}

function renderRoster() {
  const kpSel = $('kpSelect');
  kpSel.innerHTML = '';
  ROSTER.entries.forEach((e) => {
    const o = el('option', null, `${e.handle}（${e.gender}）`);
    o.value = e.id;
    if (e.id === ROSTER.keeper) o.selected = true;
    kpSel.appendChild(o);
  });
  $('plCount2').value = (ROSTER.players || []).filter(Boolean).length;

  renderPlPicker();
  renderRosterEditors();

  const kpName = (ROSTER.entries.find((e) => e.id === ROSTER.keeper) || {}).handle || '—';
  $('rosterHint').textContent =
    `当前：${kpName} 当守秘人，${(ROSTER.players || []).length} 人当玩家。`;
}

function renderPlPicker() {
  const box = $('plPicker');
  box.innerHTML = '';
  const kp = $('kpSelect').value;
  (ROSTER.entries || []).forEach((e) => {
    const on = (ROSTER.players || []).includes(e.id);
    const chip = el('div', 'player-chip' + (on ? ' active' : ''));
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = on;
    cb.dataset.pid = e.id;
    if (e.id === kp) { cb.checked = false; cb.disabled = true; }
    chip.appendChild(cb);
    chip.appendChild(el('span', 'n', e.handle));
    chip.appendChild(el('span', 'm',
      `${e.gender} · 严谨度${e.rigor}（${RIGOR_LABEL[e.rigor] || '?'}）· ${e.tagline || ''}`));
    chip.onclick = (ev) => {
      if (cb.disabled || ev.target === cb) return;
      cb.checked = !cb.checked;
      chip.classList.toggle('active', cb.checked);
      syncCount();
    };
    cb.onchange = () => { chip.classList.toggle('active', cb.checked); syncCount(); };
    box.appendChild(chip);
  });
  $('kpSelect').onchange = () => renderPlPicker();
}

function syncCount() {
  const n = Array.from(document.querySelectorAll('#plPicker input:checked')).length;
  $('plCount2').value = n;
}

function renderRosterEditors() {
  const box = $('rosterEditors');
  box.innerHTML = '';
  (ROSTER.entries || []).forEach((e) => {
    const d = el('div', 'seat-editor ' + (e.default_role === 'KP' ? 'kp' : 'pl'));
    const head = el('div', 'head');
    head.appendChild(el('b', null, `${e.handle} → ${e.display || e.short}`));
    head.appendChild(el('span', 'pill', e.gender));
    head.appendChild(el('span', 'pill', '严谨度 ' + e.rigor));
    if (e.default_role === 'KP') head.appendChild(el('span', 'pill ok', '默认 KP'));
    d.appendChild(head);

    const g = el('div', 'grid-2');
    const mk = (label, field, value, opts = {}) => {
      const f = el('div', 'field' + (opts.span2 ? ' span-2' : ''));
      f.appendChild(el('span', null, label));
      let inp;
      if (opts.select) {
        inp = el('select');
        opts.select.forEach((v) => {
          const o = el('option', null, v); o.value = v;
          if (v === value) o.selected = true;
          inp.appendChild(o);
        });
      } else {
        inp = el('input');
        inp.type = opts.type || 'text';
        if (opts.min != null) inp.min = opts.min;
        if (opts.max != null) inp.max = opts.max;
        inp.value = value == null ? '' : value;
      }
      inp.dataset.pid = e.id;
      inp.dataset.field = field;
      f.appendChild(inp);
      g.appendChild(f);
    };
    mk('网名', 'handle', e.handle, { span2: true });
    mk('简称（桌上怎么喊）', 'short', e.short);
    mk('性别', 'gender', e.gender, { select: ['男', '女'] });
    mk('严谨度（1-5）', 'rigor', e.rigor, { type: 'number', min: 1, max: 5 });
    mk('一句话人设', 'tagline', e.tagline, { span2: true });
    mk('在桌边怎么说话', 'voice', e.voice, { span2: true });
    mk('玩的风格', 'playstyle', e.playstyle);
    mk('桌上的习惯（用；分隔）', 'habits', (e.habits || []).join('；'), { span2: true });
    d.appendChild(g);
    box.appendChild(d);
  });
}

function collectRoster() {
  const byId = {};
  (ROSTER.entries || []).forEach((e) => { byId[e.id] = { ...e }; });
  document.querySelectorAll('#rosterEditors [data-pid]').forEach((inp) => {
    const e = byId[inp.dataset.pid];
    if (!e) return;
    const f = inp.dataset.field;
    if (f === 'rigor') e.rigor = Number(inp.value) || 3;
    else if (f === 'habits') {
      e.habits = inp.value.split(/[；;、,，]/).map((x) => x.trim()).filter(Boolean);
    } else e[f] = inp.value;
    delete e.rigor_note;
  });
  return Object.values(byId);
}

/* ══════════════════════════ 开新团向导 ══════════════════════════ */

const NEW = { module: '', premise: '', keeper: '', players: [] };

async function openNewGame() {
  const b = await call('bootstrap');
  S.modules = b.modules || [];
  if (b.roster) ROSTER = b.roster;
  S.cfg = b.config;
  S.providers = S.cfg._providers || {};
  $('repoPath').textContent = (S.cfg._paths && S.cfg._paths.modules_root) || '';

  // 预填：沿用当前站位，没选过模组就默认第一个
  NEW.keeper = NEW.keeper || ROSTER.keeper || '';
  if (!NEW.players.length) NEW.players = (ROSTER.players || []).filter(Boolean);
  if (!NEW.module) {
    const first = S.modules.find((m) => !m.error);
    NEW.module = first ? first.id : '';
  }
  $('premiseInput2').value = NEW.premise;

  renderNewGame();
  openModal('modalNewGame');
}

function renderNewGame() {
  // ① 模组
  const box = $('newGameModules');
  box.innerHTML = '';
  const freeCard = el('div', 'module-card' + (NEW.module === '' ? ' selected' : ''));
  freeCard.appendChild(el('div', 't', '不选模组 · 自由跑团'));
  freeCard.appendChild(el('div', 's', '世界由守秘人现场搭。在下面写一句开场设定。'));
  freeCard.onclick = () => { NEW.module = ''; renderNewGame(); };
  box.appendChild(freeCard);

  (S.modules || []).forEach((m) => {
    const c = el('div', 'module-card' + (NEW.module === m.id ? ' selected' : ''));
    c.appendChild(el('div', 't', esc(m.title || m.id)));
    if (m.summary) c.appendChild(el('div', 's', esc(m.summary)));
    c.appendChild(el('div', 'm',
      `${m.loose_file ? '单文件' : '文件夹'} · ${m.scene_count || 0} 幕 · ` +
      `${m.handout_count || 0} 份 handout · ` +
      (m.has_truth ? '含幕后真相 ✓' : '⚠ 缺 secret_truth.md')));
    (m.warnings || []).forEach((w) => c.appendChild(el('div', 'warns', w)));
    c.onclick = () => { NEW.module = m.id; renderNewGame(); };
    box.appendChild(c);
  });

  // ② 人
  const kpSel = $('kpSelect2');
  kpSel.innerHTML = '';
  (ROSTER.entries || []).forEach((e) => {
    const o = el('option', null, `${e.handle}（${e.gender}）`);
    o.value = e.id;
    if (e.id === NEW.keeper) o.selected = true;
    kpSel.appendChild(o);
  });
  kpSel.onchange = () => {
    NEW.keeper = kpSel.value;
    NEW.players = NEW.players.filter((p) => p !== NEW.keeper);
    renderNewGame();
  };

  const picker = $('plPicker2');
  picker.innerHTML = '';
  (ROSTER.entries || []).forEach((e) => {
    const on = NEW.players.includes(e.id);
    const chip = el('div', 'player-chip' + (on ? ' active' : ''));
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = on;
    cb.dataset.pid = e.id;
    if (e.id === NEW.keeper) { cb.checked = false; cb.disabled = true; }
    chip.appendChild(cb);
    chip.appendChild(el('span', 'n', e.handle));
    chip.appendChild(el('span', 'm',
      `${e.gender} · 严谨度${e.rigor}（${RIGOR_LABEL[e.rigor] || '?'}）· ${e.tagline || ''}`));
    const toggle = () => {
      if (cb.disabled) return;
      cb.checked = !cb.checked;
      chip.classList.toggle('active', cb.checked);
      syncNewPlayers();
    };
    chip.onclick = (ev) => { if (ev.target !== cb) toggle(); };
    cb.onchange = () => { chip.classList.toggle('active', cb.checked); syncNewPlayers(); };
    picker.appendChild(chip);
  });
  $('plCount3').value = NEW.players.length;

  // ③ 确认
  const kp = (ROSTER.entries || []).find((e) => e.id === NEW.keeper);
  const names = NEW.players
    .map((p) => ((ROSTER.entries || []).find((e) => e.id === p) || {}).handle)
    .filter(Boolean);
  const mod = (S.modules || []).find((m) => m.id === NEW.module);
  $('newGameSummary').innerHTML =
    `<b>守秘人：</b>${esc(kp ? kp.handle : '（未选）')}<br>` +
    `<b>玩家：</b>${names.length ? esc(names.join('、')) : '（一个都没有）'}<br>` +
    `<b>剧本：</b>${mod ? esc(mod.title) : '自由跑团'}` +
    (mod && !mod.has_truth ? '<br><span style="color:#e5b567">⚠ 这个模组没有幕后真相文件，' +
      '守秘人手里会是空的，跑起来会比较飘。</span>' : '');
}

function syncNewPlayers() {
  NEW.players = Array.from(document.querySelectorAll('#plPicker2 input:checked'))
    .map((c) => c.dataset.pid);
  $('plCount3').value = NEW.players.length;
  const kp = (ROSTER.entries || []).find((e) => e.id === NEW.keeper);
  const names = NEW.players
    .map((p) => ((ROSTER.entries || []).find((e) => e.id === p) || {}).handle)
    .filter(Boolean);
  const mod = (S.modules || []).find((m) => m.id === NEW.module);
  $('newGameSummary').innerHTML =
    `<b>守秘人：</b>${esc(kp ? kp.handle : '（未选）')}<br>` +
    `<b>玩家：</b>${names.length ? esc(names.join('、')) : '（一个都没有）'}<br>` +
    `<b>剧本：</b>${mod ? esc(mod.title) : '自由跑团'}`;
}

function renderCurrentGame() {
  const box = $('currentGame');
  const st = S.state;
  if (!st || !st.has_session) {
    box.innerHTML = '还没有团。点上面 <b>🎬 开新团</b> 起一局。';
    return;
  }
  const kpSeat = (st.seats || []).find((s) => s.kind === 'KP');
  const pls = (st.seats || []).filter((s) => s.kind === 'PL');
  const phase = {
    setup: '还没车卡', chargen: '车卡中', running: '进行中',
    paused: '已暂停', ended: '已结束',
  }[st.phase] || st.phase;
  box.innerHTML =
    `<b>${esc(st.module_title || '自由跑团')}</b> · 第 ${st.round} 轮 · ${phase}<br>` +
    `守秘人 <b>${esc(kpSeat ? kpSeat.player_name || kpSeat.display_name : '—')}</b>；` +
    `玩家 ${pls.length} 人`;
}

/* ══════════════════════════ 模组弹窗 ══════════════════════════ */

async function refreshModules() {
  const b = await call('bootstrap');
  S.modules = b.modules || [];
  S.players = b.players || [];
  S.cfg = b.config;
  S.providers = S.cfg._providers || {};
  $('modulesPath').textContent = (S.cfg._paths && S.cfg._paths.modules_root) || 'data/modules';
  renderModules();
}

function renderModules() {
  const box = $('moduleList');
  box.innerHTML = '';
  if (!S.modules.length) {
    box.appendChild(el('div', 'empty',
      '还没有模组。把剧本文件夹丢进上面那个目录，然后点刷新。'));
    return;
  }
  S.modules.forEach((m) => {
    const c = el('div', 'module-card' + (S.selectedModule === m.id ? ' selected' : ''));
    c.appendChild(el('div', 't', esc(m.title || m.id)));
    if (m.summary) c.appendChild(el('div', 's', esc(m.summary)));
    c.appendChild(el('div', 'm',
      `${m.scene_count || 0} 幕 · ${m.handout_count || 0} 份 handout · ` +
      `${m.has_truth ? '含幕后真相 ✓' : '⚠ 缺 secret_truth.md'} · ${m.players || '人数未标注'}`));
    if ((m.scenes || []).length) {
      c.appendChild(el('div', 'scenes',
        m.scenes.map((s) => s.title).join(' → ')));
    }
    (m.warnings || []).forEach((w) => c.appendChild(el('div', 'warns', w)));
    c.onclick = () => { S.selectedModule = m.id; renderModules(); };
    box.appendChild(c);
  });
}

/* ══════════════════════════ 研读室 ══════════════════════════ */

let STUDIES = [];

async function openStudyRoom() {
  openModal('modalStudy');
  const b = await call('bootstrap');
  S.modules = b.modules || S.modules;
  STUDIES = b.studies || [];
  renderStudyPicker();
  await syncStudyRoom();
}

function renderStudyPicker() {
  const sel = $('studyModuleSel');
  const keep = sel.value || S.selectedModule;
  sel.innerHTML = '';
  if (!S.modules.length) {
    sel.appendChild(el('option', null, '（data/modules 里还没有模组）'));
    sel.disabled = true;
    return;
  }
  sel.disabled = false;
  S.modules.forEach((m) => {
    const o = el('option', null, `${m.title || m.id}　(${m.scene_count || 0} 幕)`);
    o.value = m.id;
    sel.appendChild(o);
  });
  if (keep && S.modules.some((m) => m.id === keep)) sel.value = keep;
  renderStudySaved();
}

function renderStudySaved() {
  const id = $('studyModuleSel').value;
  const hit = STUDIES.find((s) => s.module_id === id);
  const h = $('studySaved');
  if (hit) {
    h.textContent = `✓ 这个本 ${hit.keeper || '某位 KP'} 已经读过`
      + `（${(hit.updated_at || '').slice(0, 16)}）——开真局会直接复用。`;
  } else {
    h.textContent = '这个本还没读过。';
  }
}

async function syncStudyRoom() {
  const st = await call('study_state');
  const rep = $('studyReport');
  const talk = $('studyTalk');

  if (!st.open) {
    rep.className = 'study-report empty';
    rep.textContent = '还没读过。挑一个模组，点「让 KP 读一遍」。';
  } else {
    if (st.report) {
      rep.className = 'study-report';
      rep.textContent = st.report;
    } else if (st.busy) {
      rep.className = 'study-report empty';
      rep.textContent = '守秘人正在读……（这个本越厚越慢）';
    }
    talk.innerHTML = '';
    (st.talk || []).forEach((t) => {
      const line = el('div', 'study-line' + (t.who === '导演' ? ' me' : ''));
      line.appendChild(el('span', 'who', esc(t.who || '？')));
      line.appendChild(el('span', 'said', esc(t.text || '')));
      talk.appendChild(line);
    });
    if (!(st.talk || []).length && st.report) {
      talk.appendChild(el('div', 'empty', '还没有问过它什么。'));
    }
  }
  const busy = !!st.busy;
  ['btnRunStudy', 'btnRereadStudy', 'btnForgetStudy', 'btnStudyAsk']
    .forEach((id) => { const b = $(id); if (b) b.disabled = busy; });
}

async function runStudy(force) {
  const id = $('studyModuleSel').value;
  if (!id) { toast('先挑一个模组', 'err'); return; }
  S.selectedModule = id;
  const r = await call('study_room', id, !!force);
  toast(r.ok ? '研读室开起来了，它正在读' : r.message, r.ok ? 'ok' : 'err');
  if (r.ok) { await syncStudyRoom(); setTimeout(syncStudyRoom, 1200); }
}

async function askStudy() {
  const box = $('studyAskInput');
  const q = (box.value || '').trim();
  if (!q) return;
  const r = await call('study_ask', q);
  if (!r.ok) { toast(r.message, 'err'); return; }
  box.value = '';
  await syncStudyRoom();
}

/* ══════════════════════════ 玩家档案弹窗 ══════════════════════════ */

async function refreshPlayers() {
  S.players = await call('player_cards');
  renderPlayers();
}

function renderPlayers() {
  const list = $('playerList');
  const detail = $('playerDetail');
  list.innerHTML = '';
  detail.innerHTML = '';
  if (!S.players.length) {
    list.appendChild(el('div', 'empty',
      '还没有玩家档案。跑完一局（点「散场复盘」）之后，每个 AI 会自己写下这一局的收获。'));
    return;
  }
  S.players.forEach((p) => {
    const c = el('div', 'player-chip');
    c.appendChild(el('span', 'n', esc(p.display_name)));
    c.appendChild(el('span', 'm',
      `${p.sessions_played} 局 · 梗 ${p.memes} · 习惯 ${p.quirks} · 角色 ${p.characters}`));
    c.onclick = async () => {
      document.querySelectorAll('.player-chip').forEach((x) => x.classList.remove('active'));
      c.classList.add('active');
      detail.innerHTML = '';
      const r = await call('player_card', p.player_id);
      if (!r.ok) { detail.appendChild(el('div', 'empty', r.message)); return; }
      detail.appendChild(playerCardView(r.card));
      const bar = el('div', 'row-actions');
      const reset = el('button', 'btn tiny danger', '清空这个玩家的记忆');
      reset.onclick = async () => {
        if (!confirm(`清空 ${p.display_name} 的跨周目记忆？（会备份成 .bak）`)) return;
        const rr = await call('reset_player_card', p.player_id);
        toast(rr.ok ? '已清空' : rr.message, rr.ok ? 'ok' : 'err');
        refreshPlayers();
      };
      bar.appendChild(reset);
      detail.appendChild(bar);
    };
    list.appendChild(c);
  });
}

/* ══════════════════════════ 规则弹窗 ══════════════════════════ */

async function refreshRules() {
  const b = await call('bootstrap');
  const g = await call('glossary_terms');
  $('rulesState').innerHTML = b.rules_full
    ? `规则书全文已抽取，可以检索（<b>检索是独立的</b>，不会每轮注入，不额外烧钱）。`
    : `还没有把规则书 PDF 抽成文本。把你自己的规则文本放进 <code>data/rules/</code> 也会被用上。`;

  const box = $('glossaryList');
  box.innerHTML = '';
  const entries = Object.entries(g).sort((a, b2) => a[0].localeCompare(b2[0], 'zh'));
  entries.forEach(([t, d]) => {
    const r = el('div', 'gterm');
    r.appendChild(el('div', 't', t));
    r.appendChild(el('div', 'd', d));
    box.appendChild(r);
  });
}

async function searchRules() {
  const q = $('ruleQuery').value.trim();
  if (!q) return;
  const box = $('ruleHits');
  box.innerHTML = el('div', 'hint', '检索中…');
  const r = await call('search_rules', q);
  box.innerHTML = '';
  if (!r.hits || !r.hits.length) {
    box.appendChild(el('div', 'hint', '没有找到。可能规则书全文还没抽取，或换个关键词。'));
    return;
  }
  r.hits.forEach((h) => {
    const d = el('div', 'rule-hit');
    d.appendChild(el('div', 'src', `${h.book} · 第 ${h.page} 页`));
    d.appendChild(el('div', null, h.text));
    box.appendChild(d);
  });
}

/* ══════════════════════════ 存档弹窗 ══════════════════════════ */

async function refreshSessions() {
  S.sessions = await call('list_sessions');
  const box = $('sessionList');
  box.innerHTML = '';
  if (!S.sessions.length) { box.appendChild(el('div', 'empty', '还没有存档。')); return; }
  S.sessions.forEach((s) => {
    const r = el('div', 'session-row');
    r.appendChild(el('span', 'id', s.session_id));
    r.appendChild(el('span', 'm', `${s.module_id || '自由跑团'} · 第 ${s.round} 轮 · ${s.players} 名玩家`));
    r.appendChild(el('span', 'spacer'));
    const b = el('button', 'btn tiny', '载入');
    b.onclick = async () => {
      const rr = await call('load_session', s.session_id);
      if (!rr.ok) { toast(rr.message, 'err'); return; }
      toast('已载入存档', 'ok');
      closeAllModals();
      await refreshState();
    };
    r.appendChild(b);
    box.appendChild(r);
  });
}

/* ══════════════════════════ 插件 ══════════════════════════ */

let PLUGINS = [];
let PLUGIN_ROOT = '';
// 打开着的插件：id -> {surface, node, api}
const PLUGIN_OPEN = new Map();
// 插件订阅的事件回调：id -> [fn]
const PLUGIN_SUBS = new Map();
// 出过错的插件：id -> 错误文字。出错了就在管理页标红。
const PLUGIN_ERRORS = new Map();

async function refreshPlugins() {
  try {
    const r = await call('list_plugins');
    PLUGINS = (r && r.plugins) || [];
    PLUGIN_ROOT = (r && r.root) || '';
  } catch (e) {
    PLUGINS = [];
    reportClientError('读插件清单失败', e && e.stack);
  }
  $('pluginsPath').textContent = PLUGIN_ROOT;
  renderPlugins();
  syncPluginSurfaces();
}

function renderPlugins() {
  const box = $('pluginList');
  box.innerHTML = '';
  if (!PLUGINS.length) {
    box.appendChild(el('div', 'empty',
      '还没有插件。点上面「打开插件目录」，把插件文件夹丢进去，再点「重新扫描」。'));
    return;
  }
  PLUGINS.forEach((p) => {
    const c = el('div', 'plugin-card' + (p.enabled ? ' on' : '') +
      (p.error || PLUGIN_ERRORS.has(p.id) ? ' bad' : ''));
    const head = el('div', 'row-between');
    head.appendChild(el('b', null, (p.enabled ? '🧩 ' : '⬜ ') + (p.name || p.id)));
    const right = el('span', 'spacer');
    head.appendChild(right);

    const openBtn = el('button', 'btn tiny' + (p.enabled ? '' : ' ghost'),
      p.surface === 'panel' ? '在右栏打开' : '打开');
    openBtn.disabled = !p.enabled || !!p.error;
    openBtn.onclick = () => openPlugin(p.id);
    head.appendChild(openBtn);

    const tg = el('button', 'btn tiny' + (p.enabled ? ' danger' : ''), p.enabled ? '停用' : '启用');
    tg.disabled = !!p.error;
    tg.onclick = async () => {
      tg.disabled = true;
      const r = await call('set_plugin_enabled', p.id, !p.enabled);
      toast(r.message || (p.enabled ? '已停用' : '已启用'), r.ok ? 'ok' : 'err');
      if (!r.ok && p.enabled) closePlugin(p.id);
      await refreshPlugins();
    };
    head.appendChild(tg);
    c.appendChild(head);

    const bits = [];
    if (p.version) bits.push('v' + p.version);
    if (p.author) bits.push(p.author);
    bits.push({ modal: '独立页面', window: '悬浮窗', panel: '右栏标签' }[p.surface] || p.surface);
    c.appendChild(el('div', 'm', bits.join(' · ')));
    if (p.description) c.appendChild(el('div', 's', p.description));

    const err = p.error || PLUGIN_ERRORS.get(p.id);
    if (err) c.appendChild(el('div', 'warns', err));
    box.appendChild(c);
  });
}

/** 顶栏给非 panel 的插件挂按钮。panel 的挂到右栏标签上。 */
function syncPluginSurfaces() {
  // 关掉已经停用/删掉的插件的界面
  PLUGIN_OPEN.forEach((_v, id) => {
    const p = PLUGINS.find((x) => x.id === id);
    if (!p || !p.enabled) closePlugin(id);
  });
  renderPluginButtons();
}

function renderPluginButtons() {
  const bar = $('pluginButtons');
  if (!bar) return;
  bar.innerHTML = '';
  PLUGINS.filter((p) => p.enabled && !p.error && p.surface !== 'panel').forEach((p) => {
    const b = el('button', 'btn ghost', '🧩 ' + (p.name || p.id));
    b.onclick = () => openPlugin(p.id);
    bar.appendChild(b);
  });
}

async function openPlugin(id) {
  const p = PLUGINS.find((x) => x.id === id);
  if (!p || !p.enabled) return;
  if (PLUGIN_OPEN.has(id)) { focusPlugin(id); return; }

  let html = '';
  let js = '';
  try {
    if (p.page) {
      const r = await call('plugin_asset', id, p.page);
      if (!r.ok) throw new Error(r.message);
      html = r.data;
    }
    if (p.script) {
      const r = await call('plugin_asset', id, p.script);
      if (!r.ok) throw new Error(r.message);
      js = r.data;
    }
  } catch (e) {
    PLUGIN_ERRORS.set(id, '读插件文件失败：' + e.message);
    renderPlugins();
    toast(`${p.name || id} 读不出来：${e.message}`, 'err');
    return;
  }

  const node = buildPluginSurface(p);
  node.querySelector('.plugin-body').innerHTML = html ||
    '<div class="empty">这个插件没有自己的页面，它可能只订阅事件。</div>';
  PLUGIN_OPEN.set(id, { surface: p.surface, node, api: null });
  PLUGIN_SUBS.set(id, []);
  $('pluginSurfaces').appendChild(node);

  if (js) runPluginScript(id, js, node);
  renderPluginButtons();
  if (p.surface === 'panel') renderRight();
}

/** 按 surface 造不同的外壳。 */
function buildPluginSurface(p) {
  const title = p.name || p.id;
  if (p.surface === 'panel') {
    const n = el('div', 'plugin-panel');
    n.dataset.plugin = p.id;
    return n;
  }
  if (p.surface === 'window') {
    const n = el('div', 'plugin-win');
    n.dataset.plugin = p.id;
    const head = el('div', 'plugin-win-head');
    head.appendChild(el('span', null, '🧩 ' + title));
    const sp = el('span', 'spacer'); head.appendChild(sp);
    const x = el('button', 'btn tiny ghost', '✕');
    x.onclick = () => closePlugin(p.id);
    head.appendChild(x);
    n.appendChild(head);
    n.appendChild(el('div', 'plugin-body'));
    makeDraggable(n, head);
    return n;
  }
  const n = el('div', 'plugin-modal');
  n.dataset.plugin = p.id;
  const box = el('div', 'plugin-modal-box');
  const head = el('header', 'modal-head');
  head.appendChild(el('h3', null, '🧩 ' + title));
  const x = el('button', 'btn tiny ghost', '✕');
  x.onclick = () => closePlugin(p.id);
  head.appendChild(x);
  box.appendChild(head);
  box.appendChild(el('div', 'plugin-body'));
  n.appendChild(box);
  n.onclick = (e) => { if (e.target === n) closePlugin(p.id); };
  return n;
}

function focusPlugin(id) {
  const rec = PLUGIN_OPEN.get(id);
  if (!rec) return;
  if (rec.surface === 'panel') { S.rightTab = 'plugin:' + id; renderRight(); return; }
  rec.node.style.zIndex = String(900 + (Date.now() % 90));
}

function closePlugin(id) {
  const rec = PLUGIN_OPEN.get(id);
  if (!rec) return;
  try { rec.node.remove(); } catch (e) { /* 已经没了 */ }
  PLUGIN_OPEN.delete(id);
  PLUGIN_SUBS.delete(id);
  renderPluginButtons();
  if (S.rightTab === 'plugin:' + id) { S.rightTab = 'chronicle'; renderRight(); }
}

/** 让悬浮窗能拖。挂在标题栏上。 */
function makeDraggable(node, handle) {
  let sx = 0, sy = 0, ox = 0, oy = 0, on = false;
  handle.style.cursor = 'move';
  handle.onmousedown = (e) => {
    if (e.target.closest('button')) return;
    on = true;
    sx = e.clientX; sy = e.clientY;
    const r = node.getBoundingClientRect();
    ox = r.left; oy = r.top;
    node.style.right = 'auto';
    node.style.left = ox + 'px';
    node.style.top = oy + 'px';
    e.preventDefault();
  };
  window.addEventListener('mousemove', (e) => {
    if (!on) return;
    node.style.left = (ox + e.clientX - sx) + 'px';
    node.style.top = Math.max(0, oy + e.clientY - sy) + 'px';
  });
  window.addEventListener('mouseup', () => { on = false; });
}

/** 跑插件的 ui.js。
 *
 * 用一个函数包起来，插件里的语法错误或初始化异常只会把它自己标红，
 * 不会把整个界面带崩。
 */
function runPluginScript(id, code, node) {
  const api = {
    id,
    root: PLUGIN_ROOT + '/' + id,
    node,
    body: node.querySelector('.plugin-body'),
    call: (m, ...a) => call(m, ...a),
    toast,
    el,
    esc,
    /** 订阅引擎事件。ev 是 {type, text, name, seat_id, meta}。 */
    onEvent: (fn) => {
      const list = PLUGIN_SUBS.get(id) || [];
      list.push(fn);
      PLUGIN_SUBS.set(id, list);
    },
    on: (type, fn) => api.onEvent((ev) => { if (ev.type === type) fn(ev); }),
    debug: (...a) => console.log('[plugin:' + id + ']', ...a),
  };
  const rec = PLUGIN_OPEN.get(id);
  if (rec) rec.api = api;
  try {
    const fn = new Function('DSH', '"use strict";\n' + code + '\n');
    fn(api);
    PLUGIN_ERRORS.delete(id);
  } catch (e) {
    PLUGIN_ERRORS.set(id, '插件脚本出错：' + (e && e.message ? e.message : e));
    reportClientError('插件 ' + id + ' 出错', e && e.stack);
    renderPlugins();
  }
}

/** 把引擎事件派发给订阅了的插件。由主循环在处理完事件之后调。 */
function dispatchToPlugins(evts) {
  if (!PLUGIN_SUBS.size) return;
  PLUGIN_SUBS.forEach((subs, id) => {
    if (!subs.length) return;
    subs.forEach((fn) => {
      evts.forEach((ev) => {
        try { fn(ev); } catch (e) {
          PLUGIN_ERRORS.set(id, '插件处理事件出错：' + (e && e.message ? e.message : e));
        }
      });
    });
  });
}

/* ══════════════════════════ 事件绑定 ══════════════════════════ */

function bind() {
  document.querySelectorAll('[data-modal]').forEach((b) => {
    b.onclick = async () => {
      // 每个按钮的准备工作都包一层：某一个弹窗的初始化出错，
      // 不能连累其他按钮点了没反应。
      try {
        const id = b.dataset.modal;
        if (id === 'modalSettings') renderSettings();
        if (id === 'modalRoster') { await refreshRoster(); }
        if (id === 'modalModule') { await refreshModules(); }
        if (id === 'modalPlugins') { await refreshPlugins(); }
        if (id === 'modalNewGame') { await openNewGame(); return; }
        if (id === 'modalPlayers') { await refreshPlayers(); }
        if (id === 'modalRules') { await refreshRules(); }
        if (id === 'modalSessions') { await refreshSessions(); }
        if (id === 'modalStudy') { await openStudyRoom(); return; }
        openModal(id);
      } catch (e) {
        reportClientError('打开「' + (b.dataset.modal || '?') + '」失败', e && e.stack);
        toast('打不开：' + (e && e.message ? e.message : e), 'err');
      }
    };
  });
  document.querySelectorAll('[data-close]').forEach((b) => {
    b.onclick = () => closeAllModals();
  });
  document.querySelectorAll('.modal').forEach((m) => {
    m.onclick = (e) => { if (e.target === m) closeAllModals(); };
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeAllModals(); });

  $('rightTabs').onclick = (e) => {
    const t = e.target.closest('.tab');
    if (!t) return;
    document.querySelectorAll('#rightTabs .tab').forEach((x) => x.classList.remove('active'));
    t.classList.add('active');
    S.rightTab = t.dataset.tab;
    renderRight();
  };

  $('btnScrollBottom').onclick = () => { logEl().scrollTop = logEl().scrollHeight; };
  $('btnClearLog').onclick = () => { logEl().innerHTML = ''; S.events = []; };

  if ($('btnOpenPluginsDir')) {
    $('btnOpenPluginsDir').onclick = async () => {
      const r = await call('open_plugins_folder');
      if (r && r.message) toast(r.message, r.ok ? 'ok' : 'err');
    };
  }
  if ($('btnReloadPlugins')) {
    $('btnReloadPlugins').onclick = async () => {
      await refreshPlugins();
      await refreshState();
      toast(`扫到 ${PLUGINS.length} 个插件`);
    };
  }

  // 研读室
  $('studyModuleSel').onchange = renderStudySaved;
  $('btnRunStudy').onclick = () => runStudy(false);
  $('btnRereadStudy').onclick = async () => {
    const id = $('studyModuleSel').value;
    if (!id) { toast('先挑一个模组', 'err'); return; }
    const hit = STUDIES.find((s) => s.module_id === id);
    if (hit && !confirm(`《${hit.title || id}》已经读过了，重读会丢掉旧记录（再花一次钱）。继续？`)) return;
    await runStudy(true);
  };
  $('btnForgetStudy').onclick = async () => {
    const id = $('studyModuleSel').value;
    if (!id) return;
    if (!confirm('忘掉这个模组的研读记录？以后开这个本要重新读一遍。')) return;
    const r = await call('forget_study', id);
    toast(r.ok ? '已忘掉' : '没有这个模组的研读记录', r.ok ? 'ok' : 'err');
    const b = await call('bootstrap');
    STUDIES = b.studies || [];
    renderStudySaved();
  };
  $('btnRefreshStudies').onclick = async () => {
    const b = await call('bootstrap');
    S.modules = b.modules || S.modules;
    STUDIES = b.studies || [];
    renderStudyPicker();
    await syncStudyRoom();
  };
  $('btnStudyAsk').onclick = askStudy;  $('studyAskInput').onkeydown = (e) => { if (e.key === 'Enter') askStudy(); };
  $('btnStudyToGame').onclick = async () => {
    const id = $('studyModuleSel').value;
    if (!id) { toast('先挑一个模组', 'err'); return; }
    const r = await call('new_session', id, '');
    if (!r.ok) { toast(r.message, 'err'); return; }
    toast('已按这个模组建好新团，接着去「🎬 开新团」确认站位', 'ok');
    closeAllModals();
    await refreshState();
    await openNewGame();
  };

  // 推进
  $('btnPrepare').onclick = async () => {
    if (!S.state || !S.state.has_session) {
      toast('还没开团，先点左上角「🎬 开新团」。', 'err');
      openNewGame();
      return;
    }
    const r = await call('prepare');
    if (!r.ok) toast(r.message, 'err');
    else toast('开始掷属性、做功课、车卡…这一步比较慢');
  };
  $('btnStart').onclick = async () => {
    if (!S.state || !S.state.has_session) {
      toast('还没开团，先点左上角「🎬 开新团」。', 'err');
      openNewGame();
      return;
    }
    const r = await call('start');
    if (!r.ok) toast(r.message, 'err');
  };
  $('btnAuto').onclick = async () => {
    const n = Number($('autoRounds').value) || 5;
    const r = await call('run_auto', n);
    if (!r.ok) toast(r.message, 'err');
  };
  $('btnStep').onclick = async () => {
    const r = await call('step');
    if (!r.ok) toast(r.message, 'err');
  };
  $('btnStop').onclick = async () => { await call('stop'); toast('已请求停止'); };
  $('btnFinish').onclick = async () => {
    if (!confirm('结束本局并让每个 AI 复盘？（这一步会生成跨周目记忆）')) return;
    await call('finish');
    toast('正在散场复盘…');
  };

  $('btnDirector').onclick = sendDirector;
  $('directorInput').onkeydown = (e) => { if (e.key === 'Enter') sendDirector(); };

  // 出错记录
  $('btnRefreshErrors').onclick = () => refreshErrors();
  $('btnClearErrors').onclick = async () => {
    if (!confirm('清空出错记录？')) return;
    await call('clear_errors');
    refreshErrors();
    toast('已清空', 'ok');
  };
  $('errLogBox').addEventListener('toggle', (e) => {
    if (e.target.open) refreshErrors();
  });

  // 设置
  $('btnSaveConfig').onclick = async () => {
    try { await saveConfig(false); } catch (e) {
      reportClientError('保存设置失败', e && e.stack);
      toast('保存失败：' + e.message, 'err');
    }
  };
  $('btnResetConfig').onclick = async () => {
    if (!confirm('恢复默认设置？会覆盖所有席位配置。')) return;
    S.cfg = await call('reset_config');
    renderSettings();
    await refreshState();
    toast('已恢复默认', 'ok');
  };
  $('btnApplyPlCount').onclick = async () => {
    const n = Number($('plCount').value);
    S.cfg = await call('set_player_count', n);
    renderSettings();
    toast(`已设为 ${n} 名玩家`);
  };
  $('btnFillDeepseek').onclick = () => {
    let hit = 0;
    document.querySelectorAll('#seatEditors .seat-editor').forEach((box) => {
      const prov = box.querySelector('[data-k="provider"]');
      const base = box.querySelector('[data-k="base_url"]');
      const model = box.querySelector('[data-k="model"]');
      if (prov && base && model) {
        prov.value = 'deepseek';
        base.value = 'https://api.deepseek.com/v1';
        if (!model.value) model.value = 'deepseek-chat';
        updateModelList(box, 'deepseek');
        hit++;
      }
    });
    toast(`已把 ${hit} 个席位的地址填成 DeepSeek 官方，现在只需粘 Key`);
  };
  $('btnProbeAll').onclick = async () => {
    try {
      await saveConfig(true);
      toast('正在逐个测试连接…');
      const rs = await call('probe_all');
      const ok = rs.filter((r) => r.ok).length;
      toast(`${ok}/${rs.length} 个席位连接正常`, ok === rs.length ? 'ok' : 'err');
      rs.forEach((r) => toast(`${r.display_name}：${r.message}`, r.ok ? 'ok' : 'err'));
    } catch (e) { toast('测试失败：' + e.message, 'err'); }
  };

  // 名册与站位
  $('btnApplyRoles').onclick = async () => {
    const kp = $('kpSelect').value;
    const pls = Array.from(document.querySelectorAll('#plPicker input[type=checkbox]'))
      .filter((c) => c.checked && !c.disabled).map((c) => c.dataset.pid);
    const r = await call('assign_roles', kp, pls);
    if (!r.ok) { toast(r.message, 'err'); return; }
    S.cfg = r.config;
    ROSTER = r.roster;
    renderRoster();
    renderSettings();
    await refreshState();
    toast('站位已应用', 'ok');
  };
  $('btnRotateKeeper').onclick = async () => {
    const r = await call('rotate_keeper');
    if (!r.ok) { toast(r.message, 'err'); return; }
    S.cfg = r.config;
    ROSTER = r.roster;
    renderRoster();
    renderSettings();
    await refreshState();
    toast('换庄完成，轮到下一位当守秘人了', 'ok');
  };
  $('btnApplyCount2').onclick = async () => {
    const n = Number($('plCount2').value) || 0;
    const kp = $('kpSelect').value;
    const r = await call('set_player_count', n, kp);
    if (!r.ok) { toast(r.message, 'err'); return; }
    S.cfg = r.config;
    ROSTER = r.roster;
    renderRoster();
    renderSettings();
    await refreshState();
    toast(`已排成 ${n} 名玩家`, 'ok');
  };
  $('btnSaveRoster').onclick = async () => {
    const r = await call('save_roster', collectRoster());
    if (!r.ok) { toast(r.message, 'err'); return; }
    ROSTER = r.roster;
    renderRoster();
    toast('名册已保存。点「应用站位」让改动落到座位上。', 'ok');
  };

  // 开新团
  $('btnNewGame').onclick = () => { openNewGame().catch((e) => toast(e.message, 'err')); };
  const openRepo = async () => {
    const r = await call('open_modules_folder');
    toast(r.ok ? `已打开模组仓库：${r.path}` : r.message, r.ok ? 'ok' : 'err');
  };
  $('btnOpenRepo').onclick = openRepo;
  $('btnOpenRepo2').onclick = openRepo;
  $('btnRefreshModules2').onclick = async () => {
    const b = await call('bootstrap');
    S.modules = b.modules || [];
    renderNewGame();
    toast(`扫到 ${S.modules.length} 个模组`, 'ok');
  };
  $('btnApplyCount3').onclick = () => {
    const n = Math.max(0, Math.min(6, Number($('plCount3').value) || 0));
    const pool = (ROSTER.entries || []).map((e) => e.id).filter((id) => id !== NEW.keeper);
    NEW.players = pool.slice(0, n);
    renderNewGame();
  };
  $('btnRotateKeeper2').onclick = async () => {
    const r = await call('rotate_keeper');
    if (!r.ok) { toast(r.message, 'err'); return; }
    S.cfg = r.config;
    ROSTER = r.roster;
    NEW.keeper = ROSTER.keeper;
    NEW.players = (ROSTER.players || []).filter(Boolean).filter((p) => p !== NEW.keeper);
    renderNewGame();
    await refreshState();
    toast(`换庄完成，这一局由「${(ROSTER.entries.find((e) => e.id === NEW.keeper) || {}).handle}」当守秘人`, 'ok');
  };
  $('btnStartNewGame').onclick = async () => {
    NEW.premise = $('premiseInput2').value.trim();
    if (!NEW.keeper) { toast('得先选一个守秘人。', 'err'); return; }
    if (!NEW.players.length) { toast('至少要有一个玩家。', 'err'); return; }
    const btn = $('btnStartNewGame');
    btn.disabled = true;
    btn.textContent = '正在开团…';
    try {
      const r = await call('assign_roles', NEW.keeper, NEW.players);
      if (!r.ok) { toast(r.message, 'err'); return; }
      S.cfg = r.config;
      ROSTER = r.roster;
      const res = await call('new_session', NEW.module, NEW.premise);
      if (!res.ok) { toast(res.message, 'err'); return; }
      logEl().innerHTML = '';
      closeAllModals();
      renderSeats();
      renderSettings();
      await refreshState();
      toast('新团已开。接着点「① 车卡」。', 'ok');
    } finally {
      btn.disabled = false;
      btn.textContent = '🎬 开始新团';
    }
  };

  // 模组
  $('btnRefreshModules').onclick = async () => { await refreshModules(); toast('已刷新'); };
  $('btnFreePlay').onclick = () => {
    S.selectedModule = '';
    renderModules();
    toast('已选择自由跑团');
  };
  $('btnNewSession').onclick = async () => {
    const premise = $('premiseInput').value.trim();
    const r = await call('new_session', S.selectedModule, premise);
    if (!r.ok) { toast(r.message, 'err'); return; }
    logEl().innerHTML = '';
    closeAllModals();
    toast('新会话已建立。接着点「① 车卡」。', 'ok');
    await refreshState();
    renderRight();
  };

  // 扫描版 PDF → OCR
  const loadVision = async () => {
    const v = await call('vision_settings');
    $('visBase').value = v.base_url || '';
    $('visModel').value = v.model || '';
    $('visKey').value = v.api_key || '';
    $('visDpi').value = v.dpi || 150;
  };
  loadVision().catch(() => {});
  $('btnProbePdf').onclick = async () => {
    const p = $('ocrPath').value.trim();
    if (!p) { toast('先填 PDF 路径', 'err'); return; }
    const r = await call('probe_pdf', p);
    if (!r.ok) { toast(r.message, 'err'); return; }
    $('probeResult').textContent =
      `${r.name}：${r.pages} 页，采样页平均 ${Math.round(r.avg_chars)} 字 → ` +
      (r.needs_ocr ? '扫描型，需要 OCR' : '文本型，直接当模组用就行');
    toast(r.needs_ocr ? '这是扫描件，要 OCR' : '这是文本型 PDF，不用 OCR', 'ok');
  };
  $('btnVisModels').onclick = async () => {
    const base = $('visBase').value.trim();
    if (!base) { toast('先填视觉模型的 Base URL', 'err'); return; }
    const btn = $('btnVisModels');
    btn.disabled = true; btn.textContent = '拉取中…';
    try {
      // 先存一次，服务端拿得到 Key
      await call('save_vision_settings', {
        base_url: base, model: $('visModel').value.trim(),
        api_key: $('visKey').value, dpi: Number($('visDpi').value) || 150,
      });
      const r = await call('fetch_models', '', base, $('visKey').value);
      if (!r.ok) { toast(r.message, 'err'); return; }
      installModelList($('visModel').parentElement, $('visModel'), r.models);
      toast(`拉到 ${r.count} 个模型，点下面的名字就能选`, 'ok');
    } catch (e) {
      toast('拉取失败：' + e.message, 'err');
    } finally {
      btn.disabled = false; btn.textContent = '🔍 拉取';
    }
  };
  $('btnSaveVision').onclick = async () => {
    const key = $('visKey').value;
    const r = await call('save_vision_settings', {
      base_url: $('visBase').value.trim(),
      model: $('visModel').value.trim(),
      api_key: key,
      dpi: Number($('visDpi').value) || 150,
    });
    if (r.ok) { await loadVision(); toast('视觉模型配置已保存', 'ok'); }
    else toast(r.message || '保存失败', 'err');
  };
  $('btnRunOcr').onclick = async () => {
    const p = $('ocrPath').value.trim();
    if (!p) { toast('先填 PDF 路径', 'err'); return; }
    const r = await call('ocr_pdf', p, '', '', $('ocrPages').value.trim());
    if (!r.ok) { toast(r.message, 'err'); return; }
    toast('OCR 已开始，进度会显示在主日志里。跑完点「↻ 刷新」。', 'ok');
  };

  // 规则
  $('btnSearchRules').onclick = searchRules;
  $('ruleQuery').onkeydown = (e) => { if (e.key === 'Enter') searchRules(); };

  // 存档
  $('btnSaveNow').onclick = async () => { await call('save_now'); toast('已存档', 'ok'); };
  $('btnExport').onclick = async () => {
    const r = await call('export_markdown');
    if (r.ok) toast('已导出：' + r.path, 'ok');
    else toast(r.message, 'err');
  };
  $('btnRefreshSessions').onclick = refreshSessions;
}

async function sendDirector() {
  const inp = $('directorInput');
  const text = inp.value.trim();
  if (!text) return;
  const r = await call('director_note', text);
  if (r.ok) { inp.value = ''; toast('导演指令已送入守秘人'); }
  else toast(r.message, 'err');
}

/* ══════════════════════════ 启动 ══════════════════════════ */

/** pywebview 是在页面加载之后才注入 window.pywebview 的，
 *  而且注入分两步：先建出 window.pywebview（api 还是空对象），
 *  再异步把 Python 方法挂上去。所以**不能只看 api 存不存在**——
 *  必须确认我们要调的那个方法真的在了，否则 boot() 会抢跑失败。 */
function bridgeReady() {
  return !!(window.pywebview && window.pywebview.api
    && typeof window.pywebview.api.bootstrap === 'function');
}

async function waitBridge(timeoutMs = 25000) {
  const t0 = Date.now();
  while (!bridgeReady()) {
    if (Date.now() - t0 > timeoutMs) return false;
    await new Promise((r) => setTimeout(r, 100));
  }
  return true;
}

let booting = false;
let timersStarted = false;

async function boot() {
  if (booting) return;
  booting = true;

  renderFilters();
  bind();

  try {
    if (!(await waitBridge())) {
      toast('界面桥接没有就绪。如果窗口里一直没反应，关掉重开一次。', 'err');
      booting = false;
      return;
    }

    const b = await call('bootstrap');
    S.cfg = b.config;
    S.providers = S.cfg._providers || {};
    S.modules = b.modules || [];
    S.sessions = b.sessions || [];
    S.players = b.players || [];
    if (b.roster) ROSTER = b.roster;
    renderSettings();
    renderModules();
    renderRoster();
    await refreshState();
    renderRight();

    // ★ 自动接回上一局。
    //   数据一直在盘上（transcript.jsonl 逐条落盘），但如果界面开局是空的，
    //   用户就会以为记录没了——所以这里主动接回去。
    if (!S.state || !S.state.has_session) {
      try {
        const r = await call('resume_last');
        if (r && r.ok && !r.already) {
          const sid = (r.session && r.session.session_id) || '';
          toast(`已自动接上上一局（${sid}）`, 'ok');
          await refreshState();
          renderSeats();
          renderRight();
        }
      } catch (e) { /* 没有存档就算了 */ }
    }

    if (!S.cfg.seats || !S.cfg.seats.length) {
      toast('还没有配置席位，先打开「设置」。', 'err');
    } else {
      // 上次运行出过错就把话说出来，别让人对着一个不动的界面猜
      if ((b.errors || []).length) {
        toast(`上次运行记下 ${b.errors.length} 条出错，`
              + `「设置 → 🩺 最近出错」里能看到全部`, 'err');
      }
      const missing = (S.cfg.seats || []).filter(
        (s) => s.enabled !== false && (s.provider === 'deepseek' || s.provider === 'custom')
          && !s.api_key_set);
      if (missing.length) {
        toast(`还有 ${missing.length} 个席位没填 API Key。` +
          `想零成本试跑，就把接口改成「离线模拟」。`, 'err');
      }
    }

    // ★ 第一次打开、或者上一局已经结束：直接把开团向导推到他面前
    const noGame = !S.state || !S.state.has_session;
    if (noGame) {
      setTimeout(() => {
        if (!S.state || !S.state.has_session) {
          toast('点左上角的「🎬 开新团」开始一局。', 'ok');
        }
      }, 1200);
    }

    if (!timersStarted) {
      timersStarted = true;
      setInterval(pollEvents, POLL_MS);
      setInterval(refreshState, 2500);
    }
    booting = false;
  } catch (e) {
    toast('初始化失败：' + e.message, 'err');
    booting = false;
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
window.addEventListener('pywebviewready', () => { if (!S.cfg) boot(); });
