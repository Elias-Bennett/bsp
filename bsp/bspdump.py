#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bspdump.py — 解析 collect.sh 产出的诊断包（主机侧，纯离线，只用标准库）

为什么需要它：
    一份 boot profile 的采集输出现场就有 100~300 KB。整段贴进模型上下文，
    模型会废掉，而且大概率抓不住重点。这里的做法是先在本地做一次"降维"：
    只把信号（崩溃、deferred、只读 rootfs、异常温度…）提炼成十几行表格，
    模型看完再决定读哪一段。

子命令
------
  list   FILE                 列出包里有哪些段、多大、是否被截断
  stat   FILE                 信号摘要（给模型看的第一屏）
  show   FILE SECTION [--grep RE] [--head N]
                              看某一段的原文或过滤结果
  diff   A B [--full]         两个包逐段比对（好板 vs 坏板、改前 vs 改后）

用法
----
  python bspdump.py list  dumps/bspdump-20251025-1430.txt
  python bspdump.py stat  dumps/bspdump-20251025-1430.txt
  python bspdump.py show  dumps/bspdump-20251025-1430.txt deferred
  python bspdump.py diff  dumps/good.txt dumps/bad.txt --full
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SECTION_RE = re.compile(r"^===BSPDUMP-SECTION:(?P<name>.+?)===\s*$")
END_RE = re.compile(r"^===BSPDUMP-END:(?P<name>[^=\s]+)(?P<attrs>.*?)===\s*$")
TS_RE = re.compile(r"^\[\s*\d+\.\d+\]\s?")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

CRASH_PATTERNS = {
    "PANIC": r"Kernel panic - not syncing|end Kernel panic",
    "OOPS": r"Unable to handle kernel|Internal error: Oops|Oops:",
    "BUG": r"kernel BUG at |BUG: ",
    "WARN": r"WARNING: CPU: \d+|WARNING: at ",
    "LOCKDEP": r"possible (circular|recursive) locking|possible deadlock",
    "HUNG": r"soft lockup|hard LOCKUP",
    "RCU": r"rcu_sched detected stalls|RCU Stall",
    "OOM": r"Out of memory: Killed process|oom-kill:",
}

ERR_BUCKETS = [
    ("时空/probe", r"(?i)probe|defer"),
    ("时钟/电源", r"(?i)\bclk\b|clock|regulator|voltage|supply"),
    ("总线", r"(?i)\bi2c\b|\bspi\b|smbus|timeout"),
    ("存储", r"(?i)mmc|sdhci|block|EXT4|I/O error"),
    ("网络", r"(?i)\beth\b|phy|mdio|link"),
    ("权限/缺失", r"(?i)denied|permission|not found|no such"),
]


class Section:
    __slots__ = ("name", "lines", "declared", "truncated", "complete")

    def __init__(self, name):
        self.name = name
        self.lines = []
        self.declared = None
        self.truncated = False
        self.complete = False

    @property
    def text(self):
        return "\n".join(self.lines)

    @property
    def size(self):
        return len(self.text.encode("utf-8", "replace"))


def parse(path: Path):
    """解析诊断包，返回 (sections, preamble, footer)。容忍截断。"""
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    sections, order = {}, []
    preamble, footer = [], []
    cur, cur_expected_end = None, None

    for line in raw.splitlines():
        m = SECTION_RE.match(line)
        if m:
            cur = Section(m.group("name").strip())
            cur_expected_end = cur.name
            continue
        m = END_RE.match(line)
        if m and cur is not None:
            cur.complete = True
            attrs = m.group("attrs")
            sm = re.search(r"size=(\d+)", attrs)
            cur.declared = int(sm.group(1)) if sm else None
            cur.truncated = "TRUNCATED" in attrs
            sections[cur.name] = cur
            order.append(cur.name)
            cur = None
            continue
        if line.startswith("===BSPDUMP-MANIFEST==="):
            cur = None
            continue
        if cur is not None:
            cur.lines.append(line)
        elif not sections:
            preamble.append(line)
        else:
            footer.append(line)

    if cur is not None and cur_expected_end:      # 尾巴被截断
        sections[cur.name] = cur
        order.append(cur.name)

    return [sections[n] for n in order], preamble, footer


def norm(line: str) -> str:
    line = ANSI_RE.sub("", line)
    line = TS_RE.sub("", line)
    line = re.sub(r"0x[0-9a-fA-F]{4,16}", "0xADDR", line)
    line = re.sub(r"\[<[0-9a-fA-F]{6,16}>\]", "[<ADDR>]", line)
    line = re.sub(r"\b[0-9a-fA-F]{8,16}\b", "ADDR", line)
    return re.sub(r"\s+$", "", line)


# ---------------------------------------------------------------- 信号提炼

