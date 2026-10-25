#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bspcheck.py — 上板前的离线体检（纯标准库，不依赖 dt-schema / QEMU / 交叉工具链）

为什么是这三个子命令
--------------------
约束：厂商 SDK 没有 binding YAML，也没有 QEMU。所以能做的离线校验只有三类：

  1. dtb     直接解析编译产物 .dtb 的 FDT 结构。不看 binding，只看"结构自洽性"：
             reg 长度和父节点 cells 对不上、phandle 悬空、names 和 list 数量不匹配、
             reserved-memory 重叠/越界、祖先 disabled 子节点却 okay…… 这些都是
             "编译能过、上板才炸" 的典型，而且不需要任何 schema 就能判定。
  2. sym     用 System.map + vmlinux 把 oops 里的裸地址翻译成符号；
             有 addr2line 就再给到 file:line。KASLR 偏移自动处理。
  3. config  检查 .config 里"能不能查案"相关的选项（DEBUG_INFO / PSTORE /
             KALLSYMS / FRAME_POINTER…）。缺这些，出了问题你连现场都留不下。

用法
----
  python bspcheck.py dtb    out/board.dtb
  python bspcheck.py dtb    out/board.dtb --ignore "/soc@0/spi" --max 80
  python bspcheck.py sym    ffffffc00812a3c4 ffffffc00812a3d0 --map System.map --vmlinux vmlinux
  python bspcheck.py sym    --log logs/boot-bad.log --map System.map --vmlinux vmlinux
  python bspcheck.py config out/.config

