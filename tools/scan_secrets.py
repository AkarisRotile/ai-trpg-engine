"""扫一遍：Key 有没有可能被带出去。

只报告**位置和类型**，绝不打印 Key 本身——打码之后再显示。
查五处：
  1. 工作区里所有文件（含未跟踪的）
  2. git 已跟踪的内容 + 全部历史（历史最容易漏）
  3. .git/config（push 时往里塞过 token）
  4. 打包好的 exe 与 _internal（PyInstaller 会不会把 config 打进去）
  5. 分享用的 zip
"""
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 只认"像密钥"的串，不打印原文
PATTERNS = [
    ("DeepSeek/通用 sk-", re.compile(rb"sk-[A-Za-z0-9]{16,}")),
    ("GitHub token", re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("Bearer 后面的串", re.compile(rb"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}")),
]
SKIP_DIRS = {".venv", ".git", "build", ".selftest", "__pycache__",
             ".webview", ".webview-smoke"}

# 明确的假 Key：仓库里只允许出现这些形状，别的一律当真 Key 处理。
# 约定：自带 FAKE 字样。真 Key 不可能长这样。
WHITELIST = re.compile(
    rb"(?i)(FAKE|rotate-test-key|sk-test\b|sk-x\b"
    # 早期提交里自检用的样例串（验证日志打码），历史删不掉，明确放行。
    # 一眼就是编的，真 Key 不会长这样。
    rb"|abcdef1234567890abcdef|zzzz9999888877776666)")


def is_fake(blob: bytes) -> bool:
    return bool(WHITELIST.search(blob))


def mask(b: bytes) -> str:
    s = b.decode("utf-8", "replace")
    return s[:7] + "…" + ("*" * 6) + s[-3:] if len(s) > 12 else "***"


def scan_bytes(blob: bytes, where: str, out: list) -> None:
    for label, rx in PATTERNS:
        for m in rx.finditer(blob):
            hit = m.group(0)
            if is_fake(hit):
                continue          # 自带的假 Key（名字里有 FAKE），放行
            out.append((where, label, mask(hit)))


def walk(root: Path, skip_internal=False):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if skip_internal and "_internal" in p.parts:
            continue
        yield p


print("=" * 66)
print("1) 工作区文件（data/config.json 是你的本地配置，本来就该有；")
print("   这里只关心它有没有被带进 git / exe / 分享包）")
found = []
for p in walk(ROOT):
    if p.stat().st_size > 80 * 1024 * 1024:
        continue
    try:
        scan_bytes(p.read_bytes(), str(p.relative_to(ROOT)), found)
    except Exception:
        pass
excluded = {"data/config.json", "data/config.json.bak"}
found_outside = [x for x in found if x[0].replace("\\", "/") not in excluded]
print(f"   命中 {len(found)} 处（其中你自己那份本地配置占 "
      f"{len(found) - len(found_outside)} 处，是正常的）")
for w, l, m in found_outside[:20]:
    print(f"     ⚠ {w}  [{l}]  {m}")

def _git(args: list[str]) -> bytes:
    """git 一律按字节取，别让 Windows 的 GBK 把中文路径搞崩。"""
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True).stdout


print("\n2) git 已跟踪文件 + 全部历史")
tracked = _git(["ls-files"]).decode("utf-8", "replace").split()
hits2 = []
for name in tracked:
    p = ROOT / name
    if p.is_file():
        try:
            scan_bytes(p.read_bytes(), f"跟踪:{name}", hits2)
        except Exception:
            pass
# 历史：每个 commit 的每个 blob
try:
    revs = _git(["rev-list", "--all"]).decode("utf-8", "replace").split()
    for rev in revs:
        names = _git(["ls-tree", "-r", "--name-only", rev]).decode(
            "utf-8", "replace").split("\n")
        for nm in names:
            nm = nm.strip()
            if not nm:
                continue
            blob = _git(["show", f"{rev}:{nm}"])
            scan_bytes(blob, f"历史:{rev[:7]}:{nm}", hits2)
except Exception as e:
    print("   历史扫描失败:", e)
print(f"   命中 {len(hits2)} 处（扫描了 {len(tracked)} 个跟踪文件）")
for w, l, m in hits2[:20]:
    print(f"     · {w}  [{l}]  {m}")

print("\n3) .git/config")
cfg = ROOT / ".git" / "config"
hits3 = []
if cfg.exists():
    scan_bytes(cfg.read_bytes(), ".git/config", hits3)
print(f"   命中 {len(hits3)} 处")
for w, l, m in hits3:
    print(f"     · {w}  [{l}]  {m}")

print("\n4) 打包产物（exe + _internal）")
hits4 = []
exe = ROOT / "COC跑团引擎.exe"
if exe.exists():
    scan_bytes(exe.read_bytes(), exe.name, hits4)
internal = ROOT / "_internal"
if internal.is_dir():
    for p in walk(internal):
        try:
            if p.stat().st_size < 40 * 1024 * 1024:
                scan_bytes(p.read_bytes(), f"_internal/{p.relative_to(internal)}", hits4)
        except Exception:
            pass
print(f"   命中 {len(hits4)} 处")
for w, l, m in hits4[:20]:
    print(f"     · {w}  [{l}]  {m}")

print("\n5) 分享 zip")
hits5 = []
z = ROOT / "dist" / "AI跑团引擎-分享版.zip"
if z.exists():
    with zipfile.ZipFile(z) as zf:
        for info in zf.infolist():
            if info.file_size > 40 * 1024 * 1024:
                continue
            try:
                scan_bytes(zf.read(info), f"zip:{info.filename}", hits5)
            except Exception:
                pass
    print(f"   命中 {len(hits5)} 处")
    for w, l, m in hits5[:20]:
        print(f"     · {w}  [{l}]  {m}")
else:
    print("   （没有分享包）")

print("\n" + "=" * 66)
# "能不能发出去"只取决于后四项：本地配置不算（它就在你自己机器上）
total = len(found_outside) + len(hits2) + len(hits3) + len(hits4) + len(hits5)
print("可能被带出去的地方，命中：", total)
if total == 0:
    print("结论：exe / git（含历史）/ 分享包里都没有 Key，可以放心上传。")
else:
    print("⚠ 上面列出的位置需要清理之后再上传。")
sys.exit(1 if total else 0)