def collect_signals(secs):
    """返回 [(level, title, detail)]，level ∈ CRIT / WARN / INFO。"""
    by = {s.name: s for s in secs}
    sig = []

    # 1) 崩溃
    for name in ("dmesg", "pstore", "lastkmsg", "dmesg-err", "dmesg-stor"):
        s = by.get(name)
        if not s:
            continue
        text = s.text
        kinds = sorted(k for k, p in CRASH_PATTERNS.items() if re.search(p, text))
        if kinds:
            hit = next((l.strip() for l in s.lines
                        if any(re.search(p, l) for p in CRASH_PATTERNS.values())), "")
            sig.append(("CRIT", f"崩溃信号（{name}）", f"{', '.join(kinds)} — {hit[:100]}"))

    # 2) deferred probe
    s = by.get("deferred")
    if s:
        devs = [l.strip() for l in s.lines
                if l.strip() and not l.strip().startswith("(")]
        if devs:
            sig.append(("WARN", "deferred probe", f"{len(devs)} 个设备未 probe 成功: "
                                                  + ", ".join(devs[:5])
                                                  + (" ..." if len(devs) > 5 else "")))
        elif not s.complete:
            sig.append(("WARN", "deferred probe", "该段未采集/被截断，无法判断"))

    # 3) rootfs 只读
    s = by.get("mounts")
    if s:
        for l in s.lines:
            if re.match(r"^\S+ on / type \S+ \((?=[^)]*\bro\b)", l):
                sig.append(("WARN", "rootfs 只读", l.strip()[:100] + " —— 很多服务起不来是因为这个"))
                break

    # 4) 温度
    s = by.get("thermal")
    if s:
        temps = [(int(m.group(1)) / 1000.0, l) for l in s.lines
                 for m in [re.search(r"temp=(-?\d+)", l)] if m]
        if temps:
            hot, line = max(temps)
            sig.append(("WARN" if hot > 85 else "INFO", "最高温度",
                        f"{hot:.1f}°C — {line.strip()[:80]}"))

    # 5) 时钟 / 电源
    s = by.get("clk")
    if s and s.complete:
        unused = sum(1 for l in s.lines if re.search(r"^\s+\S+.*\bunused\b", l))
        if unused:
            sig.append(("INFO", "未被使用的时钟", f"{unused} 个（不一定是问题，但可看出驱动没 enable）"))
    s = by.get("regulator")
    if s and s.complete:
        dummy = sum(1 for l in s.lines if re.search(r"(?i)dummy|no consumer", l))
        if dummy:
            sig.append(("INFO", "regulator 无消费者", f"{dummy} 个（dummy regulator 说明 DT 里没配电源）"))

    # 6) dmesg-err 分桶
    s = by.get("dmesg-err")
    if s:
        n = len([l for l in s.lines if l.strip()])
        if n:
            buckets = []
            for label, pat in ERR_BUCKETS:
                c = sum(1 for l in s.lines if re.search(pat, l))
                if c:
                    buckets.append(f"{label}={c}")
            sig.append(("WARN" if n > 20 else "INFO", "dmesg 错误行",
                        f"{n} 行；{'  '.join(buckets)}"))

    # 7) pstore
    s = by.get("pstore-list")
    if s and any("dmesg-" in l for l in s.lines):
        sig.append(("INFO", "pstore 有上次崩溃日志",
                    "用 show pstore 取，是最准的现场"))

    # 8) 中断
    s = by.get("interrupts")
    if s and s.complete:
        sig.append(("INFO", "中断", f"{len([l for l in s.lines if ':' in l])} 条"))
    return sig


def cmd_stat(secs, path):
    sig = collect_signals(secs)
    crit = [x for x in sig if x[0] == "CRIT"]
    warn = [x for x in sig if x[0] == "WARN"]
    info = [x for x in sig if x[0] == "INFO"]

    print(f"## 诊断包摘要  {path.name}")
    print(f"段数 {len(secs)}  合计 {sum(s.size for s in secs) / 1024:.1f} KB", end="")
    bad = [s.name for s in secs if not s.complete or s.truncated]
    print(f"  异常段: {', '.join(bad) if bad else '无'}")
    print()

    verdict = "未发现崩溃信号"
    if crit:
        verdict = f"发现崩溃信号 {len(crit)} 处，先把这部分解决再看别的"
    elif warn:
        verdict = f"{len(warn)} 项需要看，优先看第一项"
    print(f"**结论**: {verdict}\n")

    if sig:
        print("| 级别 | 项目 | 内容 |")
        print("|---|---|---|")
        for level, title, detail in crit + warn + info:
            print(f"| {level} | {title} | {detail} |")
    else:
        print("（没有提取到任何已知信号 —— 可能 profile 选错了，或者板子根本没到这个阶段）")

    print("\n### 各段清单")
    print("| 段 | 大小 | 行数 | 状态 |")
    print("|---|---|---|---|")
    for s in secs:
        state = "ok"
        if not s.complete:
            state = "**未收完**"
        elif s.truncated:
            state = "已截断"
        print(f"| {s.name} | {s.size} B | {len(s.lines)} | {state} |")

    print("\n下一步：`show <段名>` 看原文，或直接把这屏贴给模型让它决定读哪段。")