退出码: 0 无 ERROR / 1 有 ERROR / 2 用法或解析失败
"""

from __future__ import annotations

import argparse
import bisect
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8-sig", errors="replace")
    except Exception:
        pass


# ============================================================ FDT 解析

FDT_MAGIC = 0xD00DFEED
FDT_BEGIN_NODE, FDT_END_NODE, FDT_PROP, FDT_NOP, FDT_END = 1, 2, 3, 4, 9


class Node:
    __slots__ = ("name", "props", "children", "parent", "path")

    def __init__(self, name, parent):
        self.name = name
        self.props = {}
        self.children = []
        self.parent = parent
        if parent is None:
            self.path = "/"
        elif parent.path == "/":
            self.path = "/" + name
        else:
            self.path = parent.path + "/" + name

    # ---- 属性读取
    def raw(self, key):
        return self.props.get(key)

    def u32(self, key, default=None):
        v = self.props.get(key)
        if v is None or len(v) < 4:
            return default
        return struct.unpack(">I", v[:4])[0]

    def u32s(self, key):
        v = self.props.get(key)
        if v is None or len(v) % 4:
            return None
        return list(struct.unpack(">%dI" % (len(v) // 4), v))

    def string(self, key, default=None):
        v = self.props.get(key)
        if v is None:
            return default
        return v.split(b"\0")[0].decode("utf-8", "replace")

    def strings(self, key):
        v = self.props.get(key)
        if v is None:
            return None
        return [s.decode("utf-8", "replace") for s in v.split(b"\0") if s]

    def status(self):
        return self.string("status", "okay")

    def enabled(self):
        return self.status() in ("okay", "ok")


class Tree:
    def __init__(self, root):
        self.root = root
        self.by_phandle = {}
        self.by_path = {}
        self.nodes = []
        self._index(root)

    def _index(self, node):
        self.nodes.append(node)
        self.by_path[node.path] = node
        for k in ("phandle", "linux,phandle"):
            v = node.u32(k)
            if v is not None:
                self.by_phandle[v] = node
        for c in node.children:
            self._index(c)

    def walk(self):
        yield from self.nodes


def parse_fdt(blob: bytes) -> Tree:
    if len(blob) < 40:
        raise ValueError("文件太小，不是 FDT")
    (magic, totalsize, off_struct, off_strings, _off_rsv,
     _ver, _lastver, _cpu, _size_strings, _size_struct) = struct.unpack(">10I", blob[:40])
    if magic != FDT_MAGIC:
        raise ValueError(f"FDT magic 不对: 0x{magic:08x}（期望 0xd00dfeed）")
    if totalsize > len(blob):
        raise ValueError(f"FDT 头声明 {totalsize} 字节，实际只有 {len(blob)} 字节（被截断？）")

    pos = off_struct
    root = None
    stack = []
    while pos + 4 <= totalsize:
        (tok,) = struct.unpack(">I", blob[pos:pos + 4])
        pos += 4
        if tok == FDT_BEGIN_NODE:
            end = blob.index(b"\0", pos)
            name = blob[pos:end].decode("utf-8", "replace")
            pos = (end + 1 + 3) & ~3
            node = Node(name, stack[-1] if stack else None)
            if stack:
                stack[-1].children.append(node)
            elif root is None:
                root = node
            stack.append(node)
        elif tok == FDT_END_NODE:
            if not stack:
                raise ValueError("结构块不平衡：多余的 END_NODE")
            stack.pop()
        elif tok == FDT_PROP:
            if pos + 8 > totalsize:
                raise ValueError("结构块被截断（PROP 头）")
            plen, nameoff = struct.unpack(">II", blob[pos:pos + 8])
            pos += 8
            if pos + plen > totalsize:
                raise ValueError("结构块被截断（PROP 数据）")
            s = blob.index(b"\0", off_strings + nameoff)
            pname = blob[off_strings + nameoff:s].decode("utf-8", "replace")
            if stack:
                stack[-1].props[pname] = blob[pos:pos + plen]
            pos = (pos + plen + 3) & ~3
        elif tok == FDT_NOP:
            continue
        elif tok == FDT_END:
            break
        else:
            raise ValueError(f"未知 token 0x{tok:x} @ {pos - 4}")

    if root is None:
        raise ValueError("没有根节点")
    return Tree(root)


# ============================================================ 体检

class Report:
    def __init__(self):
        self.items = []          # (level, path, title, detail)
        self.skipped = 0

    def add(self, level, path, title, detail=""):
        # 同一节点同一问题只报一次（多个子节点会重复触发同一检查）
        key = (level, path, title)
        seen = getattr(self, "_seen", None)
        if seen is None:
            seen = self._seen = set()
        if key in seen:
            return
        seen.add(key)
        self.items.append((level, path, title, detail))

    def err(self, path, title, detail=""):
        self.add("ERROR", path, title, detail)

    def warn(self, path, title, detail=""):
        self.add("WARN", path, title, detail)

    def info(self, path, title, detail=""):
        self.add("INFO", path, title, detail)

    def count(self, level):
        return sum(1 for i in self.items if i[0] == level)


def cells_for_children(node, kind, rep: Report):
    """子节点的 reg / interrupts 用 node 的 #xxx-cells 解释；缺失则向上继承。"""
    key = f"#{kind}-cells"
    cur = node
    while cur is not None:
        v = cur.u32(key)
        if v is not None:
            return v
        cur = cur.parent
    return None


ADDR_CELLS_DEFAULT = {"#address-cells": 2, "#size-cells": 1}


def check_reg(tree, rep):
    for node in tree.walk():
        if node.parent is None:
            continue
        reg = node.raw("reg")
        if reg is None:
            continue
        ac = cells_for_children(node.parent, "address", rep)
        sc = cells_for_children(node.parent, "size", rep)
        if ac is None or sc is None:
            rep.skipped += 1
            continue
        unit = (ac + sc) * 4
        if unit <= 0:
            continue
        if len(reg) == 0:
            rep.warn(node.path, "reg 是空属性", "有些驱动会因此 fail probe，或落到错误的分区")
            continue
        if len(reg) % unit:
            rep.err(node.path, "reg 长度和父节点的 cells 对不上",
                    f"父节点 {node.parent.path} 是 #address-cells={ac} #size-cells={sc}，"
                    f"每条 entry 应为 {unit} 字节，实际 {len(reg)} 字节 —— "
                    f"内核会按错误边界切分，地址/长度全错")


