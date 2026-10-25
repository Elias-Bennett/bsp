#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bspctl.py — Linux BSP 工作台 (U-Boot / Kernel / Buildroot)

设计原则
--------
1. 所有"碰硬件"的动作都收敛到这里；AI 只负责调用与判断，不臆造寄存器值。
2. 每次动作都落盘（日志 + 元数据），既能复盘，也能直接喂给模型。
3. 危险动作（烧写 SD/eMMC）默认拒绝，必须 --yes 且通过根设备/挂载检查。

子命令
------
  log        抓串口日志（支持复位后抓、超时、until 匹配、USB 掉线重连）
  cmd        通过串口执行命令（agent 闭环主入口，带退出码）
  ssh        在板子上执行命令
  build      触发构建，流式落盘，自动提取错误
  flash      烧写镜像（sdcard / 自定义命令）
  reset      复位或上下电（uhubctl / USB 继电器 / 自定义命令）
  up         reset -> log -> 启动耗时，一条命令跑完一轮
  smoke      板级冒烟测试
  boottime   从日志提取各启动阶段耗时
  oops       从日志提取崩溃现场（输出可直接粘贴给模型）
  diff       两次启动 / 两块板日志归一化比对

示例
----
  python bspctl.py log --until "login:" --timeout 90
  python bspctl.py cmd "cat /proc/device-tree/model" "dmesg | tail -20"
  python bspctl.py up --until "login:"
  python bspctl.py oops logs/boot-20251025-142233.log
  python bspctl.py diff logs/good.log logs/bad.log
