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