def check_interrupts(tree, rep):
    for node in tree.walk():
        intr = node.raw("interrupts")
        if intr is None:
            continue
        # interrupt-parent 可能定义在本节点或任一祖先
        cur, parent_node = node, None
        while cur is not None:
            ph = cur.u32("interrupt-parent")
            if ph is not None:
                parent_node = tree.by_phandle.get(ph)
                break
            cur = cur.parent
        if parent_node is None:
            continue                      # 靠 irq domain 继承，静态查不了
        n = cells_for_children(parent_node, "interrupt", rep)
        if not n:
            continue
        unit = n * 4
        if len(intr) % unit:
            rep.err(node.path, "interrupts 长度和 interrupt-parent 的 #interrupt-cells 对不上",
                    f"{parent_node.path} 的 #interrupt-cells={n}，每条 {unit} 字节，"
                    f"实际 {len(intr)} 字节 —— 中断号会被错位解析")
        names = node.strings("interrupt-names")
        if names:
            cnt = len(intr) // unit
            if len(names) != cnt:
                rep.err(node.path, "interrupt-names 数量与 interrupts 条数不一致",
                        f"names={len(names)} 条，interrupts={cnt} 条")


def check_duplicate_siblings(tree, rep):
    for node in tree.walk():
        seen = {}
        for c in node.children:
            if not c.name:
                continue
            if c.name in seen:
                rep.err(c.path, "兄弟节点重名（unit-address 冲突）",
                        f"和 {seen[c.name].path} 同名；dtc 通常不报错，"
                        f"但内核只会认其中一个，另一个静默消失")
            else:
                seen[c.name] = c


def _is_i2c(node):
    if node.name.split("@")[0].startswith(("i2c", "iic")):
        return True
    for c in (node.strings("compatible") or []):
        if "i2c" in c or "iic" in c:
            return True
    return False


def check_i2c_addr(tree, rep):
    for bus in tree.walk():
        if not _is_i2c(bus):
            continue
        for child in bus.children:
            words = child.u32s("reg")
            if not words:
                continue
            addr = words[0]
            if addr > 0x7F:
                rep.err(child.path, "I2C 从地址超出 7 位范围",
                        f"reg[0]=0x{addr:x}；4 字节 reg 忘了写 address-cells=1 时最常见，"
                        f"或误用了 10 位地址")
            elif 0x00 <= addr <= 0x07 or 0x78 <= addr <= 0x7F:
                rep.warn(child.path, "I2C 保留地址",
                         f"reg[0]=0x{addr:02x} 属于保留段（0x00-0x07 / 0x78-0x7F）")


SINGLE_PHANDLE_PROPS = (
    "interrupt-parent", "phy-handle", "phy", "ddc-i2c-bus", "backlight",
    "remote-endpoint", "syscon", "nvmem-cells", "reset-gpios",
)


def check_dangling_phandle(tree, rep):
    for node in tree.walk():
        for key, val in node.props.items():
            single = key in SINGLE_PHANDLE_PROPS or key.endswith("-supply")
            if not single:
                continue
            if len(val) != 4:
                if key.endswith("-supply") and len(val) % 4 == 0:
                    rep.warn(node.path, f"{key} 引用了多个 phandle",
                             f"{len(val) // 4} 个；supply 一般只该有一个")
                continue
            ph = struct.unpack(">I", val)[0]
            if ph and ph not in tree.by_phandle:
                rep.err(node.path, f"{key} 指向不存在的 phandle",
                        f"phandle=0x{ph:x} 在整棵树里找不到 —— 驱动会拿不到资源，"
                        f"通常表现为 probe 无限延迟（-EPROBE_DEFER）")


NAMES_PAIRS = [
    ("clocks", "clock-names", "#clock-cells"),
    ("resets", "reset-names", "#reset-cells"),
    ("dmas", "dma-names", "#dma-cells"),
    ("power-domains", "power-domain-names", "#power-domain-cells"),
    ("io-channels", "io-channel-names", "#io-channel-cells"),
    ("mboxes", "mbox-names", "#mbox-cells"),
    ("pwms", "pwm-names", "#pwm-cells"),
]