"""

from __future__ import annotations

import argparse
import codecs
import configparser
import datetime as _dt
import difflib
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

# Windows 控制台默认 cp936，统一成 utf-8，避免中文 help / 日志乱码
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent

DEFAULTS = {
    "serial": {"port": "", "baud": "115200", "eol": "crlf", "logdir": "logs",
               "dtr": "", "rts": ""},
    "board":  {"name": "board", "ip": "", "user": "root", "ssh_port": "22",
               "ssh_extra": "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8"},
    "build":  {"cmd": "", "logdir": "logs/build"},
    "flash":  {"mode": "none", "image": "", "device": "", "cmd": ""},
    "reset":  {"mode": "none", "cmd": "", "hub": "", "port": "", "delay": "6",
               "relay_port": "", "relay_on": "", "relay_off": ""},
    "ssh":    {"extra": ""},
}

PRINT = {"quiet": False}


# ---------------------------------------------------------------- 基础设施

def die(msg: str, code: int = 2):
    print(f"\n[bspctl] 错误: {msg}", file=sys.stderr)
    sys.exit(code)


def info(msg: str):
    print(f"[bspctl] {msg}", file=sys.stderr)


def run(cmd, check=False, capture=False, timeout=None, cwd=None):
    """统一的外部命令执行入口。"""
    info(f"$ {cmd}")
    try:
        return subprocess.run(
            cmd, shell=isinstance(cmd, str), check=check, cwd=cwd, timeout=timeout,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError as exc:
        die(f"命令不存在: {exc}")
    except subprocess.TimeoutExpired:
        die(f"命令超时: {cmd}", 124)


class Conf:
    """INI 配置，带默认值合并。"""

    def __init__(self, data: dict, path: Path | None):
        self.data = data
        self.path = path

    def get(self, section: str, key: str, fallback: str = "") -> str:
        return self.data.get(section, {}).get(key, DEFAULTS.get(section, {}).get(key, fallback)).strip()

    def get_int(self, section: str, key: str, fallback: int = 0) -> int:
        raw = self.get(section, key)
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return fallback

    def logdir(self, section: str) -> Path:
        p = Path(self.get(section, "logdir") or "logs")
        if not p.is_absolute():
            base = self.path.parent if self.path else Path.cwd()
            p = base / p
        p.mkdir(parents=True, exist_ok=True)
        return p


def load_conf(explicit: str | None) -> Conf:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("BSPCTL_CONF"):
        candidates.append(Path(os.environ["BSPCTL_CONF"]))
    candidates += [Path.cwd() / "bsp.conf", ROOT / "bsp.conf",
                   Path.home() / ".config" / "bspctl" / "bsp.conf"]

    path = next((p for p in candidates if p.is_file()), None)
    data = {sec: dict(vals) for sec, vals in DEFAULTS.items()}
    if path:
        cp = configparser.ConfigParser(interpolation=None)
        try:
            # utf-8-sig: 兼容 Windows 记事本 / VSCode 写出的 BOM
            cp.read(path, encoding="utf-8-sig")
        except (configparser.Error, UnicodeDecodeError) as exc:
            die(f"配置文件解析失败: {path}\n  {exc}\n"
                f"  提示: INI 格式，第一行必须是 [serial] 这样的节名")
        for sec in cp.sections():
            data.setdefault(sec, {}).update({k: v for k, v in cp.items(sec)})
    elif explicit:
        die(f"配置文件不存在: {explicit}")
    return Conf(data, path)


def _read_text(path: Path) -> str:
    """读文本时容忍 BOM（Windows 记事本产物）。"""
    return path.read_text(encoding="utf-8-sig", errors="replace")


def _import_serial():
    try:
        import serial  # noqa: F401
        import serial.tools.list_ports  # noqa: F401
        return serial
    except ImportError:
        die("缺少 pyserial。安装: python -m pip install -r requirements.txt")


def _port(conf: Conf, needed: bool = True) -> str:
    port = conf.get("serial", "port")
    if port:
        return port
    serial = _import_serial()
    ports = [p.device for p in serial.tools.list_ports.comports()]
    if len(ports) == 1:
        info(f"未配置端口，自动使用唯一可用串口: {ports[0]}")
        return ports[0]
    detail = "\n  ".join(ports) if ports else "(无)"
    die(f"未配置 [serial] port。当前可用串口:\n  {detail}")


def _eol(conf: Conf) -> str:
    return {"crlf": "\r\n", "cr": "\r", "lf": "\n", "none": ""}.get(
        conf.get("serial", "eol").lower(), "\r\n")


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
CRLF_RE = re.compile(r"\r+\n")


def _norm_newlines(text: str) -> str:
    """统一换行。很多串口终端会发 \\r\\r\\n，直接替换会多出空白行。"""
    return CRLF_RE.sub("\n", text).replace("\r", "\n")


def _clean(text: str) -> str:
    return ANSI_RE.sub("", _norm_newlines(text))


def _stamp_name(conf: Conf, prefix: str) -> Path:
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = re.sub(r"[^\w.-]+", "_", conf.get("board", "name") or "board")
    return conf.logdir("serial") / f"{prefix}-{name}-{ts}.log"


# ---------------------------------------------------------------- 串口

def _make_port(serial, port: str, baud: int):
    if "://" in port:
        return serial.serial_for_url(port, baud, timeout=0.05)
    return serial.Serial(port, baud, timeout=0.05, write_timeout=2)


def _open_serial(conf: Conf):
    serial = _import_serial()
    port = _port(conf)
    baud = conf.get_int("serial", "baud", 115200)
    try:
        ser = _make_port(serial, port, baud)
    except Exception as exc:
        die(f"打开串口失败 {port}@{baud}: {exc}\n"
            f"  排查: 端口被别的程序占用？(串口助手 / minicom / screen 先关掉)\n"
            f"        驱动装了吗？(CH340 / CP210x / FTDI)")
    for sig in ("dtr", "rts"):
        val = conf.get("serial", sig)
        if val in ("0", "1"):
            try:
                setattr(ser, sig, val == "1")
            except Exception:
                pass
    return ser, port


def capture(conf: Conf, until=None, timeout=60.0, linger=2.0, reset_first=False,
            out: str | None = None, stamp=False, reconnect=True) -> dict:
    """抓串口日志到文件，返回元数据。"""
    serial = _import_serial()
    if reset_first:
        do_reset(conf)
        time.sleep(0.3)

    until_re = re.compile(until, re.MULTILINE | re.IGNORECASE) if until else None
    out_path = Path(out) if out else _stamp_name(conf, "boot")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ser, port = _open_serial(conf)
    info(f"开始抓取 {port} -> {out_path}  (timeout={timeout}s"
         + (f", until=/{until}/" if until else "") + ")")

    dec = codecs.getincrementaldecoder("utf-8")("replace")
    t0 = time.time()
    deadline = t0 + timeout
    matched_at = None
    pending = ""
    tail = ""
    nlines = 0
    reconnects = 0
    timed_out = False

    f = open(out_path, "w", encoding="utf-8", newline="\n")
    try:
        while True:
            now = time.time()
            if matched_at is not None and (now - matched_at) >= linger:
                break
            if now >= deadline:
                timed_out = matched_at is None
                break
            try:
                n = ser.in_waiting
                raw = ser.read(n if n else 1)
            except (serial.SerialException, OSError) as exc:
                if not reconnect or reconnects > 60:
                    die(f"串口读取失败: {exc}")
                reconnects += 1
                info(f"串口掉线（{exc}），重连中 [{reconnects}]...")
                try:
                    ser.close()
                except Exception:
                    pass
                time.sleep(0.5)
                try:
                    ser = _make_port(serial, port, conf.get_int("serial", "baud", 115200))
                except Exception:
                    continue
                continue
            if not raw:
                continue
            # 先攒够一整行再规整换行：chunk 边界可能把 \r\n 劈成两半
            pending += dec.decode(raw)
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                _emit(f, line.rstrip("\r"), t0, stamp)
                nlines += 1
                tail = (tail + line + "\n")[-8192:]
                if until_re and matched_at is None and until_re.search(tail):
                    matched_at = time.time()
                    info(f"命中 until 规则，{linger}s 后停止")
        if pending:
            _emit(f, pending, t0, stamp)
            nlines += 1
    finally:
        f.close()
        try:
            ser.close()
        except Exception:
            pass

    meta = {
        "log": str(out_path), "port": port, "started": _dt.datetime.fromtimestamp(t0).isoformat(),
        "duration_s": round(time.time() - t0, 2), "lines": nlines,
        "until": until, "matched": matched_at is not None, "timed_out": timed_out,
        "reconnects": reconnects,
    }
    out_path.with_suffix(out_path.suffix + ".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    status = "OK" if matched_at is not None else ("TIMEOUT" if timed_out else "EOF")
    print(f"--- bspctl: {status} | {nlines} 行 | {meta['duration_s']}s | {out_path} ---")
    return meta


def _emit(f, line: str, t0: float, stamp: bool):
    if stamp:
        delta = time.time() - t0
        out = f"[{delta:9.3f}] {line}"
    else:
        out = line
    f.write(out + "\n")
    f.flush()
    if not PRINT["quiet"]:
        print(out, flush=True)


def serial_cmd(conf: Conf, commands, timeout=20.0, settle=0.4, reset_first=False,
               out: str | None = None) -> list:
    """在板子上执行命令，用哨兵串取回输出与退出码。"""
    serial = _import_serial()
    if reset_first:
        do_reset(conf)
        time.sleep(0.3)
    ser, port = _open_serial(conf)
    transcript = []

    def drain(duration: float) -> str:
        buf, end = "", time.time() + duration
        while time.time() < end:
            try:
                n = ser.in_waiting
                chunk = ser.read(n if n else 1)
            except Exception as exc:
                die(f"串口读取失败: {exc}")
            if chunk:
                buf += _clean(chunk.decode("utf-8", "replace"))
        return buf

    results = []
    try:
        drain(settle)  # 清掉开机残留
        for command in commands:
            mid = random.randint(10000, 99999)
            marker = f"__BSPCTL_EOF_{mid}__"
            # 关键：让"回显的命令行"里不含已解析的 marker。
            # 终端会把发出去的那行原样回显，若命令里直接写 marker，
            # 一回显就会误命中哨兵 —— 这是最容易踩的坑。
            unresolved = f"__BSPCTL_EOF_$(echo {mid})__"
            line = f"{command} ; rc=$? ; echo {unresolved} $rc"
            ser.write((line + _eol(conf)).encode("utf-8"))
            ser.flush()

            # 按"完整行"收：只有收到整行才判断哨兵。
            # 否则 marker 和它后面的 rc 可能被 TCP/串口切成两个 chunk，
            # 提前 break 会解析出 rc=None。
            pending, got, found = "", [], False
            end = time.time() + timeout
            while not found and time.time() < end:
                n = ser.in_waiting
                chunk = ser.read(n if n else 1)
                if not chunk:
                    continue
                pending += ANSI_RE.sub("", chunk.decode("utf-8", "replace"))
                while "\n" in pending:
                    ln, pending = pending.split("\n", 1)
                    ln = ln.rstrip("\r")
                    got.append(ln)
                    if marker in ln:
                        found = True
                        break
            if not found and pending.strip():
                got.append(pending.rstrip("\r"))

            timed_out = not found
            rc, text = None, ""
            if found:
                idx = next(i for i, ln in enumerate(got) if marker in ln)
                body, m = got[:idx], re.search(re.escape(marker) + r"\s*(-?\d+)", got[idx])
                rc = int(m.group(1)) if m else None
                # 丢掉回显行（含未被 shell 展开的 BSPCTL_EOF_）及其之前的行
                cut = 0
                for i, ln in enumerate(body):
                    if "BSPCTL_EOF_" in ln:
                        cut = i + 1
                text = "\n".join(body[cut:]).strip("\n")
            results.append({"cmd": command, "rc": rc, "out": text, "timed_out": timed_out})
            transcript.append(f"$ {command}\n{text}\n{'-' * 60}")
    finally:
        try:
            ser.close()
        except Exception:
            pass

    for r in results:
        print(f"$ {r['cmd']}")
        print(r["out"] if r["out"] else "(无输出)")
        tail = "[TIMEOUT]" if r["timed_out"] else f"[rc={r['rc']}]"
        print(tail)
        print("-" * 60)

    if out:
        Path(out).write_text("\n".join(transcript), encoding="utf-8")
        info(f"transcript -> {out}")

    return results


# ---------------------------------------------------------------- 复位 / 烧写 / 构建

def do_reset(conf: Conf) -> int:
    mode = conf.get("reset", "mode").lower()
    delay = conf.get_int("reset", "delay", 6)

    if mode in ("none", ""):
        info("未配置复位方式（[reset] mode），跳过复位。")
        info("  可用: uhubctl | relay | cmd | gpio")
        return 1
    if mode == "uhubctl":
        hub, port = conf.get("reset", "hub"), conf.get("reset", "port")
        if not hub or not port:
            die("uhubctl 模式需要 [reset] hub 和 port，例如 hub=1-1  port=2")
        run(["uhubctl", "-l", hub, "-p", port, "-a", "cycle", "-d", str(delay)])
    elif mode == "cmd":
        cmd = conf.get("reset", "cmd")
        if not cmd:
            die("cmd 模式需要 [reset] cmd")
        run(cmd.format(delay=delay))
    elif mode == "relay":
        serial = _import_serial()
        port = conf.get("reset", "relay_port")
        on, off = conf.get("reset", "relay_on"), conf.get("reset", "relay_off")
        if not (port and on and off):
            die("relay 模式需要 [reset] relay_port / relay_on / relay_off（十六进制字节串）")

        def send_hex(payload: str):
            data = bytes.fromhex(payload.replace(" ", "").replace(",", ""))
            with serial.Serial(port, 9600, timeout=0.5) as r:
                r.write(data)
                r.flush()

        send_hex(off)
        time.sleep(delay)
        send_hex(on)
        info(f"继电器复位完成（断电 {delay}s）")
    elif mode == "gpio":
        cmd = conf.get("reset", "cmd")
        if not cmd:
            die("gpio 模式需要 [reset] cmd，例如 gpioset gpiochip0 17=1; sleep 3; gpioset gpiochip0 17=0")
        run(cmd.format(delay=delay))
    else:
        die(f"未知复位模式: {mode}")
    info("复位已触发")
    return 0


def do_flash(conf: Conf, yes=False) -> int:
    mode = conf.get("flash", "mode").lower()
    image = conf.get("flash", "image")

    if mode in ("none", ""):
        die("未配置烧写方式（[flash] mode）。可用: sdcard | cmd")
    if not image or not Path(image).exists():
        die(f"镜像不存在: {image!r}，请设置 [flash] image")

    if mode == "cmd":
        cmd = conf.get("flash", "cmd")
        if not cmd:
            die("cmd 模式需要 [flash] cmd，可用 {image} 占位，例如:\n"
                "  cmd = rkdeveloptool wl 0 {image}")
        run(cmd.format(image=image))
        return 0

    if mode == "sdcard":
        if os.name == "nt":
            die("sdcard 模式不支持 Windows。用 balenaEtcher / Win32DiskImager，"
                "或改用 [flash] mode=cmd")
        device = conf.get("flash", "device")
        if not re.fullmatch(r"/dev/(sd[a-z]+|mmcblk\d+|nvme\d+n\d+|disk\d+)", device or ""):
            die(f"[flash] device 不合法: {device!r}（必须是 /dev/sdX / /dev/mmcblkX 这类）")

        root_src = subprocess.run(["findmnt", "-no", "SOURCE", "/"], capture_output=True,
                                  text=True).stdout.strip()
        if root_src.startswith(device):
            die(f"拒绝执行: {device} 上就是当前根文件系统（{root_src}）")
        mounted = subprocess.run(["findmnt", "-S", device, "-no", "TARGET"], capture_output=True,
                                 text=True).stdout.strip()
        if mounted:
            die(f"拒绝执行: {device} 仍被挂载于 {mounted}，先 umount")

        info(f"将向 {device} 写入 {image}（{Path(image).stat().st_size / 1e6:.1f} MB）")
        if not yes:
            if not sys.stdin.isatty():
                die("非交互环境必须加 --yes")
            if input(f"确认覆盖 {device}? 输入设备名确认: ").strip() != device:
                die("已取消")
        run(["dd", f"if={image}", f"of={device}", "bs=4M", "conv=fsync", "status=progress"])
        run(["sync"])
        return 0

    die(f"未知烧写模式: {mode}")


ERROR_RE = re.compile(
    r"(?i)(\berror\b|\bfatal\b|undefined reference|No rule to make target|"
    r"recipe for target .* failed|make(\[\d+\])?: \*\*\*|CMake Error|configure: error|"
    r"cannot find -l|Segmentation fault|Killed)")


def do_build(conf: Conf, extra: str = "") -> int:
    cmd = conf.get("build", "cmd")
    if not cmd:
        die("未配置 [build] cmd，例如:\n"
            "  cmd = make -C buildroot O=output BR2_EXTERNAL=$PWD/../br2-external myboard_defconfig all")
    if extra:
        cmd = f"{cmd} {extra}"

    logdir = conf.logdir("build")
    log = logdir / f"build-{_dt.datetime.now():%Y%m%d-%H%M%S}.log"
    info(f"构建: {cmd}")
    info(f"日志: {log}")

    errors, warnings = [], []
    code = 0
    with open(log, "w", encoding="utf-8", newline="\n") as f:
        try:
            proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace", bufsize=1)
        except FileNotFoundError as exc:
            die(f"无法启动构建: {exc}")
        for line in proc.stdout:
            f.write(line)
            if not PRINT["quiet"]:
                sys.stdout.write(line)
                sys.stdout.flush()
            if ERROR_RE.search(line):
                errors.append(line.rstrip())
            elif "warning:" in line.lower():
                warnings.append(line.rstrip())
        code = proc.wait()

    print(f"\n--- bspctl: build {'OK' if code == 0 else f'FAILED (exit {code})'} ---")
    print(f"日志: {log}  | 错误行: {len(errors)}  | warning: {len(warnings)}")
    if errors:
        print("\n首批错误（已去重）:")
        seen = set()
        for line in errors:
            key = line.strip()[:120]
            if key in seen:
                continue
            seen.add(key)
            print(f"  {line.strip()[:200]}")
            if len(seen) >= 12:
                break
        print(f"\n完整错误请直接让模型读: {log}（搜 '{errors[0].strip()[:40]}' 前后 30 行）")
    return code


# ---------------------------------------------------------------- 日志分析

BOOT_MARKERS = [
    (r"U-Boot \d", "U-Boot 启动"),
    (r"Booting Linux on physical CPU", "内核开始"),
    (r"Linux version \d", "内核版本横幅"),
    (r"Freeing unused kernel memory", "kernel_init 结束（进入用户态前）"),
    (r"Run /(sbin/)?init as init process", "exec init"),
    (r"systemd\[1\]: (Started|Reached target)", "systemd 起来"),
    (r"(?i)login:\s*$|Welcome to ", "登录提示"),
]

TS_RE = re.compile(r"^\[\s*(\d+\.\d+)\]\s?(.*)$")


def _parse_log(path: Path):
    rows = []
    for raw in _read_text(path).splitlines():
        m = TS_RE.match(raw)
        if m:
            rows.append((float(m.group(1)), m.group(2)))
        else:
            rows.append((None, raw))
    return rows


def do_boottime(path: Path) -> int:
    if not path.is_file():
        die(f"日志不存在: {path}")
    rows = _parse_log(path)
    print(f"=== 启动阶段 (kernel 时间戳，单位 s) ===\n{path}\n")

    # 单趟扫描，保持日志顺序。无时间戳的行（U-Boot / 用户态）继承最近的内核时间戳，
    # 语义是"发生在这个时刻之后"，用 ≥ 标记出来，不用它算 delta。
    found, seen, last_ts = [], set(), None
    for pos, (ts, text) in enumerate(rows):
        eff = ts if ts is not None else last_ts
        if ts is not None:
            last_ts = ts
        for pattern, label in BOOT_MARKERS:
            if label in seen:
                continue
            if re.search(pattern, text, re.IGNORECASE):
                seen.add(label)
                found.append({"pos": pos, "ts": eff, "text": text.strip()[:80],
                              "label": label, "derived": ts is None})

    if not found:
        print("未识别到任何标准启动标记。可能原因：")
        print("  - 日志不是 dmesg 格式（U-Boot 阶段的输出没有 [  0.000000] 时间戳）")
        print("  - 板子没有跑到内核；用 `bspctl.py oops` 看是否有崩溃")
        return 1

    width = max(len(h["label"]) for h in found)
    prev = None
    for h in found:
        ts, eff = h["ts"], h["ts"]
        if eff is None:
            ts_s = "   n/a    "
        elif h["derived"]:
            ts_s = f"  ≥{eff:8.3f}"
        else:
            ts_s = f"[{eff:9.3f}]"
        delta = ""
        if prev is not None and h["ts"] is not None and not h["derived"]:
            delta = f"  (+{h['ts'] - prev:.3f}s)"
        print(f"  {ts_s} {h['label'].ljust(width)}{delta}")
        print(f"             {h['text']}")
        if h["ts"] is not None and not h["derived"]:
            prev = h["ts"]

    kernel_only = [h for h in found if h["ts"] is not None and not h["derived"]]
    if kernel_only:
        print(f"\n内核时间轴合计: {kernel_only[-1]['ts']:.3f}s"
              f"（到 '{kernel_only[-1]['label']}'）")
    if len(kernel_only) > 1:
        slow = max(zip(kernel_only, kernel_only[1:]),
                   key=lambda pr: pr[1]["ts"] - pr[0]["ts"])
        gap = slow[1]["ts"] - slow[0]["ts"]
        print(f"最大阶段耗时: {slow[0]['label']} -> {slow[1]['label']} = {gap:.3f}s")
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    if meta_path.is_file():
        meta = json.loads(_read_text(meta_path))
        print(f"实际抓取时长: {meta.get('duration_s')}s（含 U-Boot / 掉电等待）")
    print("\n注：U-Boot 阶段没有内核时间戳，需要逻辑分析仪或 U-Boot 的 "
          "CONFIG_BOOTSTAGE / bootcounter 才能精确测量。")
    return 0


OOPS_KINDS = {
    "PANIC":  r"Kernel panic - not syncing|end Kernel panic",
    "OOPS":   r"Unable to handle kernel|Internal error: Oops|Oops:|Unable to handle kernel paging request",
    "BUG":    r"kernel BUG at |BUG: ",
    "WARN":   r"WARNING: CPU: \d+|WARNING: at ",
    "LOCKDEP": r"WARNING: possible (circular|recursive) locking|possible deadlock",
    "HUNG":   r"soft lockup|hard LOCKUP|watchdog: BUG: soft lockup",
    "RCU":    r"rcu_sched detected stalls|RCU Stall|INFO: rcu_.* stall",
    "OOM":    r"Out of memory: Killed process|oom-kill:",
}

CRASH_HEAD = [
    r"^\s*(pc|lr|sp|el|spsr_el\w*)\s*:\s*",
    r"^PC is at |^LR is at |^pc : |^lr : ",
    r"^CPU: \d+ PID:",
    r"^Modules linked in:",
    r"^Hardware name:",
    r"^Call trace:",
    r"^Workqueue:|^Task:",
]


def do_oops(path: Path, context: int = 40) -> int:
    if not path.is_file():
        die(f"日志不存在: {path}")
    lines = _read_text(path).splitlines()

    hits = []
    for idx, raw in enumerate(lines):
        body = TS_RE.sub(r"\2", raw).strip()   # 去掉 [  1.500900] 前缀再匹配
        for kind, pattern in OOPS_KINDS.items():
            if re.search(pattern, body):
                hits.append((idx, kind, body))
    hits.sort(key=lambda h: h[0])
    if not hits:
        print(f"未在 {path} 中发现崩溃/告警模式。")
        print("可检查: 板子是否根本没启动、日志是否被截断、串口波特率是否错误。")
        return 1

    # 合并重叠窗口（命中点按行号排序，所以只需要比最后一个窗口）
    windows = []
    for idx, kind, _ in hits:
        lo, hi = max(0, idx - context // 2), min(len(lines), idx + context)
        if windows and lo <= windows[-1][1]:
            windows[-1][0] = min(windows[-1][0], lo)
            windows[-1][1] = max(windows[-1][1], hi)
            windows[-1][2].append(kind)
        else:
            windows.append([lo, hi, [kind]])

    kinds = sorted({k for _, k, _ in hits})
    print("=" * 72)
    print(f"崩溃现场摘要  文件: {path}")
    print(f"命中类型: {', '.join(kinds)}   命中次数: {len(hits)}   窗口: {len(windows)}")
    print("=" * 72)

    for _, _, body in hits[:8]:
        print(f"  ! {body[:160]}")
    if len(hits) > 8:
        print(f"  ... 另有 {len(hits) - 8} 处")

    key = []
    for raw in lines:
        body = TS_RE.sub(r"\2", raw).strip()
        if any(re.search(p, body) for p in CRASH_HEAD):
            key.append(body)
    if key:
        print("\n--- 关键寄存器 / 调用栈 ---")
        for body in key[:16]:
            print(f"  {body[:160]}")
        if len(key) > 16:
            print(f"  ... 另有 {len(key) - 16} 行")

    for i, (lo, hi, ks) in enumerate(windows, 1):
        print(f"\n--- 上下文 #{i} (第 {lo + 1}-{hi} 行, {','.join(sorted(set(ks)))}) ---")
        for l in lines[lo:hi]:
            print(f"  {l[:200]}")
    print("\n提示：把本输出连同 vmlinux / 内核源码一起交给模型，"
          "能显著提高定位精度；没有 vmlinux 时模型只能猜函数名。")
    return 0


NORM_RULES = [
    (re.compile(r"^\[\s*\d+\.\d+\]\s?"), "[TS] "),
    (re.compile(r"0x[0-9a-fA-F]{4,16}"), "0xADDR"),
    (re.compile(r"\[<[0-9a-fA-F]{6,16}>\]"), "[<ADDR>]"),
    (re.compile(r"\b[0-9a-fA-F]{8,16}\b"), "ADDR"),
    (re.compile(r"\b\d{4,}-\d{2}-\d{2}T[\d:.+-]+"), "<TIMESTAMP>"),
    (re.compile(r"\s+$"), ""),
]


def _norm(line: str) -> str:
    # 裸的十六进制数字只有在"看起来像地址"时才替换，避免把版本号、大小也改掉
    for rx, rep in NORM_RULES[:-1]:
        line = rx.sub(rep, line)
    return NORM_RULES[-1][0].sub("", line)


def do_diff(a: Path, b: Path, out: str | None = None) -> int:
    for p in (a, b):
        if not p.is_file():
            die(f"日志不存在: {p}")
    la = [_norm(l) for l in _read_text(a).splitlines()]
    lb = [_norm(l) for l in _read_text(b).splitlines()]
    diff = list(difflib.unified_diff(la, lb, fromfile=str(a), tofile=str(b), n=2, lineterm=""))

    if not diff:
        print("两份日志归一化后完全一致。")
        return 0
    added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
    removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
    print(f"归一化 diff: -{removed} / +{added}  (A={a.name}  B={b.name})")
    print("=" * 72)
    text = "\n".join(diff)
    print(text if len(diff) < 400 else "\n".join(diff[:400]) + "\n... (截断)")
    if out:
        Path(out).write_text(text, encoding="utf-8")
        info(f"完整 diff -> {out}")
    return 0


# ---------------------------------------------------------------- 板级冒烟

BUILTIN_SMOKE = [
    ("dtb-model", "cat /proc/device-tree/model", r".+"),
    ("uptime", "cat /proc/uptime", r"\d"),
    ("kmsg-clean", 'dmesg | grep -E "Unable to handle|Kernel panic|BUG: "', r"!"),
    ("rootfs-rw", "touch /tmp/.bsp_smoke && rm /tmp/.bsp_smoke && echo ok", r"ok"),
    ("disk", "df -h / | tail -1", r"\d+%"),
    ("thermal", "cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo NA", r"\d"),
]


def load_smoke(path: Path):
    cases = []
    if path.is_file():
        for raw in _read_text(path).splitlines():
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            parts = [p.strip() for p in raw.split("|")]
            if len(parts) >= 3:
                cases.append((parts[0], parts[1], parts[2]))
    return cases


def do_smoke(conf: Conf, file: str | None = None) -> int:
    path = Path(file) if file else (conf.path.parent / "smoke.tsv" if conf.path else ROOT / "smoke.tsv")
    cases = load_smoke(path) or BUILTIN_SMOKE
    info(f"冒烟用例 {len(cases)} 条（{path if path.is_file() else '内置默认'}）")

    results = serial_cmd(conf, [c[1] for c in cases], timeout=25,
                         out=str(conf.logdir("serial") / f"smoke-{_dt.datetime.now():%Y%m%d-%H%M%S}.log"))
    print("\n=== 冒烟结果 ===")
    failed = 0
    for (name, cmd, expect), res in zip(cases, results):
        out = res["out"] or ""
        negate = expect.startswith("!")
        pattern = expect.lstrip("!") or r".+"
        hit = re.search(pattern, out, re.IGNORECASE) is not None
        ok = (not hit) if negate else hit
        failed += 0 if ok else 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<14} rc={res['rc']}  "
              f"{'' if ok else '| 期望' + ('不出现' if negate else '出现') + f' /{pattern}/'}")
        if not ok:
            print(f"        输出: {out[:200]!r}")
    print(f"\n{len(cases) - failed}/{len(cases)} 通过")
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------- 主流程

def do_ssh(conf: Conf, command: str) -> int:
    ip, user = conf.get("board", "ip"), conf.get("board", "user")
    if not ip:
        die("未配置 [board] ip")
    extra = conf.get("board", "ssh_extra")
    port = conf.get("board", "ssh_port")
    cmd = f'ssh -p {port} {extra} {user}@{ip} "{command}"'
    proc = subprocess.run(cmd, shell=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode


def do_up(conf: Conf, until, timeout, build, flash, yes) -> int:
    if build and do_build(conf) != 0:
        die("构建失败，已中止（不烧一块编译不过的板子）", 1)
    if flash and do_flash(conf, yes=yes) != 0:
        die("烧写失败，已中止", 1)
    do_reset(conf)
    meta = capture(conf, until=until, timeout=timeout, reset_first=False)
    if meta["lines"] == 0:
        print("\n!! 串口 0 行输出。检查：串口接对了吗？板子上电了吗？"
              "波特率对吗？USB 转串口 TX/RX 是否交叉？", file=sys.stderr)
        return 1
    do_boottime(Path(meta["log"]))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="bspctl", description="Linux BSP 工作台 (U-Boot / Kernel / Buildroot)",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("-c", "--conf", help="配置文件路径（默认 ./bsp.conf）")
    ap.add_argument("-q", "--quiet", action="store_true", help="不打印串口实时输出")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("log", help="抓串口日志")
    p.add_argument("--until", help="停止条件正则，如 'login:'")
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--linger", type=float, default=2.0, help="命中后继续抓多久")
    p.add_argument("--reset", action="store_true", help="抓之前先复位")
    p.add_argument("--out")
    p.add_argument("--stamp", action="store_true", help="每行加相对时间戳")

    p = sub.add_parser("cmd", help="通过串口执行命令")
    p.add_argument("commands", nargs="+")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--settle", type=float, default=0.4)
    p.add_argument("--reset", action="store_true")
    p.add_argument("--out")

    p = sub.add_parser("ssh", help="在板子上执行命令")
    p.add_argument("command")

    p = sub.add_parser("build", help="构建")
    p.add_argument("extra", nargs="?", default="", help="追加到构建命令末尾，如某个 target")

    p = sub.add_parser("flash", help="烧写镜像")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("reset", help="复位/上下电")

    p = sub.add_parser("up", help="reset -> log -> 启动耗时")
    p.add_argument("--until", default="login:")
    p.add_argument("--timeout", type=float, default=90.0)
    p.add_argument("--build", action="store_true")
    p.add_argument("--flash", action="store_true")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("smoke", help="板级冒烟测试")
    p.add_argument("--file", help="用例文件，默认 smoke.tsv")

    p = sub.add_parser("boottime", help="提取启动耗时")
    p.add_argument("log")

    p = sub.add_parser("oops", help="提取崩溃现场")
    p.add_argument("log")
    p.add_argument("--context", type=int, default=40)

    p = sub.add_parser("diff", help="比对两份日志")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--out")

    args = ap.parse_args(argv)
    PRINT["quiet"] = args.quiet
    conf = load_conf(args.conf)
    # 纯日志分析类命令（boottime / oops / diff）不需要板子配置，别拿警告打扰它们
    if args.cmd in ("log", "cmd", "ssh", "build", "flash", "reset", "up", "smoke"):
        if conf.path:
            info(f"配置: {conf.path}")
        else:
            info("未找到 bsp.conf，使用默认值（先 cp bsp.conf.example bsp.conf 并改 [serial] port）")

    if args.cmd == "log":
        capture(conf, until=args.until, timeout=args.timeout, linger=args.linger,
                reset_first=args.reset, out=args.out, stamp=args.stamp)
        return 0
    if args.cmd == "cmd":
        res = serial_cmd(conf, args.commands, timeout=args.timeout, settle=args.settle,
                         reset_first=args.reset, out=args.out)
        return 0 if all(r["rc"] in (None, 0) and not r["timed_out"] for r in res) else 1
    if args.cmd == "ssh":
        return do_ssh(conf, args.command)
    if args.cmd == "build":
        return do_build(conf, args.extra)
    if args.cmd == "flash":
        return do_flash(conf, yes=args.yes)
    if args.cmd == "reset":
        return do_reset(conf)
    if args.cmd == "up":
        return do_up(conf, args.until, args.timeout, args.build, args.flash, args.yes)
    if args.cmd == "smoke":
        return do_smoke(conf, args.file)
    if args.cmd == "boottime":
        return do_boottime(Path(args.log))
    if args.cmd == "oops":
        return do_oops(Path(args.log), args.context)
    if args.cmd == "diff":
        return do_diff(Path(args.a), Path(args.b), args.out)
    die(f"未知子命令 {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
