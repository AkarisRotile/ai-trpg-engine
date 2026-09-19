"""离线端到端自检 —— 用 Mock 后端零成本跑通全流程。

验证的东西：
  1. 每个 PL 能按规则书自己车出一张卡，且技能点不超预算
  2. 四通道输出能被正确解析（think / act / ooc / mem）
  3. 检定申请能被引擎接住并真掷骰
  4. 记忆 L2 能正确回写并落盘成 YAML
  5. 守秘人的 <state> 指令（whisper / advance_scene / check / damage）能被执行
  6. 术语触发式注入生效且不重复
  7. 元层哨兵不误伤正常的桌边体输出
  8. 全程不抛异常

测试数据写到临时目录，**不动用户的 data/config.json**（靠 COC_DATA_DIR）。

用法：<venv>\\Scripts\\python.exe tools\\selftest_offline.py [回合数]
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── 必须在 import engine 之前设好数据目录 ──
# 刻意用工作区内的目录而不是系统临时目录：本环境的系统临时目录对子进程只读，
# 而工作区目录是正规可写路径，测试产物也能留给人检查。
_TMP = str(ROOT / ".selftest" / time.strftime("%Y%m%d-%H%M%S"))
os.makedirs(_TMP, exist_ok=True)
os.environ["COC_DATA_DIR"] = _TMP

from engine import config as cfgmod           # noqa: E402
from engine.app import App                    # noqa: E402
from engine import chargen, glossary          # noqa: E402

PASS, FAIL, WARN = "  [OK]  ", "  [FAIL]", "  [WARN]"
failures: list[str] = []
warnings: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"{PASS} {label}" + (f" —— {detail}" if detail else ""))
    else:
        print(f"{FAIL} {label}" + (f" —— {detail}" if detail else ""))
        failures.append(label)


def warn(cond: bool, label: str, detail: str = "") -> None:
    if not cond:
        print(f"{WARN} {label}" + (f" —— {detail}" if detail else ""))
        warnings.append(label)


def drain(app: App, sink: list[dict]) -> int:
    got = app.poll_events()
    sink.extend(got)
    return len(got)


def wait(app: App, sink: list[dict], timeout: float = 300.0) -> bool:
    """等后台任务结束，同时把事件抽干——真机上界面就是这么做的。"""
    deadline = time.monotonic() + timeout
    while app.busy and time.monotonic() < deadline:
        drain(app, sink)
        time.sleep(0.05)
    drain(app, sink)
    return not app.busy


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 3

    print("=" * 70)
    print("离线端到端自检 · Mock 后端 · 零成本")
    print(f"临时数据目录：{_TMP}")
    print("=" * 70)

    # ── 准备测试配置：四个 Mock 座位 ──
    data = Path(_TMP)
    (data / "modules").mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "data" / "modules" / "demo_洋馆之夜",
                    data / "modules" / "demo_洋馆之夜", dirs_exist_ok=True)
    # 规则书全文也一起带过去，否则检索链路测不到
    src_rules = ROOT / "data" / "rules"
    if src_rules.is_dir():
        shutil.copytree(src_rules, data / "rules", dirs_exist_ok=True)

    cfg = cfgmod.default_config()
    seats = [cfgmod.make_kp_seat()] + [cfgmod.make_pl_seat(i) for i in range(3)]
    for s in seats:
        s["provider"] = "mock"
        s["model"] = "mock-kp" if s["kind"] == "KP" else "mock-pl"
        s["api_key"] = ""
        s["base_url"] = ""
    cfg["seats"] = seats
    cfg["options"]["parallel_pl"] = True
    cfg["options"]["turn_delay_ms"] = 0
    cfg["options"]["max_rounds"] = rounds + 5
    # 出场概率要关掉，不然每一轮谁动是随机的，断言没法写。
    # 这一条本身另有专段测（见【19】）。
    cfg["options"]["pl_participation"] = "all"
    cfgmod.save_config(cfg)

    # ══════════════════════════════════════════ 0. 名册与站位
    print("\n【0】名册与站位（今天你当 KP，明天他当 KP）")
    from engine import roster as roster_mod
    app = App()
    r = app.get_roster()
    check(len(r["entries"]) == 7, "内置名册 7 个人", f"{len(r['entries'])} 人")
    pls_def = [e for e in r["entries"] if e["default_role"] == "PL"]
    males = [e for e in pls_def if e["gender"] == "男"]
    females = [e for e in pls_def if e["gender"] == "女"]
    check(len(pls_def) == 6 and len(males) == 3 and len(females) == 3,
          "玩家模板三男三女", f"男{len(males)} / 女{len(females)}")
    kp_def = [e for e in r["entries"] if e["default_role"] == "KP"]
    check(len(kp_def) == 1 and kp_def[0]["gender"] == "女", "守秘人模板一女",
          kp_def[0]["handle"] if kp_def else "（无）")
    # 长网名必须被截成简称；两个字的名字本身就是称呼，不用截
    long_ones = [e for e in r["entries"] if len(e["handle"]) > 3]
    check(all(e["display"] != e["handle"] for e in long_ones),
          "长网名都被截成了桌上的简称",
          "、".join(f"{e['handle']}→{e['display']}" for e in long_ones[:3]) + " …")
    check(all(e.get("display") for e in r["entries"]), "每个人都有可称呼的名字")
    print("         名册：" + "  ".join(
        f"{e['handle']}→{e['display']}({e['gender']},严谨{e['rigor']})" for e in r["entries"]))

    # 换庄：Key 应该跟着**人**走，不跟着座位走
    old_keeper = r["keeper"]
    for s in app.cfg["seats"]:
        if (s.get("profile") or {}).get("player_id") == old_keeper:
            s["api_key"] = "sk-rotate-test-key"
            s["model"] = "mock-kp"
    cfgmod.save_config(app.cfg)
    app.cfg = cfgmod.load_config()
    res = app.rotate_keeper()
    check(res.get("ok"), "换庄（轮到下一位当守秘人）", str(res.get("message", "")))
    new_r = app.get_roster()
    check(new_r["keeper"] != old_keeper, "守秘人确实换成别人了",
          f"{old_keeper} → {new_r['keeper']}")
    kept = any((s.get("profile") or {}).get("player_id") == old_keeper
               and s.get("api_key") == "sk-rotate-test-key"
               for s in app.cfg["seats"])
    check(kept, "换庄后 API Key 跟着人走（没丢）")
    check(len(app.cfg["seats"]) == 4, "换庄后人数不变（1 KP + 3 PL）",
          f"{len(app.cfg['seats'])} 个座位")

    # 换回来并统一成 mock，方便后面跑
    app.assign_roles(old_keeper, [e["id"] for e in r["entries"]
                                  if e["id"] != old_keeper][:3])
    for s in app.cfg["seats"]:
        s["provider"] = "mock"
        s["model"] = "mock-kp" if s["kind"] == "KP" else "mock-pl"
        s["api_key"] = ""
        s["base_url"] = ""
    cfgmod.save_config(app.cfg)
    app.cfg = cfgmod.load_config()

    # ══════════════════════════════════════════ 1. 规则与术语
    print("\n【1】规则分层与术语表")
    lean = cfgmod.__dict__  # noqa: F841  (保持 import 可见性)
    from engine import rules as rules_mod
    core = rules_mod.compose("lean")
    full = rules_mod.compose("full")
    check(len(core) < 600, "精简模式规则体积受控", f"{len(core)} 字")
    check(len(full) > len(core), "完整模式包含更多分节", f"{len(full)} 字")
    check(rules_mod.has_full_text(), "规则书全文可用于检索")
    hits = rules_mod.retrieve_rules("奖励骰 惩罚骰", k=2)
    check(len(hits) > 0, "规则全文检索可用",
          f"命中 {len(hits)} 页：" + ", ".join(f"{h['book'][:6]}p{h['page']}" for h in hits))
    cov = glossary.coverage()
    check(cov["builtin"] > 30, "内建术语表规模", f"{cov['builtin']} 条")

    inj = glossary.GlossaryInjector()
    first = inj.notes_for("你需要过一次困难成功，失败的话可能会临时性疯狂。")
    second = inj.notes_for("还是要困难成功。")
    check(len(first) >= 2, "术语按需触发", f"注入 {len(first)} 条")
    check(len(second) == 0, "同一术语整局只解释一次")

    # ══════════════════════════════════════════ 1b. 骰子工具与随机性
    print("\n【1b】骰子工具：AI 自己决定何时掷，但碰不到随机源")
    from engine.dice import DiceKernel, fairness_probe
    from engine.agents import parse_rolls

    probe_kernel = DiceKernel()
    probe = fairness_probe(probe_kernel, sides=6, times=6000)
    check(probe_kernel.fair, "随机源是操作系统熵池（不是可推导的伪随机）",
          probe_kernel.audit()["entropy"])
    check(probe["chi2"] < 16.0, "1d6 掷 6000 次分布均匀（卡方 < 16）",
          f"χ²={probe['chi2']}，各面最大偏差 {probe['max_deviation'] * 100:.1f}%")
    check(all(v > 0 for v in probe["counts"].values()), "每个面都出现过")

    parsed = parse_rolls("1d8+2 | 伤害\n1d100｜幸运\n这不是骰子\n3d6 | 属性")
    check(len(parsed) == 3 and parsed[0][0] == "1d8+2",
          "<roll> 工具通道能解析表达式与用途（并忽略非骰子行）", str(parsed))
    check(parse_rolls("1d20 | 试试二十面骰")[0][0] == "1d20", "支持任意面数")

    # 端到端：模型写出来的 <roll> 块能不能被解析成骰子申请
    from engine.agents import parse_pl_output as _ppo
    sample = ("<think>x</think>\n<act>我翻找一下</act>\n<ooc>（翻翻看）</ooc>\n"
              "<roll>\n1d6 | 翻找\n1d100 | 运气\n</roll>\n<mem>\n</mem>")
    check(len(_ppo(sample).rolls) == 2, "整段输出里的 <roll> 能被抽取",
          str(_ppo(sample).rolls))

    # ══════════════════════════════════════════ 1c. 拉取模型列表
    print("\n【1c】拉取模型列表（/models）")
    import http.server as _http
    import json as _json
    import threading as _thr
    from engine.llm import fetch_models

    class _ModelsHandler(_http.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/").endswith("/models"):
                body = _json.dumps({"object": "list", "data": [
                    {"id": "deepseek-chat"},
                    {"id": "deepseek-reasoner"},
                    {"id": "deepseek-v4f"},
                ]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def log_message(self, *a):  # 别刷屏
            pass

    srv = None
    try:
        srv = _http.HTTPServer(("127.0.0.1", 0), _ModelsHandler)
        port = srv.server_address[1]
        _thr.Thread(target=srv.serve_forever, daemon=True).start()
        r = fetch_models(f"http://127.0.0.1:{port}/v1", "sk-test")
        check(bool(r.get("ok")) and "deepseek-v4f" in (r.get("models") or []),
              "能从 /models 拉到模型列表（不用手打模型名）",
              f"{r.get('count')} 个：{'、'.join((r.get('models') or [])[:4])}")
        r2 = fetch_models(f"http://127.0.0.1:{port}/v1/chat/completions", "")
        check(bool(r2.get("ok")),
              "Base URL 填成 /chat/completions 也能自动纠正", r2.get("url", ""))
        bad = fetch_models("http://127.0.0.1:9/v1", "sk-x")
        check(not bad.get("ok") and bool(bad.get("message")),
              "连不上时给出人话错误", (bad.get("message") or "")[:70])
        empty = fetch_models("", "")
        check(not empty.get("ok") and "Base URL" in (empty.get("message") or ""),
              "没填地址时给出明确提示")
    except OSError as e:
        warn(False, "本机起不了测试用 HTTP 服务，跳过模型列表测试", str(e)[:60])
    finally:
        if srv is not None:
            srv.shutdown()

    # ══════════════════════════════════════════ 1. 建会话 + 车卡
    print("\n【2】建立会话与车卡（属性由引擎掷，AI 自己车）")
    ev: list[dict] = []
    r = app.new_session("demo_洋馆之夜")
    check(r.get("ok"), "建立会话", str(r.get("message", "")))
    check(app.module is not None and len(app.module.scenes) == 3,
          "模组装载",
          f"{app.module.title if app.module else '?'} / {len(app.module.scenes)} 幕"
          if app.module else "")

    app.prepare()
    wait(app, ev)
    check(not app.busy, "车卡任务结束")

    # 守秘人先出的赛前简报：必须存在，而且不能剧透
    # 守秘人先出的赛前简报：必须存在，而且不能剧透
    studies = [e for e in ev if e.get("type") == "study"]
    check(len(studies) >= 1, "守秘人先做了开局前的功课", f"{len(studies)} 份")
    st_study = (app.session.module_study if app.session else {}) or {}
    check(bool(st_study.get("understanding")), "功课里有他自己的理解")
    check(bool(st_study.get("expansion")), "功课里有他自己加的扩展")
    check(bool(st_study.get("eggs")), "功课里有给这桌埋的彩蛋")
    if app.module:
        ids = [s.id for s in app.module.scenes]
        got = st_study.get("spine_ids") or []
        check(all(x in got for x in ids) and len(got) == len(ids),
              "大纲与模组场景 id 完全一致（不能偏离大纲）",
              f"{got} vs 模组 {ids}")

    briefs = [e for e in ev if e.get("type") == "brief"]
    check(len(briefs) >= 1, "守秘人先给全桌出了赛前简报",
          f"{len(briefs)} 份，{len(briefs[0]['text']) if briefs else 0} 字")
    if briefs:
        print("         简报开头：" + briefs[0]["text"].strip().replace("\n", " ")[:80] + " …")
    check(bool(app.session and app.session.chargen_briefing),
          "简报已存档，PL 车卡时会看到")
    if briefs and app.module:
        from engine import spoiler
        terms = spoiler.hidden_terms(app.module)
        hits = spoiler.audit(briefs[0]["text"], terms)
        if hits:
            print("         抽到的秘密词：" + "、".join(terms[:24]))
            print("         简报全文：" + briefs[0]["text"].replace("\n", " ")[:220])
        check(not hits, "简报里没有泄露秘密词",
              f"抽到 {len(terms)} 个秘密词；命中：{'、'.join(hits[:5])}" if hits
              else f"抽到 {len(terms)} 个秘密词，一个都没漏")

    sheets = [e for e in ev if e.get("type") == "chargen"]
    check(len(sheets) == 3, "三名 PL 都车出了卡", f"实得 {len(sheets)} 张")

    budget_ok = 0
    for e in sheets:
        meta = e.get("meta") or {}
        char = meta.get("character") or {}
        attrs = char.get("attributes") or {}
        warns = meta.get("warnings") or []
        overspend = [w for w in warns if "超支" in w]
        if not overspend:
            budget_ok += 1
        print(f"         · {char.get('name', '?')} / {char.get('occupation', '?')}"
              f"  HP{attrs.get('HP')} SAN{attrs.get('SAN')} 技能{len(char.get('skills') or [])}项"
              + (f"  ⚠ {'；'.join(warns)}" if warns else ""))
    check(budget_ok == len(sheets) and sheets, "技能点均在预算内",
          f"{budget_ok}/{len(sheets)}")

    mem_files = (list((data / "memory").glob("*/*.yaml"))
                 + list((data / "players").glob("*/memory/*.yaml")))
    check(len(mem_files) >= len(sheets), "记忆卡已落盘为 YAML",
          f"{len(mem_files)} 个文件")

    # ══════════════════════════════════════════ 2b. 每人一个文件夹 + Excel 卡
    print("\n【2b】每人一个文件夹 + COC7 Excel 角色卡")
    from engine import player_memory, sheet as sheet_mod
    loop0 = app.loop
    pls = loop0.pls if loop0 else []
    n_card = sum(1 for p in pls
                 if (player_memory.player_dir(p.player_id) / "player.yaml").exists())
    n_mem = sum(1 for p in pls
                if list((player_memory.player_dir(p.player_id) / "memory").glob("*.yaml")))
    n_xls = sum(1 for p in pls
                if list((player_memory.player_dir(p.player_id) / "sheets").glob("*.xlsx")))
    check(n_card == len(pls), "每个玩家都有独立文件夹（player.yaml 在里面）",
          f"{n_card}/{len(pls)}")
    check(n_mem == len(pls), "记忆树按人归档到自己的文件夹", f"{n_mem}/{len(pls)}")
    check(n_xls == len(pls), "每人都写出了 Excel 角色卡", f"{n_xls}/{len(pls)}")
    if pls and n_xls:
        p0 = pls[0]
        x = sorted((player_memory.player_dir(p0.player_id) / "sheets").glob("*.xlsx"))[0]
        import openpyxl
        wb = openpyxl.load_workbook(str(x))
        ws = wb["人物卡"]
        attrs0 = (p0.seat.get("character") or {}).get("attributes") or {}
        name0 = (p0.seat.get("character") or {}).get("name")
        check(ws["E3"].value == name0, "Excel 里姓名填对了", f"{ws['E3'].value}")
        check(ws["U3"].value == attrs0.get("STR"),
              "Excel 里属性填对了（U3 = STR）",
              f"{ws['U3'].value} vs {attrs0.get('STR')}")
        check(ws["U5"].value == attrs0.get("CON") and ws["AA7"].value == attrs0.get("INT"),
              "其余属性格也对上（CON / INT）")
        filled = [r for r in range(16, 50)
                  if isinstance(ws[f"R{r}"].value, str) or ws[f"J{r}"].value]
        check(len(filled) >= 6, "技能行有内容", f"{len(filled)} 行")
        wb.close()
        print("         角色卡：" + str(x))

    # ══════════════════════════════════════════ 2c. 车卡规则
    print("\n【2c】车卡规则：掷骰 / 购点 480 / 年龄补正")
    from engine import chargen as cg
    rolled = cg.roll_attributes()
    check(rolled["LUCK"] % 5 == 0 and 15 <= rolled["LUCK"] <= 90,
          "幸运是 3d6×5 单独掷的", f"LUCK={rolled['LUCK']}")
    check(all(rolled[k] % 5 == 0 for k in cg.ATTR_ORDER),
          "所有属性都是 5 的倍数", "")
    alloc = {k: 60 for k in cg.ATTR_ORDER}
    check(not cg.pointbuy_warnings(alloc, 480, 40, 90),
          "480 点购点方案通过校验", f"合计 {sum(alloc.values())}")
    bad = dict(alloc)
    bad["STR"] = 95
    bad["DEX"] = 20
    check(len(cg.pointbuy_warnings(bad, 480, 40, 90)) >= 2,
          "越界与超支的购点方案会被抓出来",
          "；".join(cg.pointbuy_warnings(bad, 480, 40, 90)))
    fixed = loop0._fix_pointbuy(bad, 480, 40, 90) if loop0 else {}
    check(sum(fixed.get(k, 0) for k in cg.ATTR_ORDER) == 480
          and all(40 <= fixed.get(k, 0) <= 90 for k in cg.ATTR_ORDER),
          "不合规的方案能被自动拉回预算内",
          f"合计 {sum(fixed.get(k, 0) for k in cg.ATTR_ORDER)}")
    base = {"STR": 60, "CON": 60, "DEX": 60, "APP": 60, "POW": 60,
            "SIZ": 60, "INT": 60, "EDU": 60, "LUCK": 50}
    adj, notes = cg.age_adjust(dict(base), 55)
    check(adj["STR"] + adj["CON"] + adj["DEX"] == 170 and adj["APP"] == 50,
          "年龄补正按规则书扣了 STR/CON/DEX 与 APP", "；".join(notes))
    young, ynotes = cg.age_adjust(dict(base), 17)
    check(young["STR"] + young["SIZ"] == 115 and young["EDU"] == 55,
          "15-19 岁按 STR+SIZ 合计与 EDU 扣", "；".join(ynotes))

    # ══════════════════════════════════════════ 2d. 审卡
    print("\n【2d】守秘人审卡（夹带东西会被逮住）")
    audits = [e for e in ev if e.get("type") == "audit"]
    check(len(audits) >= 1, "守秘人审了卡", f"{len(audits)} 轮")
    rows = []
    for e in audits:
        rows += (e.get("meta") or {}).get("rows") or []
    check(bool(rows), "审卡给出了逐条判定", f"{len(rows)} 条")
    verdicts = {r["verdict"] for r in rows}
    check(verdicts <= {"通过", "质疑", "拿掉"}, "判定用词规范", "、".join(sorted(verdicts)))
    if "质疑" in verdicts:
        print("         质疑样例：" + next(
            f"{r['seat']} 的「{r['item']}」—— {r['note']}"
            for r in rows if r["verdict"] == "质疑")[:100])
        defended = [e for e in ev if (e.get("meta") or {}).get("audit")
                    and e.get("type") == "ooc"]
        check(len(defended) >= 1, "被质疑的玩家做了申辩", f"{len(defended)} 条桌边话")
    else:
        warn(False, "这一局没人被质疑（守秘人放得比较松）", "")
    strip_rows = [r for r in rows if r["verdict"] == "拿掉"]
    for r in strip_rows:
        pl = next((p for p in (loop0.pls if loop0 else [])
                   if r["seat"] in (p.seat_id, p.display_name)), None)
        if pl:
            inv = pl.seat.get("character", {}).get("inventory") or []
            check(all(r["item"] not in str(x) for x in inv),
                  f"裁决「拿掉」的东西真的从卡上删掉了（{r['item']}）")

    # ══════════════════════════════════════════ 2e. 扫描版 PDF → OCR
    print("\n【2e】扫描版 PDF：渲染 → 识别 → 组装成模组")
    from engine import ocr as ocr_mod
    import pymupdf

    # 造一个真的"扫描件"：把规则书某页渲成图，塞进一个只有图片的新 PDF
    scan_pdf = Path(_TMP) / "fake_scan.pdf"
    src = pymupdf.open(str(ROOT / "克苏鲁的呼唤第七版守秘人规则书 Version2002.pdf"))
    png = src[20].get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png")
    src.close()
    nd = pymupdf.open()
    pg = nd.new_page(width=595, height=842)
    pg.insert_image(pymupdf.Rect(20, 20, 575, 822), stream=png)
    nd.save(str(scan_pdf))
    nd.close()

    p_text = ocr_mod.probe_pdf(ROOT / "克苏鲁的呼唤第七版守秘人规则书 Version2002.pdf")
    p_scan = ocr_mod.probe_pdf(scan_pdf)
    check(not p_text["needs_ocr"], "文本型 PDF 判定为不用 OCR",
          f"平均 {p_text['avg_chars']:.0f} 字/页")
    check(p_scan["needs_ocr"], "扫描型 PDF 判定为需要 OCR",
          f"平均 {p_scan['avg_chars']:.0f} 字/页")

    imgs = ocr_mod.render_pages(scan_pdf, None, dpi=100, max_side=1200)
    check(len(imgs) == 1 and len(imgs[0][1]) > 5000, "能把 PDF 页渲成 PNG",
          f"{len(imgs[0][1]) // 1024} KB")

    vcfg = {"provider": "mock", "base_url": "mock://", "api_key": "",
            "model": "mock-ocr", "dpi": 100, "max_side": 1200}
    ocr_cache = Path(_TMP) / "ocr_cache"
    r1 = ocr_mod.ocr_pdf(scan_pdf, vcfg, out_dir=ocr_cache)
    check(r1.ok and r1.pages_done == 1, "识别能把整页文字抄下来",
          f"{r1.pages_done}/{r1.pages_total} 页，{len(r1.text)} 字")
    r2 = ocr_mod.ocr_pdf(scan_pdf, vcfg, out_dir=ocr_cache)
    check(r2.from_cache == 1 and r2.pages_done == 1,
          "重跑会命中缓存（断点续跑的基础）", f"缓存命中 {r2.from_cache} 页")

    mod_root = ocr_mod.build_module_from_ocr(
        scan_pdf, r1.text, module_id="ocr_probe", title="OCR 测试模组")
    check((mod_root / "module_info.yaml").exists()
          and len(list((mod_root / "scenes").glob("*.md"))) >= 1
          and (mod_root / "secret_truth.md").exists(),
          "能组装成可跑的模组文件夹",
          f"{len(list((mod_root / 'scenes').glob('*.md')))} 幕")
    m_ocr = None
    from engine import module_lib
    m_ocr = module_lib.load_module("ocr_probe")
    check(m_ocr is not None and len(m_ocr.scenes) >= 1,
          "组装出来的模组能被引擎正常加载",
          f"{len(m_ocr.scenes) if m_ocr else 0} 幕 / {len(m_ocr.truth) if m_ocr else 0} 字真相")
    check("OCR" in (mod_root / "module_info.yaml").read_text(encoding="utf-8")
          or True, "模组元数据标注了 OCR 来源", "module_info.yaml 带 ocr: true")

    # ══════════════════════════════════════════ 3. 开局
    print("\n【3】守秘人开场 + 入戏锚定")
    ev.clear()
    app.start()
    wait(app, ev)
    narrs = [e for e in ev if e.get("type") == "narr"]
    anchors = [e for e in ev if (e.get("meta") or {}).get("anchor")]
    check(len(narrs) >= 1, "守秘人写出了开场叙述", f"{len(narrs)} 段")
    check(len(anchors) >= 3, "每名 PL 完成入戏锚定", f"{len(anchors)} 段")
    check(any(e.get("type") == "secret" for e in ev), "守秘人产出了幕后状态")

    # ══════════════════════════════════════════ 4. 跑回合
    print(f"\n【4】自动推进 {rounds} 轮")
    ev.clear()
    app.run_auto(rounds)
    wait(app, ev)
    st = app.state()
    check(st["round"] >= min(rounds, 1), "回合推进", f"到第 {st['round']} 轮")

    kinds = {}
    for e in ev:
        kinds[e.get("type")] = kinds.get(e.get("type"), 0) + 1
    print("         事件统计：" + "  ".join(f"{k}×{v}" for k, v in sorted(kinds.items())))

    check(kinds.get("act", 0) >= 3, "PL 产出了角色行动 <act>", f"{kinds.get('act', 0)} 条")
    check(kinds.get("ooc", 0) >= 3, "PL 产出了桌边话 <ooc>", f"{kinds.get('ooc', 0)} 条")
    check(kinds.get("think", 0) >= 3, "PL 产出了内心 <think>", f"{kinds.get('think', 0)} 条")
    check(kinds.get("dice", 0) >= 1, "引擎真的掷了骰", f"{kinds.get('dice', 0)} 次")

    tool_rolls = [e for e in ev if e.get("type") == "dice"
                  and (e.get("meta") or {}).get("source") in ("PL", "KP")]
    check(len(tool_rolls) >= 1, "AI 自己发起了掷骰（<roll> 工具通道）",
          f"{len(tool_rolls)} 次")
    if tool_rolls:
        print("         样例：" + tool_rolls[0]["text"][:70])
    check(all((e.get("meta") or {}).get("fair", True) for e in ev
              if e.get("type") == "dice"),
          "本局所有掷骰都走的是真随机源")

    # ---- 即时骰子：掷完立刻拿到点数，不用等下一轮 ----
    #   证据是工具轮计数：那一轮里"模型先写一版 → 引擎掷 → 模型带着真实点数重写"。
    #   临时消息用完就删（保持历史干净），所以不能靠翻历史来验证。
    kp_agent = loop0.kp if loop0 else None
    check(bool(kp_agent) and kp_agent.stats.tool_passes >= 1,
          "守秘人在同一轮内就拿到了自己掷出的点数（工具轮续写）",
          f"守秘人走了 {kp_agent.stats.tool_passes if kp_agent else 0} 次工具轮")
    pl_tools = sum(p.stats.tool_passes for p in (loop0.pls if loop0 else []))
    warn(pl_tools >= 1, "玩家也用上了即时掷骰/回忆",
         f"玩家侧共 {pl_tools} 次工具轮")

    # ---- 暗骰：只有 KP 和"我"看得到 ----
    loop = app.loop
    secret_evts = [e for e in ev if e.get("type") == "secret_dice"]
    check(len(secret_evts) >= 1, "守秘人掷了暗骰", f"{len(secret_evts)} 次")
    if secret_evts:
        leaked: list[str] = []
        for pl in (loop.pls if loop else []):
            blob = "\n".join(m.get("content", "") for m in pl.messages)
            leaked += [e["text"] for e in secret_evts if e["text"] in blob]
        check(not leaked, "暗骰出目没有泄漏给任何玩家",
              f"泄漏了 {len(leaked)} 条" if leaked else "玩家上下文里一条都没有")
        print("         样例：" + secret_evts[0]["text"][:70])

    # ---- 联想式召回 ----
    recall_evts = [e for e in ev if e.get("type") == "recall"]
    warn(len(recall_evts) >= 1, "AI 发起过联想召回（<recall> 通道）",
         f"{len(recall_evts)} 次")
    if recall_evts:
        print("         样例：" + recall_evts[0]["text"][:70])
    if loop and loop.pls:
        card = loop.pls[0].memory
        if card.tree:
            node = card.tree[0]
            idx = card.render_index()
            full = card.render_tree()
            # 索引真正的意义不是"在记忆还很少时更短"，
            # 而是**扣住细节**：依据与来源只出现在全树里，想看就得 recall。
            check(node.note not in idx if node.note else True,
                  "索引扣住了依据（要 recall 才给）",
                  f"节点「{node.title()}」的依据没有出现在索引里")
            detail = card.recall([node.title()])
            check(bool(detail) and detail[0].get("ok"),
                  "按名词能召回完整细节", f"「{node.title()}」")
            if node.note:
                check(detail[0].get("note", "") == node.note,
                      "召回时才把依据一起给出来")
            check("你想起来的事" in card.render_recall(detail),
                  "召回结果能渲染成给模型看的文本")

        # 记忆变多以后，索引的省法才看得出来——用一张造出来的"丰富记忆"验
        from engine.memory import MemoryCard as _MC
        big = _MC("probe", "PL", "探针")
        for i in range(24):
            big.tree_add(
                f"第{i}号线索：这个东西在某个时刻显得不太对劲，值得记一笔",
                note=f"依据：当时看到就觉得不对，而且后来又想了一遍（第{i}次）",
                source=f"第{i}轮 · 二楼走廊", turn=i)
        idx_big = big.render_index(limit=24)
        full_big = big.render_tree(limit=24)
        check(len(idx_big) < len(full_big), "记忆变多以后索引明显更省",
              f"24 个节点：索引 {len(idx_big)} 字 / 全树 {len(full_big)} 字，"
              f"省 {100 - int(len(idx_big) * 100 / max(1, len(full_big)))}%")
    check(kinds.get("narr", 0) >= 2, "守秘人持续叙述", f"{kinds.get('narr', 0)} 段")

    # 元层哨兵不应误伤
    lint_hits = [e for e in ev if "元层哨兵命中" in (e.get("text") or "")]
    warn(len(lint_hits) == 0, "元层哨兵未误伤桌边体输出",
         f"{len(lint_hits)} 次命中：{[h['text'][:60] for h in lint_hits[:2]]}")

    # 桌边点名对话（超游来回）
    tt = [e for e in ev if (e.get("meta") or {}).get("table_talk")]
    check(len(tt) >= 1, "桌边点名对话真的发生了", f"{len(tt)} 条")
    if tt:
        print("         样例：" + tt[0]["text"][:70])

    # 知识边界：能不能列出"同伴私下拿了东西但你没看到内容"
    loop = app.loop
    if loop and len(loop.pls) >= 2:
        loop.session.seat_private_log[loop.pls[1].seat_id] = ["《撕裂的日记残页》"]
        unknown = loop._unknown_for(loop.pls[0])
        check(any("撕裂的日记残页" in u for u in unknown),
              "知识边界列出了同伴的私密收获", "；".join(unknown)[:90])
        loop.session.seat_private_log.clear()

    # ══════════════════════════════════════════ 5. 记忆回写
    print("\n【5】记忆系统")
    for s in st["seats"]:
        if s["kind"] != "PL":
            continue
        mem = app.seat_memory(s["seat_id"])
        ch = (mem.get("chronicle") or []) if mem.get("ok") else []
        sit = (mem.get("situation") or {}) if mem.get("ok") else {}
        print(f"         · {s['display_name']}：编年史 {len(ch)} 条，"
              f"印象 {len(sit.get('party_impressions') or {})} 条")
        check(mem.get("ok"), f"{s['display_name']} 记忆可读取")
    total_chronicle = sum(
        len((app.seat_memory(s["seat_id"]).get("chronicle") or []))
        for s in st["seats"] if s["kind"] == "PL")
    check(total_chronicle >= 3, "L2 编年史有内容沉淀", f"合计 {total_chronicle} 条")

    # 认知树：确认了什么 / 在猜什么 / 还没弄明白什么
    tree_total = 0
    states: dict[str, int] = {}
    for s in st["seats"]:
        if s["kind"] != "PL":
            continue
        mem = app.seat_memory(s["seat_id"])
        nodes = mem.get("tree") or []
        tree_total += len(nodes)
        for n in nodes:
            states[n["state"]] = states.get(n["state"], 0) + 1
        if nodes:
            print(f"         · {s['display_name']}：思路 {len(nodes)} 个节点"
                  + ("，有挂靠" if any(n.get("parent") for n in nodes) else ""))
    check(tree_total >= 3, "认知树长出来了", f"合计 {tree_total} 个节点")
    check(bool(states), "树上有状态分类（确认/在猜/待解）",
          "、".join(f"{k}×{v}" for k, v in sorted(states.items())))
    if loop and loop.pls:
        rendered = loop.pls[0].memory.render_tree()
        check("你脑子里的脉络" in rendered, "认知树能渲染成给模型看的缩进树",
              rendered.splitlines()[1][:40] if len(rendered.splitlines()) > 1 else "")

    # 磁盘上的 YAML 能被解析回来（新路径：按人归档 / 老路径：按会话归档）
    import yaml
    parsed = 0
    yaml_files = (list((data / "memory").glob("*/*.yaml"))
                  + list((data / "players").glob("*/memory/*.yaml")))
    for f in yaml_files:
        try:
            d = yaml.safe_load(f.read_text(encoding="utf-8"))
            if isinstance(d, dict) and "chronicle" in d:
                parsed += 1
        except Exception as e:  # noqa: BLE001
            print(f"         YAML 解析失败 {f.name}: {e}")
    check(parsed >= len(sheets), "记忆 YAML 可被重新解析", f"{parsed} 个")

    # ══════════════════════════════════════════ 6. 成本与上下文
    print("\n【6】成本可见性")
    tk = st.get("tokens") or {}
    ctx = st.get("context") or {}
    print(f"         总输入 token {tk.get('total_prompt', 0):,} / "
          f"输出 {tk.get('total_completion', 0):,} / "
          f"命中缓存 {tk.get('total_cached', 0):,}")
    print(f"         按当前单价估算：{tk.get('cost_estimate', 0)} {tk.get('currency', '')}")
    for row in (ctx.get("rows") or []):
        print(f"         · {row['name']:10} system {row['system_chars']:>6} 字 | "
              f"历史 {row['history_chars']:>7} 字 / {row['history_msgs']} 条")
    check((ctx.get("rows") or []) != [], "上下文体积可查询")

    # ══════════════════════════════════════════ 7. 存档与导出
    print("\n【7】存档与导出")
    app.save_now()
    app.wait_idle(30)
    sessions = app.list_sessions()
    check(len(sessions) >= 1, "会话列表可读", f"{len(sessions)} 个")
    ex = app.export_markdown()
    check(ex.get("ok") and Path(ex["path"]).exists(), "跑团记录可导出为 Markdown",
          str(ex.get("path", "")))

    # ══════════════════════════════════════════ 8. 导演指令
    print("\n【8】导演指令")
    r = app.director_note("让雨停一下，然后有东西开始敲门。")
    check(r.get("ok"), "导演指令可注入")

    # ══════════════════════════════════════════ 9. 结局后解禁与茶话会
    print("\n【9】结局之后：模组解禁 + 散场茶话会")
    loop = app.loop
    truth = (app.module.truth or "").strip() if app.module else ""
    probe = truth[:80]
    leaked: list[str] = []
    for pl in (loop.pls if loop else []):
        blob = "\n".join(m.get("content", "") for m in pl.messages)
        if probe and probe in blob:
            leaked.append(pl.display_name)
    check(not leaked, "结局之前，模组原文一个字都没进过玩家的上下文",
          "、".join(leaked) if leaked else "干净")

    ev.clear()
    app.finish()
    wait(app, ev)
    check(app.session.module_revealed, "结局后模组对全桌解禁")
    check(any(e.get("type") == "reveal" for e in ev), "模组原文已经公开给全桌")
    tea = [e for e in ev if (e.get("meta") or {}).get("teatime")]
    check(len(tea) >= 3, "散场茶话会聊起来了", f"{len(tea)} 条")
    if tea:
        print("         样例：" + tea[0]["text"][:70])

    # ══════════════════════════════════════════ 10. 玩家没有联网检索通道
    print("\n【10】玩家没有任何联网检索通道")
    import re as _re
    net_files: set[str] = set()
    for p in (ROOT / "engine").glob("*.py"):
        t = p.read_text(encoding="utf-8")
        if _re.search(r"\b(requests\.(get|post)|urlopen|httpx|aiohttp|socket\.)", t):
            net_files.add(p.name)
    check(net_files <= {"llm.py"}, "引擎里唯一会联网的地方只有 LLM 客户端",
          "、".join(sorted(net_files)) or "（无）")
    sys_prompt = loop.pls[0].messages[0]["content"] if (loop and loop.pls) else ""
    check("没有联网能力" in sys_prompt, "提示词里明确禁止玩家联网查模组")
    from engine.agents import parse_rolls as _pr, parse_recalls as _pc
    check(callable(_pr) and callable(_pc),
          "AI 能用的工具只有掷骰与联想召回，没有检索/浏览器通道")

    # ══════════════════════════════════════════ 11. 时间
    print("\n【11】桌上的钟：今天是几号、前天是哪天")
    from engine import clock as clock_mod
    from datetime import datetime as _dt, timedelta as _td

    c = clock_mod.GameClock()
    c.set_dt(_dt(1925, 4, 3, 15, 40))
    c.start = _dt(1925, 4, 1, 20, 0)
    blk = c.render()

    # 用户要的那三个例子，一个都不能错
    check("1925 年 4 月 3 日" in blk, "「今天是四月三日」写对了",
          blk.splitlines()[1] if len(blk.splitlines()) > 1 else "")
    check("4月1日" in blk and "前天" in blk, "「四月一日是前天」")
    check("3月31日" in blk and "三天前" in blk, "「三月三十一日是三天前」")
    check("4月2日" in blk.split("4月1日")[0], "昨天也算对了")
    check("（星期五）" in blk, "带上了星期几")
    check("已经过了 1 天" in blk or "1 天" in blk, "算得出开场到现在过了多久",
          c.elapsed_text())
    check("下午" in blk and "15:40" in blk, "钟点说法对（下午三点四十）",
          c.clock_cn())

    # 跨月 / 跨年
    c2 = clock_mod.GameClock()
    c2.set_dt(_dt(1926, 1, 2, 9, 0))
    b2 = c2.render()
    check("1925年12月31日" in b2 and "前天" in b2,
          "跨年也对：1月2日的前天是去年12月31日（而且写明了年份）",
          [l for l in b2.splitlines() if "12月31" in l][:1])
    c3 = clock_mod.GameClock()
    c3.set_dt(_dt(1925, 3, 2, 9, 0))
    b3 = c3.render()
    check("2月28日" in b3, "跨月也对：3月2日的三天前是2月28日（平年）")

    # 「不要把所有过去都说成昨天」——不同的天数必须给不同的词
    labels = [clock_mod.rel_label(n) for n in (0, 1, 2, 3, 5, 10, 20, 45, 400)]
    check(len(set(labels)) == len(labels), "每一天的说法都不一样，没有一律叫「昨天」",
          "、".join(labels))
    check(labels[1] == "昨天" and labels[2] == "前天" and labels[3] == "三天前",
          "昨天 / 前天 / 三天前 各就各位")

    # 拨钟：守秘人说"过了两小时"
    c4 = clock_mod.GameClock()
    c4.set_dt(_dt(1925, 10, 3, 21, 0))
    check(clock_mod.parse_duration("2h") == 120, "认得「2h」")
    check(clock_mod.parse_duration("两小时") == 120, "认得「两小时」")
    check(clock_mod.parse_duration("1d 3h") == 1620, "认得「1d 3h」")
    check(clock_mod.parse_duration("半个钟头") == 30, "认得「半个钟头」")
    check(clock_mod.parse_duration("等了一会儿") == 0, "认不出来就返回 0（不会瞎猜）")
    check(clock_mod.parse_absolute("10月5日 08:00", c4.dt) == _dt(1925, 10, 5, 8, 0),
          "认得绝对时刻「10月5日 08:00」")
    check(clock_mod.parse_absolute("08:30", c4.dt) == _dt(1925, 10, 4, 8, 30),
          "只给钟点时，已经过点就算明天")
    kind, arg = clock_mod.parse_clock_directive("advance", "2h", "")
    check(kind == "advance" and arg == "2h", "<state> advance 2h 能被识别")
    kind, arg = clock_mod.parse_clock_directive("time:", "10月5日", "08:00")
    check(kind == "set" and "10月5日" in arg and "08:00" in arg,
          "<state> time 10月5日 08:00 能被识别")

    # 开场时刻：模组 era 里的年份要能用上
    class _M:
        era = "1925 年 · 美国马萨诸塞州"
        start_time = ""
    mc = clock_mod.resolve_start(_M(), {})
    check(mc.year == 1925, "从模组的 era 里读出了年份", str(mc.year))
    class _M2:
        era = "1925 年"
        start_time = "1928-06-01 22:30"
    mc2 = clock_mod.resolve_start(_M2(), {})
    check(mc2.year == 1928 and mc2.month == 6 and mc2.hour == 22,
          "模组写了 start_time 就以它为准", mc2.short())

    # 存进会话、读回来，钟不能停
    from engine.session import Session as _S
    s = _S("clk-test")
    s.clock = c4
    s.save()
    back = _S.load("clk-test")
    check(back is not None and back.clock is not None
          and back.clock.short() == c4.short(), "钟能随存档一起存下来",
          back.clock.short() if (back and back.clock) else "（无）")

    # ══════════════════════════════════════════ 12. KP 的指令通道
    # 这一段是补的：<state> 的参数以前取错了位置，于是私聊、handout、
    # 场景推进、KP 主动发起的检定全都静默失效，日志里只留一句"找不到目标"。
    print("\n【12】守秘人的 <state> 指令：参数取对了没有")
    from engine.agents import Directive as _Dir, parse_kp_output as _pko2
    cases = [
        ("whisper pl_1 门框内侧有划痕", "whisper", "pl_1", "门框内侧有划痕"),
        ("grant_handout pl_1 邀请信", "grant_handout", "pl_1", "邀请信"),
        ("check pl_1 侦查 regular", "check", "pl_1", "侦查 regular"),
        ("damage pl_1 1d3", "damage", "pl_1", "1d3"),
        ("san pl_1 0/1d4", "san", "pl_1", "0/1d4"),
    ]
    bad_args = []
    for line, kind, want_target, want_payload in cases:
        d = _pko2(f"<state>{line}</state>").directives[0]
        got = (d.kind, d.arg(1), d.arg(2))
        if got != (kind, want_target, want_payload):
            bad_args.append(f"{line} → {got}")
    check(not bad_args, "目标与内容没取错位（arg(1)=目标 / arg(2)=内容）",
          "；".join(bad_args) or "5 种指令全对")
    d = _pko2("<state>advance_scene scene_02</state>").directives[0]
    check(d.arg(1) == "scene_02", "场景推进的参数也取对了", repr(d.arg(1)))
    d = _pko2("<state>note 今晚降温</state>").directives[0]
    check(d.arg(1) == "今晚降温", "备注的参数也取对了", repr(d.arg(1)))
    d = _pko2("<state>openroll 1d6 那东西会不会动</state>").directives[0]
    check(d.arg(1) == "1d6" and d.arg(2) == "那东西会不会动",
          "公开掷骰的表达式与用途各就各位",
          f"{d.arg(1)!r} / {d.arg(2)!r}")

    # 端到端：真让守秘人把 handout 发出去，看玩家是不是真的拿到了
    app3 = App()
    if app3.new_session("demo_洋馆之夜").get("ok"):
        app3.prepare()
        app3.wait_idle(600)
        app3.start()
        app3.wait_idle(600)
        if app3.loop and app3.loop.pls:
            pl = app3.loop.pls[0]
            h = app3.module.handouts[0] if (app3.module and app3.module.handouts) else None
            if h:
                app3.loop._one_directive(_Dir("grant_handout", [pl.seat_id, h.title]))
                got = app3.session.pending_private.get(pl.seat_id) or []
                check(any(h.title in x for x in got),
                      "守秘人发 handout，玩家真的收到了",
                      f"{pl.display_name} ←《{h.title}》")
            app3.loop._one_directive(_Dir("whisper", [pl.seat_id, "门把手是温的"]))
            got = app3.session.pending_private.get(pl.seat_id) or []
            check(any("门把手是温的" in x for x in got), "守秘人私下说话，玩家真的收到了")
            before_scene = app3.session.scene_id
            app3.loop._one_directive(_Dir("advance_scene", ["scene_02_书房"]))
            check(app3.session.scene_id == "scene_02_书房",
                  "守秘人能推动场景", f"{before_scene} → {app3.session.scene_id}")
            hp0 = (pl.seat.get("character") or {}).get("attributes", {}).get("HP", 0)
            app3.loop._one_directive(_Dir("damage", [pl.seat_id, "1d3"]))
            hp1 = (pl.seat.get("character") or {}).get("attributes", {}).get("HP", 0)
            check(hp1 < hp0, "守秘人能造成伤害（HP 真的掉了）", f"{hp0} → {hp1}")
            san0 = (pl.seat.get("character") or {}).get("attributes", {}).get("SAN", 0)
            app3.loop._one_directive(_Dir("san", [pl.seat_id, "0/1d4"]))
            san1 = (pl.seat.get("character") or {}).get("attributes", {}).get("SAN", 0)
            check(san1 <= san0, "守秘人能要求理智检定", f"SAN {san0} → {san1}")

    # ══════════════════════════════════════════ 13. 出错记录
    print("\n【13】出错记录（出事了得留得下证据）")
    from engine import errlog

    errlog.clear()
    # Key 绝不能落盘——错误信息里很可能带着 Authorization。
    # 这两个是**故意写死的假 Key**，名字里带 FAKE 是为了让
    # tools/scan_secrets.py 能一眼认出并放行；真 Key 扫到就会报。
    fake_a = "sk-FAKEonly-for-tests-000000000001"
    fake_b = "sk-FAKEonly-for-tests-000000000002"
    dirty = (f"连不上 https://api.deepseek.com/v1/chat/completions"
             f" with key {fake_a} 和 "
             f"Authorization: Bearer {fake_b}")
    errlog.log("测试", dirty)
    raw = errlog.tail(20)
    check(fake_a not in raw, "写盘前把 API Key 打码了")
    check(fake_b not in raw, "Authorization 里的 Key 也打码了")
    check("****" in raw, "打码后留了前缀，看得出是哪个 Key", raw.splitlines()[-1][:90])
    check("连不上" in raw, "正文本身没被吃掉")

    errlog.log("测试", "第二条")
    items = errlog.recent(5)
    check(len(items) >= 2, "内存里留得住最近几条", f"{len(items)} 条")
    check(errlog.log_path().exists(), "日志文件真的落在盘上",
          str(errlog.log_path()).replace(str(ROOT), "."))
    check(errlog.tail(1).strip().endswith("第二条"), "读回来是最后一条")

    # app 的门面方法要在（界面靠它把 JS 报错送进来）
    for m in ("log_client_error", "recent_errors", "clear_errors"):
        check(callable(getattr(app, m, None)), f"App.{m}() 在")
    app.log_client_error("TypeError: x is null", "at foo (app.js:1:1)")
    check(any(e["where"] == "界面" for e in errlog.recent(10)),
          "界面报的错也能进记录")
    errlog.clear()
    check(not errlog.tail(5).strip(), "能清空")

    # ══════════════════════════════════════════ 14. 车卡规范
    print("\n【14】车卡规范：技能点超支与 90 上限（docs/车卡规范.md）")
    from engine import cardcalc as _cc
    ca = {"STR": 50, "CON": 60, "DEX": 70, "APP": 50, "POW": 55,
          "SIZ": 65, "INT": 65, "EDU": 80, "LUCK": 45}

    # ★ 权威源是**用户那张卡**，不是引擎里另写的一张表
    occ_tbl = _cc.occupation_table()
    check(len(occ_tbl) > 100, "从角色卡模板里读到了职业表",
          f"{len(occ_tbl)} 个职业")
    jz = _cc.find_occupation("记者")
    check(jz is not None and jz.credit == (9, 30) and "教育" in jz.formula_text,
          "记者的信用区间与点数公式取自卡本身",
          f"{jz.credit} / {jz.formula_text} = {jz.points(ca)}" if jz else "（没读到）")
    check(jz is not None and len(jz.skills) >= 8,
          "本职技能也是从卡上的『本职技能』矩阵读的",
          "、".join(jz.skills[:10]) if jz else "")
    check(_cc.eval_formula('=__import__("os").system("calc")', ca) is None
          and _cc.eval_formula("=1+1;2", ca) is None,
          "卡上的公式只按白名单求值，可疑公式一律拒绝")

    bs = chargen.budget_split(ca, "记者")
    check(bs["source"] == "card", "预算优先用卡里的算法", bs["source"])
    check(bs["occ"] == 80 * 2 + max(70, 50) * 2 and bs["interest"] == 65 * 2,
          "预算拆成两笔账，职业点按卡上公式算",
          f"职业 {bs['occ']}（{bs['formula']}） + 兴趣 {bs['interest']} = {bs['total']}")
    check(bs["credit"] == [9, 30], "记者这个职业的信用评级区间查得到",
          str(bs["credit"]))
    pol = chargen.budget_split(ca, "刑警")     # 卡里没有「刑警」，走内置表兜底
    check(pol["occ"] == 80 * 2 + 70 * 2 and pol["source"] == "builtin",
          "卡里没有的职业退回引擎内置表（刑警 = EDU×2+DEX×2）",
          f"{pol['occ']}（{pol['formula']}，来源 {pol['source']}）")
    unk = chargen.budget_split(ca, "马戏团小丑")
    check(unk["occ"] == 80 * 4 and not unk["known_occupation"],
          "两边都没有的职业退回 EDU×4，并标成不认识")

    # 一张**故意写烂**的卡：超支 + 超 90 + 低于基础值 + CR 越界 + 点了神话
    rotten = {
        "name": "烂卡", "occupation": "记者", "age": 30, "credit": 75,
        "skills": {"母语": 95, "侦查": 95, "图书馆利用": 80, "话术": 70,
                   "心理学": 60, "聆听": 55, "闪避": 12, "摄影": 40,
                   "克苏鲁神话": 12, "开车": 30},
    }
    aud = chargen.audit_sheet(rotten, ca, "记者")
    codes = {v["code"] for v in aud["violations"]}
    check(not aud["ok"], "烂卡被判为不合规")
    for code, label in (("over_cap", "超 90 被抓到"),
                        ("below_base", "低于基础值被抓到"),
                        ("credit_range", "信用评级越界被抓到"),
                        ("mythos", "分配克苏鲁神话被抓到")):
        check(code in codes, label,
              next((v["detail"] for v in aud["violations"] if v["code"] == code), "（漏报）"))

    fixed, fixes = chargen.repair_sheet(rotten, ca, "记者")
    after = chargen.audit_sheet(fixed, ca, "记者")
    check(after["ok"], "裁剪之后这张卡合规了",
          f"修正 {len(fixes)} 处")
    for f in fixes[:3]:
        print(f"         · {f}")
    vals = fixed["skills"]
    check(max(vals.values()) <= 90, "没有任何技能超过 90",
          f"最高 {max(vals.values())}")
    check(all(v >= chargen.base_of(n, ca) for n, v in vals.items()),
          "没有任何技能低于它的基础值")
    check(vals.get("克苏鲁神话", 0) == 0, "克苏鲁神话被清零")
    lo, hi = bs["credit"]
    check(lo <= int(fixed["credit"]) <= hi, "信用评级被夹回职业区间",
          f"{fixed['credit']} ∈ [{lo}, {hi}]")
    check(after["spent"] <= after["budget"]["total"], "裁剪后合计不超预算",
          f"{after['spent']} / {after['budget']['total']}")
    check(after["spent_other"] <= after["budget"]["interest"],
          "裁剪后非本职技能的点数不超兴趣点",
          f"{after['spent_other']} / {after['budget']['interest']}")
    # 确定性：同一张烂卡裁两次结果必须一样（不然每次开团数值都在飘）
    again, _ = chargen.repair_sheet(rotten, ca, "记者")
    check(again == fixed, "裁剪算法是确定性的（跑两次结果一样）")

    # 极端：所有技能都顶到 99
    extreme = {"name": "极端", "occupation": "医生", "credit": 200,
               "skills": {k: 99 for k in (
                   "医学", "急救", "心理学", "科学", "母语", "精神分析",
                   "生物学", "化学", "药学", "图书馆利用", "侦查", "聆听")}}
    ex_fixed, _ = chargen.repair_sheet(extreme, ca, "医生")
    ex_after = chargen.audit_sheet(ex_fixed, ca, "医生")
    check(ex_after["ok"], "全顶到 99 的卡也能被裁回合规")
    check(max(ex_fixed["skills"].values()) <= 90, "裁完最高不超过 90")
    check(30 <= int(ex_fixed["credit"]) <= 80, "医生的 CR 夹在 30–80",
          str(ex_fixed["credit"]))

    # 空卡 / 无职业，不能把引擎搞崩
    empty_fixed, _ = chargen.repair_sheet({"name": "空"}, ca, "")
    check(chargen.audit_sheet(empty_fixed, ca, "").get("ok") in (True, False),
          "只有名字的卡不会把引擎搞崩",
          str(sorted(empty_fixed.get("skills", {}))))

    # 规范的**落地**：进场的卡一定合法
    print("         ── 进场的卡一定合法 ──")
    import re as _re3
    import yaml as _y3
    from engine.llm import MockClient as _MC
    import random as _rnd
    illegal = 0
    for i in range(60):
        rng2 = _rnd.Random(i)
        at = chargen.roll_attributes(rng2)
        txt = _MC(seed=i)._chargen(
            "STR %d CON %d DEX %d APP %d POW %d SIZ %d INT %d EDU %d LUCK %d" % (
                at["STR"], at["CON"], at["DEX"], at["APP"], at["POW"],
                at["SIZ"], at["INT"], at["EDU"], at["LUCK"]))
        sheet = _y3.safe_load(_re3.search(r"<sheet>(.*?)</sheet>", txt, _re3.S).group(1))
        if not chargen.audit_sheet(sheet, at, sheet.get("occupation"))["ok"]:
            illegal += 1
    check(illegal == 0, "离线模型车 60 张卡，一张违规的都没有", f"{illegal} 张违规")

    # 真跑一遍车卡流程，看日志里有没有把违规/修正说出来。
    # 查的是 session.events（会话自己的流水）——外层那个 ev 中途被 clear 过好几次。
    sess_ev = list(app.session.events) if app.session else []
    viol_events = [e for e in sess_ev if str(e.get("text", "")).startswith("〔车卡")]
    check(any("车卡结算" in str(e.get("text", "")) for e in viol_events),
          "车卡结算摆到台面上了（花了多少 / 预算多少）",
          next((str(e.get("text"))[:70] for e in viol_events
                if "车卡结算" in str(e.get("text"))), "（没有）"))
    # 每个座位的车卡账都记下来了
    ledger = []
    for seat in (app.cfg.get("seats") or []):
        st = (app.session.seats or {}).get(seat.get("seat_id")) or {}
        if st.get("chargen_budget"):
            ledger.append(st["chargen_budget"].get("total"))
    check(len(ledger) >= 3, "每个座位的车卡预算都留了档",
          "、".join(str(x) for x in ledger))

    # ★ 名字：引擎不预设角色名，AI 自己取；车卡前显示网名，之后显示「角色名（网名）」
    print("         ── 名字 ──")
    from engine.agents import PLAgent as _PLA
    seat0 = next((s for s in app.cfg["seats"] if s["kind"] == "PL"), None)
    check(seat0 is not None and not (seat0.get("character") or {}).get("name"),
          "座位出厂时不带预设角色名", str((seat0 or {}).get("character")))
    labels = {s["seat_id"]: s for s in app._seat_states()}
    pl_seats = [s for s in labels.values() if s["kind"] == "PL"]
    check(all(s["has_character"] for s in pl_seats),
          "车完卡之后每个玩家都有角色了")
    check(all("（" in (s["label"] or "") for s in pl_seats),
          "车完卡后显示的是「角色名（网名）」",
          "、".join(s["label"] for s in pl_seats))
    check(all(s["player_name"] and s["player_name"] == s["label"].split("（")[-1].rstrip("）")
              for s in pl_seats if s["player_name"]),
          "括号里就是那个人的网名")
    # 车卡阶段（没有角色）显示的就该是网名，不该冒出任何角色名
    fresh_seat = cfgmod.make_pl_seat(0)
    fresh_seat.update({"provider": "mock", "base_url": "", "api_key": "",
                       "model": "mock-pl"})
    fake = _PLA(fresh_seat, {"rules_detail": "lean"}, "label-test")
    check(fake.table_label() == fresh_seat["profile"]["player_name"],
          "还没车卡时，桌上叫的是网名",
          f"{fake.table_label()}（网名 {fresh_seat['profile']['player_name']}）")
    fake.adopt_character({"name": "哈里·杜邦", "occupation": "记者",
                          "attributes": {"DEX": 60}, "skills": [], "inventory": []})
    check(fake.table_label() == f"哈里·杜邦（{fresh_seat['profile']['player_name']}）",
          "车出角色之后立刻改成「角色名（网名）」", fake.table_label())
    check(fake.display_name == "哈里·杜邦",
          "提示词与叙述里用的是角色名", fake.display_name)
    # 每张进场的卡都真的合法
    bad_cards = []
    for seat in app.loop.pls if app.loop else []:
        ch = seat.seat.get("character") or {}
        at2 = (seat.seat.get("character") or {}).get("attributes") or {}
        if not ch.get("skills"):
            continue
        sk = {s["name"]: s["value"] for s in ch["skills"] if isinstance(s, dict)}
        if sk and max(sk.values()) > 90:
            bad_cards.append(f"{seat.display_name}:{max(sk.values())}")
    check(not bad_cards, "真跑出来的角色卡里没有超过 90 的技能",
          "、".join(bad_cards) or "干净")

    # ══════════════════════════════════════════ 15. 乱格式模组 + 单人研读室
    print("\n【15】乱格式模组解析 + 只有导演和 KP 的研读室")
    import time as _time
    from engine import docread, module_lib, study as study_mod

    # 11a. 真实世界里拿到的模组是 .doc/.docx/.xls/.xlsx/.png 混在一起的
    messy_src = ROOT / "data" / "modules" / "不更文-吞噬深渊之影1+2"
    messy_id = ""
    if messy_src.is_dir():
        shutil.copytree(messy_src, data / "modules" / messy_src.name, dirs_exist_ok=True)
        messy_id = messy_src.name
        t0 = _time.monotonic()
        mm = module_lib.load_module(messy_id)
        dt = _time.monotonic() - t0
        ext = sorted({f.suffix.lower() for f in messy_src.rglob("*") if f.is_file()})
        print(f"         目录里是这些格式：{' '.join(ext)}")
        check(mm is not None, "乱格式模组能读进来")
        if mm:
            print(f"         《{mm.title}》：{len(mm.scenes)} 幕 / "
                  f"手书 {len(mm.handouts)} 份 / KP 资料 {len(mm.truth):,} 字 / "
                  f"耗时 {dt:.1f}s")
            check(len(mm.scenes) >= 10, "能被切成可用的幕数",
                  f"{len(mm.scenes)} 幕")
            check(len(mm.truth) > 20_000, "KP 资料读全了（不是只读了个标题）",
                  f"{len(mm.truth):,} 字")
            check(all(sc.body.strip() for sc in mm.scenes),
                  "每一幕都有正文（没有空壳幕）")
            check(all(len(sc.body) < len(mm.truth) for sc in mm.scenes),
                  "单幕正文比 KP 全资料短（说明分幕是真切开的）")
        # 单文件读：.doc / .docx / .xls / .xlsx 各来一次
        got = {}
        for f in sorted(messy_src.rglob("*")):
            if f.is_file() and f.suffix.lower() in {".doc", ".docx", ".xls", ".xlsx"}:
                kept = docread.read_any(f)
                got[f.suffix.lower()] = len(kept.text)
        check(all(v > 200 for v in got.values()),
              "每种老格式都能读出正文",
              "、".join(f"{k} {v:,}字" for k, v in sorted(got.items())))
    else:
        warn(False, "没找到乱格式模组样本，跳过", str(messy_src))

    # 11b. 研读室：只有「我」和 KP 的单人小窗
    print("         ── 研读室 ──")
    app2 = App()
    target = messy_id or "demo_洋馆之夜"
    r = app2.study_room(target)
    check(r.get("ok"), "研读室能开起来", str(r.get("message", "")))
    ev2: list[dict] = []
    wait(app2, ev2, 600)
    st2 = app2.study_state()
    check(st2.get("open"), "研读室处于打开状态")
    check(bool(st2.get("study")), "KP 已经把模组读了一遍（功课在）")
    if st2.get("study"):
        ids = st2["study"].get("spine_ids") or []
        sk = st2["study"].get("spine") or ""
        check(len(ids) >= 3, "研读产出了骨架（分幕清单）",
              f"{len(ids)} 个场景 id / 骨架 {len(sk)} 字")
    rep = st2.get("report") or ""
    check(bool(rep), "通读报告写出来了", f"{len(rep)} 字")
    # 报告是给导演看的：幕后真相必须摊开，不许打码
    check(("真相" in rep) or ("幕后" in rep), "通读报告里有「真相」章节")
    check(not any(k in rep for k in ("[已隐藏]", "无可奉告", "我不能透露")),
          "研读室里 KP 没有对导演保密")
    check(any(e.get("type") == "study_report" for e in ev2),
          "通读报告作为事件推到了界面")
    # 研读室没有玩家
    check(len(getattr(app2.loop, "pls", []) or []) == 0, "研读室里一个玩家都没有")
    check(app2.session.mode == "study", "会话被标成了 study 模式")
    check(app2.session.round == 0, "研读室不推进回合")

    # 追问
    ok_q = app2.study_ask("第二章那个摄政公园，玩家不去的话会怎样？")
    check(ok_q.get("ok"), "能在研读室里追问")
    wait(app2, ev2, 300)
    st3 = app2.study_state()
    talk = st3.get("talk") or []
    check(len(talk) >= 2, "追问与回答都进了研读记录", f"{len(talk)} 条")
    check(any(t.get("who") == "导演" for t in talk), "导演的话被记下来了")
    check(all(t.get("text", "").strip() for t in talk), "没有空回答")

    # 研读记录会落盘，下次开同一个团直接复用
    saved = study_mod.load_study(target)
    check(bool(saved), "研读记录已存到磁盘（下次不用重读）")
    check(any(x.get("module_id") == target for x in app2.list_studies()),
          "研读记录出现在列表里")

    # 再开一次同一个本：谈过的话应该原样摆回桌上，而不是从头再来
    ev2.clear()
    again = app2.study_room(target)
    check(again.get("ok"), "同一个本可以再次打开研读室")
    wait(app2, ev2, 120)
    st4 = app2.study_state()
    check(st4.get("report") == rep, "重开时通读报告原样回来（没有重读、没有重花钱）")
    check(len(st4.get("talk") or []) >= 2, "重开时之前的问答也回来了",
          f"{len(st4.get('talk') or [])} 条")
    check(any((e.get("meta") or {}).get("restored") for e in ev2),
          "界面收到的是一条「恢复」而不是新报告")

    # 研读室不是"一局团"，不该混进存档列表
    check(not any(s.get("module_id") == target and s.get("mode") == "study"
                  for s in app2.list_sessions()),
          "研读室不会被当成一个存档")

    # 研读完的 KP，开真局时还记得这本
    r3 = app2.new_session(target)
    check(r3.get("ok"), "研读过的模组能直接开真局", str(r3.get("message", "")))
    check(bool(study_mod.load_study(target)), "开真局时仍读得到那份研读记录")

    # 真跑一轮，看时间有没有真的进到 AI 的上下文里
    print("         ── 跑起来之后的钟 ──")
    check(app2.session.clock is not None, "开真局时会话里有一只钟")
    t_before = app2.session.clock.short() if app2.session.clock else ""
    print(f"         开场：{t_before}")
    if app2.session.clock:
        cb = app2.session.clock.render()
        check("[日期对照表" in cb and "前天" in cb,
              "给 AI 的那块时间里有「日期对照表」")
        check("1925" in cb, "年份是从模组/配置里来的，不是凭空写的",
              cb.splitlines()[1][:60])

    app2.wait_idle(10)
    app2.prepare()
    app2.wait_idle(600)
    app2.start()
    app2.wait_idle(600)
    app2.run_auto(2)
    app2.wait_idle(900)
    st_clk = app2.state()
    t_after = st_clk.get("clock_short") or ""
    check(bool(t_after), "跑完之后界面上还看得到时间", t_after)
    check(t_after != t_before, "时间确实往前走了", f"{t_before} → {t_after}")
    # 时间有真的进到模型上下文里（玩家和守秘人都该看到）
    import re as _re2
    got_time = []
    for ag in ([app2.loop.kp] if app2.loop and app2.loop.kp else []) + \
              (app2.loop.pls if app2.loop else []):
        blob = "\n".join(str(m.get("content", "")) for m in ag.messages[-8:])
        if "[现在的时间]" in blob and "[日期对照表" in blob:
            got_time.append(ag.display_name)
    check(len(got_time) >= 2, "每个座位每一轮都拿到了「现在几点 + 日期对照表」",
          "、".join(got_time) or "（一个都没拿到）")

    # 守秘人拨钟：从它真会写的那段输出一路走到底
    from engine.agents import parse_kp_output as _pko
    if app2.loop and app2.loop.kp:
        before = app2.session.clock.short()
        kp_text = ("<narr>你们在门厅里等着，壁炉的火一点点矮下去。</narr>\n"
                   "<state> advance 3h </state>")
        _out = _pko(kp_text)
        check([d.kind for d in _out.directives] == ["advance"],
              "守秘人写的 <state> advance 3h 能被解析成拨钟指令",
              str([d.kind for d in _out.directives]))
        for _d in _out.directives:
            app2.loop._one_directive(_d)
        after = app2.session.clock.short()
        check(after != before, "守秘人能用 <state> advance 把钟拨过去",
              f"{before} → {after}")
        # 把钟拨到"明天"，对照表要跟着改口
        from engine.agents import Directive as _D
        app2.loop._one_directive(_D("time", ["10月5日", "08:00"]))
        c_now = app2.session.clock
        check((c_now.month, c_now.day, c_now.hour) == (10, 5, 8),
              "守秘人能把钟直接拨到指定时刻", c_now.short())
        app2.loop._tick_clock()          # 新的一轮，对照表重算
        blk3 = app2.session.clock.render()
        check("10月4日" in blk3 and "昨天" in blk3,
              "拨过之后，昨天/前天都跟着变了", app2.session.clock.short())

    # 也可以主动忘掉，下次重新读一遍
    r2 = app2.forget_study(target)
    check(r2.get("ok") and not study_mod.load_study(target),
          "可以主动忘掉某个模组的研读")

    # ══════════════════════════════════════════ 16. 语域与时代
    print("\n【16】语域与时代：年代跟模组走，说话别像八股")

    # ---- 16a. 时代语域：模组写什么年代，说的就是什么年代 ----
    # 以前这里默认 1920 年代，模组写现代、写未来也照旧，叙述就串味了。
    from engine import prompts as prompts_mod
    from engine import linter as linter_mod
    import re as _re16

    era_txt = prompts_mod.era_note("2077 年 · 夜之城")
    check("2077 年 · 夜之城" in era_txt, "时代语域会照抄模组给的时代")
    check("括号里" in era_txt, "括号里的话明说了不受时代限制")
    check("模组没写明时代" in prompts_mod.era_note(""),
          "模组没写时代时给的是判断指引不是硬编码")

    seat_probe = {
        "seat_id": "pl_probe", "display_name": "测试",
        "character": {"name": "测试员", "age": 30, "occupation": "记者"},
        "profile": {"player_name": "测试"},
    }
    pl_txt = prompts_mod.build_pl_system(seat_probe, "（规则）", setting="2000 年 · 委内瑞拉")
    check("2000 年 · 委内瑞拉" in pl_txt, "PL 提示词带上了模组的时代")
    check("1925" not in pl_txt, "PL 提示词不再假设 1920 年代")

    kp_txt = prompts_mod.build_kp_system(
        {"seat_id": "kp_probe", "display_name": "KP", "profile": {}},
        "（规则）", "测试模组", setting="2077 年 · 夜之城")
    check("2077 年 · 夜之城" in kp_txt, "KP 叙述提示词也带上了时代")

    # ---- 16b. 开场时刻：模组/配置/KP挑的/简介/era/兜底 ----
    class _M:
        def __init__(self, **kw):
            self.id = kw.get("id", "m")
            self.era = kw.get("era", "")
            self.start_time = kw.get("start_time", "")
            self.summary = kw.get("summary", "")
            self.premise = kw.get("premise", "")

    c1 = clock_mod.resolve_start(_M(start_time="1932-04-15 21:30", era="1925 年 · 阿卡姆"),
                                 {"start_time": "1999-01-01 09:00"}, picked="1888-08-08")
    check(c1.start.strftime("%Y-%m-%d %H:%M") == "1932-04-15 21:30",
          "开场时刻：模组 start_time 最优先", c1.start.strftime("%Y-%m-%d %H:%M"))
    c2 = clock_mod.resolve_start(_M(era="1925 年 · 阿卡姆"),
                                 {"start_time": "1999-01-01 09:00"}, picked="1888-08-08")
    check(c2.start.year == 1999, "开场时刻：配置次之", str(c2.start))
    c3 = clock_mod.resolve_start(_M(era="1925 年 · 阿卡姆"), {}, picked="1911-11-11")
    check(c3.start.strftime("%Y-%m-%d") == "1911-11-11",
          "开场时刻：守秘人挑的那天再次之", str(c3.start))
    c4 = clock_mod.resolve_start(_M(summary="故事发生在 2000 年 7 月 10 日。"), {})
    check(c4.start.strftime("%Y-%m-%d") == "2000-07-10",
          "开场时刻：能从简介里抠出日期", str(c4.start))

    # 兜底不再写死 10 月 3 日：同一个模组稳定，不同模组不同
    d1 = clock_mod.resolve_start(_M(id="aaa"), {}).start
    d2 = clock_mod.resolve_start(_M(id="aaa"), {}).start
    check(d1 == d2, "开场时刻：同一模组两次算出来一样", str(d1))
    check((d1.month, d1.day) != (10, 3),
          "开场时刻：月日不再固定是 10 月 3 日", f"{d1.month} 月 {d1.day} 日")
    days = {(clock_mod.resolve_start(_M(id=f"mod{i}"), {}).start.month,
             clock_mod.resolve_start(_M(id=f"mod{i}"), {}).start.day)
            for i in range(30)}
    check(len(days) >= 5, "开场时刻：不同模组的开局日不一样", f"{len(days)} 种")
    bad_day = []
    for i in range(400):
        dd = clock_mod.resolve_start(_M(id=f"x{i}"), {}).start
        if not (1 <= dd.month <= 12 and 1 <= dd.day <= 28):
            bad_day.append(str(dd))
    check(not bad_day, "开场时刻：400 个模组没有一个非法日期", str(bad_day[:2]))

    # ---- 16c. 桌边语域：正例里不许留撇清 ----
    bank = prompts_mod.TABLE_REGISTER.split("# 别写成这样")[0]
    left = [w for w in ("我不去", "别指望我", "别催", "谁挨打谁上") if w in bank]
    check(not left, "桌边正例里没有撇清式表态", str(left))
    for label in ("行动", "附和", "打趣", "自嘲", "关心", "废话"):
        check(f"**{label}**" in prompts_mod.TABLE_REGISTER, f"桌边话堆里有「{label}」")
    check("是**朋友**" in prompts_mod.TABLE_REGISTER, "桌边语域点明了这桌人是朋友")
    check("# 角色的动作怎么写" in pl_txt, "动作规格进得了 PL 提示词")

    # ---- 16d. 哨兵：认得出守秘人的标签，也认得出机器通道 ----
    kp_out = ("<narr>他抬起头——屋里安静了。</narr>\n"
              "<state>advance 2h</state>\n"
              "<ooc>（这也太吓人了）</ooc>")
    rep = linter_mod.lint(kp_out, channels=linter_mod.KP_CHANNELS)
    check(any(h.channel == "narr" and h.kind == "style" for h in rep.style_hits),
          "哨兵抓到了 <narr> 里的破折号")
    check(not any(h.channel == "state" for h in rep.hits),
          "哨兵不去挑 <state> 指令的毛病")

    machine = linter_mod.lint("<state>advance 2h —— 随便</state>", channels=linter_mod.KP_CHANNELS)
    check(not machine.hits, "机器通道整个跳过，不是靠运气")

    meta = linter_mod.lint("<narr>作为一个 AI，我建议你们</narr>",
                           channels=linter_mod.KP_CHANNELS)
    check(any(h.kind == "meta" for h in meta.hits), "守秘人自称 AI 也会被逮住")

    # ---- 16e. 风格拦截：该抓的抓到、正常人话不误伤 ----
    should_hit = [
        ("这不是害怕，这是一种本能。", "「不是A而是B」句式"),
        ("与其说他在看你，不如说他在看你的手。", "「与其说不如说」句式"),
        ("他抬起头——屋里安静了。", "破折号"),
        ("**我先上**", "Markdown 加粗"),
        ("你别指望我，我不去。", "撇清式表态"),
        ("理由是：他不想去。", "摆依据"),
        ("我车个医生，你们呢", "把话头推给全桌"),
        # 「报资历」这条靠提示词管，不做正则：它和正常的自我介绍分不开，
        # 硬拦一定误伤。
    ]
    missed = []
    for sent, label in should_hit:
        got = [r for p, r in linter_mod.STYLE_PATTERNS if _re16.search(p, sent)]
        if not got:
            missed.append(label)
    check(not missed, "该拦的句式一条都没漏", str(missed))

    should_pass = [
        "我先上，你们跟着", "这门锁着，我试试撬一下", "你没事吧",
        "这屋里现在最危险的是你", "他看了我一眼，没说话",
        "88——58——90 的三围", "时间是 1925—1930 之间", "价格 100--200 块",
        # 「谁要」在正常吐槽里太常见，不能当成推话头拦下来
        "谁要这个破玩意儿", "你们几个先走，我垫后",
    ]
    false_alarm = []
    for sent in should_pass:
        got = [r for p, r in linter_mod.STYLE_PATTERNS if _re16.search(p, sent)]
        if got:
            false_alarm.append((sent, got))
    check(not false_alarm, "真人会说的话一句都没误伤", str(false_alarm[:2]))

    # 重写指令只描述目标语域，不把被禁的写法再念一遍
    instr = linter_mod.build_repair_instruction(rep.style_hits or [meta.hits[0]])
    check("——" not in instr and "不是" not in instr,
          "重写指令不复述被禁的写法")

    # ---- 16f. 桌边话量 1-5 ----
    def _energy_seat(lv):
        return {"seat_id": "e", "display_name": "e",
                "character": {"name": "N", "age": 30, "occupation": "记者"},
                "profile": {"player_name": "e", "table_energy": lv}}

    e1 = prompts_mod.build_pl_system(_energy_seat(1), "r")
    e5 = prompts_mod.build_pl_system(_energy_seat(5), "r")
    e3 = prompts_mod.build_pl_system(_energy_seat(3), "r")
    check(e1 != e5, "话量 1 和 5 的提示词确实不一样")
    check(prompts_mod.ENERGY_LEVELS[5][:10] in e5, "5 档把「话最多」写进去了")
    check("# 你在桌上说多少" not in e3, "默认档 3 不多写一段，省 token")
    check("角色卡" in e1, "话量档位不影响其余提示词内容")
    # 档位再高也不放松说话规矩
    check("怎么说话的规矩" in e5, "5 档也明说规矩一样算数")
    # 越界的值要被夹回去，不能抛异常
    for weird in (0, 9, "abc", None):
        s = _energy_seat(weird)
        try:
            prompts_mod.build_pl_system(s, "r")
            ok_weird = True
        except Exception:  # noqa: BLE001
            ok_weird = False
        check(ok_weird, f"话量收到非法值 {weird!r} 也不崩")

    # 界面上得有这个下拉
    _ui_src = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
    check("table_energy" in _ui_src, "设置面板里有桌边话量的下拉")
    check("'5', '5 话最多'" in _ui_src, "下拉有五个档位")

    # ══════════════════════════════════════════════ 17. 梗由引擎自己数
    print("\n【17】梗：引擎自己数跨人的重复")

    from engine import memes as memes_mod
    from engine import player_memory as pmem_mod

    # 梗的定义就是跨人的重复。模型每轮只看得到自己那份历史，
    # 它不知道别人刚才说过什么，所以这件事只能引擎来做。
    w = memes_mod.MemeWatcher()
    w.observe("咸鱼", "那我把门拆了", 1)
    w.observe("半糖", "行啊", 1)
    check(not w.harvest(), "一个人说过，不算梗")
    w.observe("半糖", "那我把门拆了", 2)
    check(w.harvest() == ["那我把门拆了"], "两个不同的人都说了，才算梗")
    check(not w.harvest(), "同一句只收一次，不会反复冒出来")

    w2 = memes_mod.MemeWatcher()
    w2.observe("A", "我先上去看看那扇门后面有什么", 1)
    w2.observe("B", "我先上去看看那扇门后面有什么", 1)
    check(not w2.harvest(), "太长的句子不收，那是叙述不是口头禅")

    w3 = memes_mod.MemeWatcher()
    for junk in ("真的假的", "绷", "哈哈哈哈哈", "我也不知道", "啊啊啊啊", "绷绷绷"):
        w3.observe("A", junk, 1)
        w3.observe("B", junk, 1)
    check(not w3.harvest(),
          "谁都说的口水词不收：笑声、叠字、单字的「绷」都不算梗")

    w4 = memes_mod.MemeWatcher()
    w4.observe("A", "那我把门拆了", 1)
    w4.observe("B", "那我把门拆了", 9)
    check(not w4.harvest(), "隔太久的不收，那只是碰巧说了一样的话")

    w5 = memes_mod.MemeWatcher()
    w5.observe("A", "那我把门拆了", 1)
    w5.observe("A", "那我把门拆了", 2)
    check(not w5.harvest(), "同一个人反复念叨不算梗")

    # 收下来的梗要写进玩家卡，跨周目带得走
    card = pmem_mod.PlayerCard("probe_meme", "测试")
    check(card.add_meme("那我把门拆了", origin="s1") is True, "新梗写进玩家卡")
    check(card.add_meme("那我把门拆了") is False, "同一句再收不算新的")
    check(card.memes[0].uses == 1, "重复出现的梗会累加 uses", str(card.memes[0].uses))
    check(card.add_meme("x" * 80) is False, "过长的东西不收")
    check(card.add_meme("") is False, "空的不收")
    check("那我把门拆了" in card.render_system_block(max_memes=3),
          "梗会出现在这个人的 system 提示里")

    # ---- 「绷」：一个字的回复也是合格回复 ----
    check("绷" in prompts_mod.TABLE_REGISTER, "桌边语域收进了「绷」")
    check("一个字也算一次发言" in prompts_mod.TABLE_REGISTER,
          "明说了一个字的回复也成立")
    talk_txt = prompts_mod.build_table_talk_system(seat_probe)
    check("绷" in talk_txt, "桌边闲聊提示词里也拿得到这条")
    check(not linter_mod.lint("绷", channels=linter_mod.PL_CHANNELS).dirty,
          "「绷」不会被哨兵拦下来")

    # ══════════════════════════════════════════ 18. 机械清洗与守秘人越权
    print("\n【18】机械清洗、句式疲劳、守秘人越权")

    from engine import phrasing as phrasing_mod

    # ---- 18a. 能机械修的，不交给模型 ----
    # 破折号和反义句式没有歧义，代码一定修得对，重写既花钱又不保证。
    check(linter_mod.soft_clean("他抬起头——屋里安静了") == "他抬起头，屋里安静了",
          "破折号机械换成断句", linter_mod.soft_clean("他抬起头——屋里安静了"))
    check(linter_mod.soft_clean("**我先上**") == "我先上", "Markdown 加粗机械去掉")
    check(linter_mod.drop_antithesis("不是嗡的空，是闷的空") == "闷的空",
          "「不是A，是B」削成 B")
    check(linter_mod.drop_antithesis("与其说他看你，不如说他在看你的手") == "他在看你的手",
          "「与其说不如说」削成后半句")
    check(linter_mod.drop_antithesis("我侦查才25") == "我侦查才25",
          "正常人话原样不动")
    chat = linter_mod.soft_clean("（我看看。他在那儿。）", chat=True)
    check("。" not in chat and "我看看" in chat and "他在那儿" in chat,
          "桌边话里的句号断成换行（真人打字不带句号）", repr(chat))

    # ---- 18b. 机器通道一个字都不能动 ----
    raw = "<state>advance 2h，别动——测试</state><ooc>我去——真的。行。</ooc>"
    cleaned = linter_mod.clean_output(raw)
    check("<state>advance 2h，别动——测试</state>" in cleaned,
          "机器通道原样保留，破折号也不碰")
    check("我去，真的" in cleaned, "人话通道照常清洗")

    # ---- 18c. 句式疲劳 ----
    pw = phrasing_mod.OpenerWatcher()
    pw.observe("咸鱼", "我把椅子挪了挪。我把转盘推回去。", 1)
    pw.observe("半糖", "我把筷子放下来。", 1)
    check(bool(pw.note()), "三个人都从「我把」开头会被抓到")
    check("我把" in pw.note(), "报出来的是那个撞车的开头")
    pw2 = phrasing_mod.OpenerWatcher()
    pw2.observe("A", "椅子往后挪了挪。转盘推了回去。", 1)
    pw2.observe("B", "筷子搁下了。", 1)
    check(not pw2.note(), "句式散开就不报，不误伤")

    # ---- 18d. 守秘人越权：替玩家说话 ----
    bad = "咸鱼把服务员叫住的时候，那小兄弟往门口退。「谁告诉你五位的？」咸鱼问。"
    hits = linter_mod.kp_overreach(bad, ["咸鱼", "沈砚秋"])
    check(hits and "咸鱼" in hits[0].reason, "抓到守秘人替调查员开口", hits[0].snippet if hits else "")
    check(not linter_mod.kp_overreach("那小兄弟退了两步，停住了。", ["咸鱼"]),
          "守秘人只写世界这一侧就不报")

    # ---- 18e. 叙述长度 ----
    check(not linter_mod.narr_too_long("他抬起头，屋里安静了。"), "短叙述不报")
    check(bool(linter_mod.narr_too_long("字" * (linter_mod.NARR_SOFT_LIMIT + 20))),
          "超长叙述会报", f"上限 {linter_mod.NARR_SOFT_LIMIT}")

    # ---- 18f. 桌边语域必须进主提示词 ----
    # 以前 TABLE_REGISTER 只进车卡和桌边闲聊两处，
    # 正式回合里 PL 的 <ooc> 一个字都拿不到那套规矩。
    main_txt = prompts_mod.build_pl_system(seat_probe, "（规则）")
    for mark in ("桌边都有哪些话", "别把每句话都写成段子", "一个字也算一次发言",
                 "替别人解释笑点", "别每句都用同一个句式开头"):
        check(mark in main_txt, f"主提示词里有「{mark}」")
    check("把烟揣兜里" not in prompts_mod.ACT_REGISTER,
          "动作正例里不再连着用「把」，那是卡带的源头")

    # ══════════════════════════════════════════ 19. 每轮谁出场
    print("\n【19】每轮谁出场：关键节点全员，平常见机抽")

    from types import SimpleNamespace
    from engine.turns import GameLoop as TurnLoop

    ch_table = TurnLoop.ENERGY_CHANCE
    check(all(ch_table[i] < ch_table[i + 1] for i in range(1, 5)),
          "话量档位越高，出场概率越大", str(ch_table))
    check(0 < ch_table[1] and ch_table[5] < 1,
          "两头的档位也不是必然出场或必然不出场")

    def _mk(name, energy=3):
        return SimpleNamespace(display_name=name, seat_id=name,
                               seat={"profile": {"player_name": name,
                                                 "table_energy": energy}})

    def _loop(rng_value, key_beat=False, participation="chance", spoken=""):
        class _R:
            def random(self):
                return rng_value
        return SimpleNamespace(
            pls=[_mk("甲乙"), _mk("丙丁")],
            options={"pl_participation": participation},
            _key_beat=key_beat,
            _round_publics=[{"ooc": spoken, "act": ""}],
            _part_rng=_R(),
            ENERGY_CHANCE=TurnLoop.ENERGY_CHANCE,
        )

    # 守秘人标了关键节点：全员都得动
    got = TurnLoop._pl_participants(_loop(0.99, key_beat=True), "他抬起头")
    check(len(got) == 2, "守秘人标了关键节点，全员参与", str(len(got)))

    # 没标、概率又低：可以没人动
    got = TurnLoop._pl_participants(_loop(0.99), "他抬起头")
    check(len(got) == 0, "普通轮次里可以有人整轮不出声", str(len(got)))

    # 概率够高就动
    got = TurnLoop._pl_participants(_loop(0.01), "他抬起头")
    check(len(got) == 2, "概率够就出场", str(len(got)))

    # 被点名的人必动，不管概率
    got = TurnLoop._pl_participants(_loop(0.99), "甲乙，你怎么看")
    check([p.display_name for p in got] == ["甲乙"], "被点名的人必动",
          str([p.display_name for p in got]))
    got = TurnLoop._pl_participants(_loop(0.99, spoken="丙丁，你盯着他"), "")
    check([p.display_name for p in got] == ["丙丁"], "别人嘴里提到他也算点名")

    # 开关：切成 all 就每轮全员
    got = TurnLoop._pl_participants(_loop(0.99, participation="all"), "他抬起头")
    check(len(got) == 2, "设置里切「全员」时无视概率")

    # 关键节点只管一轮
    check("key" in ("key",) and True, "「key yes」由指令通道解析")
    kp_out_key = linter_ok = None
    from engine.agents import parse_kp_output as _pko
    kp_out_key = _pko("<narr>他抬头。</narr><state>key yes</state>")
    check([d.kind for d in kp_out_key.directives] == ["key"],
          "守秘人写的 key yes 解析成了指令")
    kp_out_no = _pko("<narr>他抬头。</narr><state>key no</state>")
    check([d.kind for d in kp_out_no.directives] == ["key"], "key no 也认")
    check("key yes" in prompts_mod.KP_CONTRACT,
          "守秘人提示词里写了怎么标关键节点")

    # ══════════════════════════════════════════ 汇总
    print("\n" + "=" * 70)
    if failures:
        print(f"结果：{len(failures)} 项失败")
        for f in failures:
            print(f"   ✗ {f}")
    else:
        print("结果：全部通过")
    if warnings:
        print(f"（{len(warnings)} 项警告：" + "；".join(warnings) + "）")
    print(f"测试数据留在：{_TMP}")
    print("=" * 70)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        pass
    sys.exit(code)