def count_phandle_entries(tree, words, cells_key):
    """按 provider 的 #xxx-cells 逐条切分，返回条数；解析不了返回 None。"""
    i = n = 0
    while i < len(words):
        node = tree.by_phandle.get(words[i])
        if node is None:
            return None
        cells = node.u32(cells_key)
        if cells is None:
            return None
        i += 1 + cells
        n += 1
    return n if i == len(words) else None


def check_names_pairs(tree, rep):
    for node in tree.walk():
        for list_prop, names_prop, cells_key in NAMES_PAIRS:
            words = node.u32s(list_prop)
            if not words:
                continue
            names = node.strings(names_prop)
            if not names:
                continue
            cnt = count_phandle_entries(tree, words, cells_key)
            if cnt is None:
                rep.skipped += 1
                continue
            if cnt != len(names):
                rep.err(node.path, f"{list_prop} 条数与 {names_prop} 不一致",
                        f"{list_prop}={cnt} 条，{names_prop}={len(names)} 条"
                        f"（按 provider 的 {cells_key} 精确解析）—— "
                        f"驱动里 clk_get(dev, names[i]) 会拿到错的时钟")


def _regions(node):
    """解析 /reserved-memory 下每个子节点的 (start, size)。"""
    ac = cells_for_children(node, "address", Report())
    sc = cells_for_children(node, "size", Report())
    if ac is None or sc is None:
        return None, None
    out = []
    for c in node.children:
        words = c.u32s("reg")
        if not words:
            continue
        unit = ac + sc
        if unit <= 0 or len(words) % unit:
            continue
        for i in range(0, len(words), unit):
            start = 0
            for w in words[i:i + ac]:
                start = (start << 32) | w
            size = 0
            for w in words[i + ac:i + unit]:
                size = (size << 32) | w
            out.append((start, size, c))
    return out, unit


def check_reserved_memory(tree, rep):
    rm = tree.by_path.get("/reserved-memory")
    if rm is None:
        return
    regions, _ = _regions(rm)
    if regions is None:
        rep.warn("/reserved-memory", "无法解析 #address-cells/#size-cells", "跳过重叠检查")
        return

    for start, size, c in regions:
        if size == 0:
            rep.warn(c.path, "reserved 区域大小为 0", "多半是 reg 写错，或被内核直接忽略")

    regions_s = sorted(regions, key=lambda r: r[0])
    for (s1, z1, c1), (s2, z2, c2) in zip(regions_s, regions_s[1:]):
        if z1 and s1 + z1 > s2:
            rep.err(c2.path, "reserved-memory 区域重叠",
                    f"{c1.path} [0x{s1:x}, +0x{z1:x}) 与 "
                    f"{c2.path} [0x{s2:x}, +0x{z2:x}) 相交 —— "
                    f"后者会被静默裁剪或直接 panic")

    mem = [n for n in tree.root.children if n.name.split("@")[0] == "memory"]
    if not mem:
        return
    ac = cells_for_children(tree.root, "address", rep)
    sc = cells_for_children(tree.root, "size", rep)
    if ac is None or sc is None:
        return
    spans = []
    for m in mem:
        words = m.u32s("reg")
        if not words:
            continue
        unit = ac + sc
        for i in range(0, len(words), unit):
            start = 0
            for w in words[i:i + ac]:
                start = (start << 32) | w
            size = 0
            for w in words[i + ac:i + unit]:
                size = (size << 32) | w
            spans.append((start, size))
    for start, size, c in regions:
        if start == 0 and size == 0:
            continue
        if not any(ms <= start and start + size <= ms + mz for ms, mz in spans):
            rep.err(c.path, "reserved 区域落在 /memory 之外",
                    f"[0x{start:x}, +0x{size:x})；内存映射 ["
                    + ", ".join(f"0x{a:x}+0x{b:x}" for a, b in spans) + "]"
                    f" —— 这一段不会被保留，会直接被分配器拿去用")