def cmd_list(secs, path):
    print(f"{path}: {len(secs)} 段, {sum(s.size for s in secs) / 1024:.1f} KB")
    for s in secs:
        flag = "" if s.complete else "  [未收完]"
        flag += "  [TRUNCATED]" if s.truncated else ""
        print(f"  {s.name:<16} {s.size:>8} B {len(s.lines):>6} 行{flag}")


def cmd_show(secs, name, grep=None, head=None):
    s = {x.name: x for x in secs}.get(name)
    if s is None:
        names = ", ".join(x.name for x in secs)
        print(f"没有段 {name!r}。可用: {names}", file=sys.stderr)
        return 2
    lines = s.lines
    if grep:
        rx = re.compile(grep, re.IGNORECASE)
        lines = [l for l in lines if rx.search(l)]
        print(f"# {name}: grep /{grep}/ -> {len(lines)} 行", file=sys.stderr)
    if head:
        lines = lines[:head]
    print(f"===BSPDUMP-SECTION:{name}===")
    print("\n".join(lines))
    print(f"===BSPDUMP-END:{name}===")
    return 0


def cmd_diff(a_secs, b_secs, a_path, b_path, full=False):
    A = {s.name: s for s in a_secs}
    B = {s.name: s for s in b_secs}
    only_a = [n for n in A if n not in B]
    only_b = [n for n in B if n not in A]

    print(f"## 比对  A={a_path.name}  B={b_path.name}")
    if only_a:
        print(f"仅 A 有: {', '.join(only_a)}")
    if only_b:
        print(f"仅 B 有: {', '.join(only_b)}")
    print()

    rows, details = [], []
    for name in [n for n in A if n in B]:
        la = [norm(l) for l in A[name].lines]
        lb = [norm(l) for l in B[name].lines]
        d = list(difflib.unified_diff(la, lb, lineterm="", n=2))
        if not d:
            continue
        add = sum(1 for x in d if x.startswith("+") and not x.startswith("+++"))
        rem = sum(1 for x in d if x.startswith("-") and not x.startswith("---"))
        rows.append((name, rem, add, len(d)))
        details.append((name, d))

    if not rows:
        print("所有共有段归一化后完全一致。")
        return 0

    rows.sort(key=lambda r: -(r[1] + r[2]))
    print("| 段 | - 行 | + 行 | 说明 |")
    print("|---|---|---|---|")
    for name, rem, add, _ in rows:
        print(f"| {name} | {rem} | {add} | {'差异最大' if rows[0][0] == name else ''} |")

    if full:
        for name, d in details:
            print(f"\n===== {name} =====")
            print("\n".join(d[:300]))
            if len(d) > 300:
                print(f"... (截断，共 {len(d)} 行)")
    else:
        print("\n加 --full 看逐行 diff。建议先看差异最大的段。")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="bspdump", description="解析 collect.sh 产出的板端诊断包",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)

    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出所有段")
    p.add_argument("file")

    p = sub.add_parser("stat", help="信号摘要")
    p.add_argument("file")

    p = sub.add_parser("show", help="看某一段")
    p.add_argument("file")
    p.add_argument("section")
    p.add_argument("--grep", help="只看匹配正则的行")
    p.add_argument("--head", type=int, help="只看前 N 行")

    p = sub.add_parser("diff", help="比对两个包")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--full", action="store_true", help="输出逐行 diff")

    args = ap.parse_args(argv)

    if args.cmd == "diff":
        a_path, b_path = Path(args.a), Path(args.b)
        for p_ in (a_path, b_path):
            if not p_.is_file():
                print(f"文件不存在: {p_}", file=sys.stderr)
                return 2
        a_secs, _, _ = parse(a_path)
        b_secs, _, _ = parse(b_path)
        return cmd_diff(a_secs, b_secs, a_path, b_path, args.full)

    path = Path(args.file)
    if not path.is_file():
        print(f"文件不存在: {path}", file=sys.stderr)
        return 2
    secs, _pre, _foot = parse(path)
    if not secs:
        print("没有解析到任何 ===BSPDUMP-SECTION:=== 段。\n"
              "  这是 collect.sh 的输出吗？还是串口日志（那种用 bspctl.py oops）？",
              file=sys.stderr)
        return 1

    if args.cmd == "list":
        cmd_list(secs, path)
        return 0
    if args.cmd == "stat":
        cmd_stat(secs, path)
        return 0
    if args.cmd == "show":
        return cmd_show(secs, args.section, args.grep, args.head)
    return 2


if __name__ == "__main__":
    sys.exit(main())