def check_enabled_under_disabled(tree, rep):
    for node in tree.walk():
        if node.status() not in ("okay", "ok"):
            continue
        cur = node.parent
        while cur is not None:
            if cur.status() == "disabled":
                rep.err(node.path, "祖先 disabled，子节点却是 okay",
                        f"祖先 {cur.path} 被 disable 了，这个节点永远不会 probe —— "
                        f"现象是「设备不存在」而不是报错，极难查")
                break
            cur = cur.parent


def check_status_values(tree, rep):
    legal = {"okay", "disabled", "reserved", "fail", "fail-sss"}
    for node in tree.walk():
        st = node.string("status")
        if st is not None and st not in legal:
            rep.warn(node.path, f"status 值不合法: {st!r}", f"合法值: {', '.join(sorted(legal))}")


def check_aliases(tree, rep):
    al = tree.by_path.get("/aliases")
    if al is None:
        return
    for key, val in al.props.items():
        if key.startswith(("phandle", "linux,")):
            continue
        target = val.split(b"\0")[0].decode("utf-8", "replace")
        if target.startswith("/") and target not in tree.by_path:
            rep.warn(f"/aliases:{key}", "aliases 指向不存在的路径", target)


def check_cells_sanity(tree, rep):
    # 注意: #size-cells = 0 是合法的（i2c / spi 这类总线就是 1+0），不能当异常报。
    for node in tree.walk():
        ac, sc = node.u32("#address-cells"), node.u32("#size-cells")
        for key, v in (("#address-cells", ac), ("#size-cells", sc),
                       ("#interrupt-cells", node.u32("#interrupt-cells"))):
            if v is not None and v > 4:
                rep.warn(node.path, f"{key} 取值过大", f"= {v}，正常 0~4")
        if node.children and ((ac is None) != (sc is None)):
            miss = "#size-cells" if sc is None else "#address-cells"
            rep.warn(node.path, f"有子节点但缺 {miss}",
                     "这两个通常成对出现，缺一个会让子节点的 reg 按错误边界切分")


def cmd_dtb(args):
    path = Path(args.dtb)
    if not path.is_file():
        print(f"文件不存在: {path}", file=sys.stderr)
        return 2
    blob = path.read_bytes()

    # 误把 .dts 源码传进来时给个明确提示
    if not blob[:4] == b"\xd0\x0d\xfe\xed":
        head = blob[:200].decode("utf-8", "replace")
        if "dts-v1/" in head or head.lstrip().startswith("/"):
            print("这是 .dts 源码，不是编译后的 .dtb。先编译：\n"
                  "  dtc -I dts -O dtb -o board.dtb board.dts\n"
                  "  # 或在 SDK 里: make dtbs", file=sys.stderr)
            return 2

    try:
        tree = parse_fdt(blob)
    except (ValueError, struct.error) as exc:
        print(f"解析 .dtb 失败: {exc}", file=sys.stderr)
        return 2

    ignore = re.compile(args.ignore) if args.ignore else None
    rep = Report()

    check_reg(tree, rep)
    check_interrupts(tree, rep)
    check_duplicate_siblings(tree, rep)
    check_i2c_addr(tree, rep)
    check_dangling_phandle(tree, rep)
    check_names_pairs(tree, rep)
    check_reserved_memory(tree, rep)
    check_enabled_under_disabled(tree, rep)
    check_status_values(tree, rep)
    check_aliases(tree, rep)
    check_cells_sanity(tree, rep)

    if ignore:
        rep.items = [i for i in rep.items if not ignore.search(i[1])]

    order = {"ERROR": 0, "WARN": 1, "INFO": 2}
    rep.items.sort(key=lambda i: (order[i[0]], i[1]))

    n_err, n_warn = rep.count("ERROR"), rep.count("WARN")
    print(f"## DTB 体检  {path.name}")
    print(f"节点 {len(tree.nodes)} 个，phandle {len(tree.by_phandle)} 个，"
          f"{len(blob) / 1024:.1f} KB")
    verdict = ("**结构自洽，没查到会必然翻车的问题**" if n_err == 0
               else f"**{n_err} 个错误必须先修**，这些上板必炸且现象隐蔽")
    print(f"结论: {verdict}"
          + (f"，另有 {n_warn} 个警告" if n_warn else ""))
    print()

    if rep.items:
        print("| 级别 | 位置 | 问题 | 说明 |")
        print("|---|---|---|---|")
        shown = rep.items[: args.max]
        for level, npath, title, detail in shown:
            print(f"| {level} | `{npath}` | {title} | {detail} |")
        if len(rep.items) > len(shown):
            print(f"\n... 另有 {len(rep.items) - len(shown)} 条（用 --max 调大）")
    else:
        print("（没有任何发现）")

    if rep.skipped:
        print(f"\n注: {rep.skipped} 处因子节点缺 #address-cells / #size-cells "
              f"或 provider 缺 #xxx-cells 而无法校验，已跳过（这本身也值得确认）。")

    print("\n这些检查不依赖 binding YAML，也不替代 `dtbs_check`；"
          "它抓的是结构性错误——编译能过、上板才炸的那一类。")
    return 1 if n_err else 0


# ============================================================ 符号化

SYM_RE = re.compile(r"^([0-9a-fA-F]+)\s+(\w)\s+(\S+)")


def load_system_map(path: Path):
    syms = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        m = SYM_RE.match(line)
        if m:
            syms.append((int(m.group(1), 16), m.group(2), m.group(3)))
    syms.sort()
    return syms


def find_addr2line():
    cands = [os.environ.get("ADDR2LINE", "")]
    for pre in ("aarch64-linux-gnu-", "arm-linux-gnueabihf-", "aarch64-none-linux-gnu-", ""):
        cands.append(pre + "addr2line")
    for c in cands:
        if c and shutil.which(c):
            return shutil.which(c)
    return None


def addr2line_batch(tool, vmlinux, addrs):
    if not tool or not vmlinux or not addrs:
        return {}
    cmd = [tool, "-f", "-e", str(vmlinux)] + [f"0x{a:x}" for a in addrs]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                             timeout=60).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return {}
    res = {}
    for i, a in enumerate(addrs):
        func = out[2 * i].strip() if 2 * i < len(out) else "?"
        loc = out[2 * i + 1].strip() if 2 * i + 1 < len(out) else "?"
        loc = loc.replace(" (discriminator", "").strip()
        if loc in ("??:0", "??:?", ""):
            loc = "-"
        res[a] = (func, loc)
    return res


KASLR_RE = re.compile(r"Kernel Offset:\s*(0x[0-9a-fA-F]+|disabled)")


def parse_kernel_offset(text):
    m = KASLR_RE.search(text)
    if not m:
        return 0, None
    if m.group(1) == "disabled":
        return 0, "disabled"
    return int(m.group(1), 16), m.group(1)


ADDR_RES = [
    re.compile(r"\[<([0-9a-fA-F]{8,16})>\]"),
    re.compile(r"\b(?:pc|lr|sp|PC|LR|SP)\s*[:=]\s*([0-9a-fA-F]{8,16})"),
    re.compile(r"\b(?:at|address)\s+([0-9a-fA-F]{8,16})"),
]


def cmd_sym(args):
    map_path = Path(args.map)
    if not map_path.is_file():
        print(f"System.map 不存在: {map_path}", file=sys.stderr)
        return 2
    syms = load_system_map(map_path)
    if not syms:
        print(f"{map_path} 里没解析出符号", file=sys.stderr)
        return 2
    addrs_sorted = [s[0] for s in syms]

    log_text, contexts = "", []
    if args.log:
        log_path = Path(args.log)
        if not log_path.is_file():
            print(f"日志不存在: {log_path}", file=sys.stderr)
            return 2
        log_text = log_path.read_text(encoding="utf-8-sig", errors="replace")
        for lineno, line in enumerate(log_text.splitlines(), 1):
            for rx in ADDR_RES:
                for m in rx.finditer(line):
                    contexts.append((int(m.group(1), 16), lineno, line.strip()))

    offset, off_raw = parse_kernel_offset(log_text)
    if off_raw:
        if offset:
            print(f"检测到 KASLR: Kernel Offset = {off_raw}（查表前会先减掉这个偏移）")
        else:
            print("KASLR: Kernel Offset = disabled（地址即链接地址，直接查表）")

    targets = []
    if args.addrs:
        for a in args.addrs:
            targets.append((int(a, 16), None, ""))
    targets += contexts

    seen, rows = set(), []
    for runtime, lineno, line in targets:
        link = runtime - offset if offset else runtime
        if runtime in seen:
            continue
        seen.add(runtime)
        i = bisect.bisect_right(addrs_sorted, link) - 1
        if i < 0:
            rows.append((runtime, link, None, None, lineno, line, "低于 System.map 最低地址"))
            continue
        base, typ, name = syms[i]
        delta = link - base
        rows.append((runtime, link, name, delta, lineno, line, None))

    known = [r for r in rows if r[2]]
    a2l = addr2line_batch(find_addr2line(), args.vmlinux, [r[1] for r in known])

    print(f"{'## 符号化  ' + map_path.name}")
    if args.vmlinux:
        print(f"vmlinux: {args.vmlinux}（addr2line: {'可用' if a2l else '不可用，只给符号+偏移'}）")
    print()
    print("| 运行地址 | 链接地址 | 符号 | 偏移 | 源码 | 日志行 |")
    print("|---|---|---|---|---|---|")
    total = 0
    for runtime, link, name, delta, lineno, line, note in rows:
        if name is None:
            print(f"| 0x{runtime:x} | 0x{link:x} | - | - | - | "
                  f"{lineno or ''} | {note or ''} |")
            continue
        func, loc = a2l.get(link, ("", "-"))
        src = loc if loc != "-" else (f"{func}" if func and func != "?" else "-")
        print(f"| 0x{runtime:x} | 0x{link:x} | `{name}` | +0x{delta:x} | {src} | "
              f"{lineno or ''} |")
        total += 1

    if not rows:
        print("没有找到地址。`--log` 模式只认 `[<ffff...>]`、`pc : xxx`、`address xxx` 这几种形式。")
    noted = [n for n in {r[6] for r in rows if r[6]}]
    for n in noted:
        print(f"\n注: {n} —— 该地址可能来自模块（.ko），要用对应 .ko 的符号表查。")
    if total and not a2l:
        print("\n提示: 把交叉工具链加进 PATH（如 aarch64-linux-gnu-addr2line），"
              "或用 ADDR2LINE=/path/to/addr2line 指定，就能拿到 file:line。")
    return 0


# ============================================================ .config 体检

CONFIG_CHECKS = [
    ("CONFIG_KALLSYMS", "y", "oops 里得有符号名",
     "关掉之后调用栈只有裸地址，分诊基本没法做"),
    ("CONFIG_KALLSYMS_ALL", "y", "全局变量也要有符号",
     "只有函数符号时，看不了 data 段里那个 NULL 是哪个变量"),
    ("CONFIG_DEBUG_INFO", "y", "拿得到 file:line",
     "没有它 addr2line 只能给函数名，定位要多花好几轮"),
    ("CONFIG_DEBUG_INFO_REDUCED", "n", "别砍调试信息",
     "打开后会丢掉行号和类型信息，等于白开 DEBUG_INFO"),
    ("CONFIG_IKCONFIG_PROC", "y", "板子上能读 /proc/config.gz",
     "否则你永远说不清「现场这个镜像到底开了什么」"),
    ("CONFIG_PSTORE", "y", "崩溃日志能落到 RAM 并跨重启存活",
     "panic 后串口可能来不及打全，pstore 是唯一可靠的现场"),
    ("CONFIG_PSTORE_CONSOLE", "y", "把 console 输出也存进 pstore",
     "配合 ramoops 用，用来救「串口只看到一半」的情况"),
    ("CONFIG_PSTORE_RAM", "y", "ramoops 后端",
     "需要在 DT 里配 ramoops 节点，否则有配置也没用"),
    ("CONFIG_MAGIC_SYSRQ", "y", "至少能触发 sysrq",
     "卡死时 sysrq-t/w 能看到所有任务栈，比猜强"),
    ("CONFIG_DEBUG_KERNEL", "y", "打开调试相关菜单",
     "很多 DEBUG_* 选项依赖它，关着的话它们根本不会出现"),
    ("CONFIG_DYNAMIC_DEBUG", "y", "能按文件/函数动态开调试打印",
     "不用重新编译就能加日志，直接减少往返次数"),
    ("CONFIG_FRAME_POINTER", "y", "栈回溯更完整（32 位尤其重要）",
     "arm32 上没它，调用栈经常断在第二层"),
]

CONFIG_NOTE = {
    "CONFIG_RANDOMIZE_BASE": ("y", "KASLR 开着", "开着也没问题，bspcheck sym 会读 Kernel Offset 自动补偿"),
    "CONFIG_DEBUG_INFO_BTF": ("y", "有 BTF", "bpftrace 之类能用，但不是必需"),
}


def cmd_config(args):
    path = Path(args.config)
    if not path.is_file():
        print(f"文件不存在: {path}", file=sys.stderr)
        return 2
    cfg = {}
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("# CONFIG_") and line.endswith(" is not set"):
            cfg[line[2:-11].strip()] = "n"
        elif line.startswith("CONFIG") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"')

    print(f"## .config 体检  {path.name}")
    print(f"条目 {len(cfg)} 个\n")
    print("| 选项 | 期望 | 现状 | 用途 | 缺了会怎样 |")
    print("|---|---|---|---|---|")
    bad = 0
    for key, want, purpose, why in CONFIG_CHECKS:
        cur = cfg.get(key, "n")
        ok = (cur == want)
        if not ok:
            bad += 1
        mark = "✅" if ok else "❌"
        print(f"| {mark} `{key}` | {want} | {cur} | {purpose} | {'' if ok else why} |")

    notes = []
    for key, (want, when, note) in CONFIG_NOTE.items():
        if cfg.get(key) == want:
            notes.append(f"- `{key}={want}`：{when} —— {note}")
    if notes:
        print("\n" + "\n".join(notes))

    print(f"\n**{bad} 项建议调整。**")
    if bad:
        print("这些不影响功能，只影响「出问题时能不能查」。按往返成本算，"
              "提前打开比事后猜便宜得多。")
    return 1 if bad else 0


# ============================================================ 入口

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="bspcheck", description="上板前的离线体检（DTB / 符号化 / .config）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("dtb", help="检查编译后的 .dtb 结构自洽性")
    p.add_argument("dtb")
    p.add_argument("--ignore", help="忽略匹配该正则的节点路径")
    p.add_argument("--max", type=int, default=60, help="最多显示多少条")

    p = sub.add_parser("sym", help="把 oops 里的地址翻译成符号")
    p.add_argument("addrs", nargs="*", help="裸地址，如 ffffffc00812a3c4")
    p.add_argument("--map", required=True, help="System.map 路径")
    p.add_argument("--vmlinux", help="vmlinux 路径（有 addr2line 时给 file:line）")
    p.add_argument("--log", help="直接解析日志里的地址")

    p = sub.add_parser("config", help="检查 .config 的可诊断性")
    p.add_argument("config")

    args = ap.parse_args(argv)
    if args.cmd == "dtb":
        return cmd_dtb(args)
    if args.cmd == "sym":
        return cmd_sym(args)
    if args.cmd == "config":
        return cmd_config(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
