#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bsp_mcp.py — 把 BSP 工作台暴露成 MCP 工具，让 AI 能"自己看板子"

用法
----
    export BSPCTL_CONF=/path/to/bsp.conf          # Linux/macOS
    $env:BSPCTL_CONF = "F:\\deeseek\\workspace\\bsp\\bsp.conf"   # Windows
    python bsp_mcp.py                             # stdio 传输

客户端配置（Claude Desktop / DSH / 其他 MCP 客户端）示例：

    {
      "mcpServers": {
        "bsp": {
          "command": "python",
          "args": ["F:/deeseek/workspace/bsp/bsp_mcp.py"],
          "env": { "BSPCTL_CONF": "F:/deeseek/workspace/bsp/bsp.conf" }
        }
      }
    }

设计要点
--------
1. 所有工具都不返回完整日志 —— 日志落盘，只把「路径 + 尾部 N 行 + 结构化摘要」回给模型，
   否则一次 3MB 的启动日志会直接把上下文打爆。
2. 破坏性操作（烧写）必须显式 confirm=True，防止模型"顺手"重新烧板子。
3. 工具名用 board_ 前缀，便于和文件系统类工具区分。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path

try:
    # MCP SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server
except Exception:  # noqa: BLE001 - 2.x 里 fastmcp 被改名为 MCPServer
    try:
        from mcp.server.mcpserver import MCPServer as _Server
    except ImportError:
        sys.exit("缺少 MCP SDK。安装: python -m pip install -r requirements.txt")

import bspctl

mcp = _Server("bsp")

TAIL_LINES = 120


# ---------------------------------------------------------------- 内部工具

def _conf() -> "bspctl.Conf":
    return bspctl.load_conf(os.environ.get("BSPCTL_CONF"))


def _capture_stdout(fn, *a, **kw):
    """运行 bspctl 的函数并捕获其 stdout（这些函数是设计给终端用的）。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*a, **kw)
    return rc, buf.getvalue()


def _tail(path: str, n: int = TAIL_LINES) -> str:
    p = Path(path)
    if not p.is_file():
        return f"(文件不存在: {path})"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    body = "\n".join(lines[-n:])
    head = f"(共 {len(lines)} 行, 显示最后 {min(n, len(lines))} 行)\n" if len(lines) > n else ""
    return head + body


def _fmt(path: str, text: str, limit: int = 6000) -> str:
    if len(text) > limit:
        text = text[:limit] + f"\n... (截断, 完整内容见 {path})"
    return text


# ---------------------------------------------------------------- 观察类工具

@mcp.tool()
def board_list_serial() -> str:
    """列出主机上可用的串口设备。配置前先调这个确认端口名。"""
    try:
        import serial.tools.list_ports as lp
    except ImportError:
        return "缺少 pyserial: python -m pip install pyserial"
    ports = [f"{p.device}\t{p.description}\t{p.hwid}" for p in lp.comports()]
    return "\n".join(ports) if ports else "未发现串口设备"


@mcp.tool()
def board_capture_log(seconds: float = 60, until: str = "login:", reset: bool = False) -> str:
    """抓取板子串口输出。

    这是最重要的工具：板子起不来、驱动 probe 失败、hang 住，都先用它拿第一手日志。

    Args:
        seconds: 最长抓取时间（秒）。
        until: 停止条件正则，默认等到登录提示。置空字符串表示抓满 seconds。
        reset: True 则先复位板子再抓（需要配置 [reset]）。
    """
    conf = _conf()
    try:
        rc, out = _capture_stdout(
            bspctl.capture, conf, until=(until or None), timeout=seconds,
            reset_first=reset, stamp=False)
    except SystemExit as exc:
        return f"抓取失败(exit={exc.code})"
    # 日志路径在函数返回的 meta 里，这里回读最近一个 meta 文件
    metas = sorted(conf.logdir("serial").glob("boot-*.log.meta.json"))
    if not metas:
        return "未生成日志文件\n" + out[-2000:]
    meta = json.loads(metas[-1].read_text(encoding="utf-8"))
    status = "命中 until" if meta["matched"] else ("超时未命中" if meta["timed_out"] else "结束")
    return (f"状态: {status} | {meta['lines']} 行 | {meta['duration_s']}s\n"
            f"日志: {meta['log']}\n"
            f"--- 尾部输出 ---\n{_tail(meta['log'])}")


@mcp.tool()
def board_run(commands: list[str], timeout: float = 20) -> str:
    """通过串口在板子上执行命令，返回输出和退出码。

    用哨兵方式取回退出码，比匹配 shell 提示符可靠。板子必须已经起到能敲命令。

    Args:
        commands: 要执行的命令列表，顺序执行。
        timeout: 单条命令超时（秒）。
    """
    conf = _conf()
    try:
        rc, out = _capture_stdout(bspctl.serial_cmd, conf, commands, timeout=timeout)
    except SystemExit as exc:
        return f"执行失败(exit={exc.code})"
    return _fmt("(见 board_capture_log)", out)


@mcp.tool()
def board_ssh(command: str) -> str:
    """通过 SSH 在板子上执行命令（需要 [board] ip 且已配置免密）。

    比串口快、无回显干扰，适合读 /proc /sys、抓 dmesg、跑测试脚本。
    """
    conf = _conf()
    rc, out = _capture_stdout(bspctl.do_ssh, conf, command)
    return f"[rc={rc}]\n{out}"


# ---------------------------------------------------------------- 分析类工具

@mcp.tool()
def board_boottime(log_path: str) -> str:
    """从启动日志提取各阶段耗时（内核时间戳）。"""
    rc, out = _capture_stdout(bspctl.do_boottime, Path(log_path))
    return out


@mcp.tool()
def board_triage_crash(log_path: str, context: int = 40) -> str:
    """从日志里抽取 oops / panic / WARN / lockdep / RCU stall / OOM 现场。

    返回值已经整理成"可读的崩溃报告"，包含关键寄存器行和调用栈。
    下一步务必让它配合 vmlinux 和源码做符号化定位，不要凭空猜函数。
    """
    rc, out = _capture_stdout(bspctl.do_oops, Path(log_path), context)
    return _fmt(log_path, out, limit=12000)


@mcp.tool()
def board_diff_logs(log_a: str, log_b: str) -> str:
    """比对两份日志（好板 vs 坏板、改前 vs 改后）。

    先做归一化（时间戳、地址替换掉），只留下真正的内容差异。
    这是找"这次改动引入了什么"最快的办法。
    """
    rc, out = _capture_stdout(bspctl.do_diff, Path(log_a), Path(log_b), None)
    return _fmt(log_a, out, limit=12000)


# ---------------------------------------------------------------- 动作类工具

@mcp.tool()
def board_build(target: str = "") -> str:
    """触发构建（Buildroot / kernel / U-Boot），返回错误摘要和日志路径。

    Args:
        target: 追加到构建命令末尾的目标，例如 "linux-rebuild"。
    """
    conf = _conf()
    rc, out = _capture_stdout(bspctl.do_build, conf, target)
    logs = sorted(conf.logdir("build").glob("build-*.log"))
    path = str(logs[-1]) if logs else "(无日志)"
    return f"[exit={rc}]\n{_fmt(path, out[-4000:])}\n构建日志: {path}"


@mcp.tool()
def board_reset() -> str:
    """复位 / 上下电板子（uhubctl / USB 继电器 / 自定义命令）。"""
    conf = _conf()
    rc, out = _capture_stdout(bspctl.do_reset, conf)
    return f"[exit={rc}] {out[-800:]}"


@mcp.tool()
def board_flash(confirm: bool = False) -> str:
    """烧写镜像到板子。

    破坏性操作：只有调用方显式传 confirm=True 才执行。
    模型在没有人类明确指令时绝不应该传 True。
    """
    if not confirm:
        return ("已拒绝：board_flash 是破坏性操作。"
                "需要人类明确要求烧写后，才可以用 confirm=True 调用。")
    conf = _conf()
    rc, out = _capture_stdout(bspctl.do_flash, conf, True)
    return f"[exit={rc}] {out[-1500:]}"


@mcp.tool()
def board_smoke() -> str:
    """跑板级冒烟测试（smoke.tsv 或内置用例），返回 PASS/FAIL 汇总。"""
    conf = _conf()
    try:
        rc, out = _capture_stdout(bspctl.do_smoke, conf, None)
    except SystemExit as exc:
        return f"冒烟失败(exit={exc.code})"
    return _fmt("(见 smoke 日志)", out)


@mcp.tool()
def board_bringup_checklist(stage: str = "spl") -> str:
    """按启动阶段返回检查清单，避免"凭感觉乱试"。

    Args:
        stage: rom | spl | atf | uboot | kernel | userspace
    """
    return CHECKLISTS.get(stage.lower(), "未知阶段。可用: " + ", ".join(CHECKLISTS))


CHECKLISTS = {
    "rom": """\
[ROM / BootROM 阶段] 串口完全无输出的第一站
1. 供电：核心电压、IO 电压是否在规格内？用万用表实测，别信丝印。
2. 启动介质 strapping：BOOT_MODE 电阻是否按目标介质配置？上电瞬间电平是否稳定？
3. 参考时钟：晶振是否起振？（示波器看波形 + 频率，别只看"有电压"）
4. 复位：RESET_N 是否释放？上电时序里各轨先后是否符合 datasheet？
5. 串口：TX/RX 是否交叉？波特率？UART 引脚是否被复用成别的功能？
6. 存储介质：eMMC/SPI-NOR 焊接、供电、CLK 波形。
判定：ROM 有输出→进 spl；无输出→先解决上面 6 条，别改代码。
""",
    "spl": """\
[SPL / 二级引导] ROM 能出字，SPL 不出
1. DDR 初始化：DDR 参数是否来自 SoC 厂商工具（如 imx ddr tool / rk ddrbin）？频率是否超规格？
2. 打印：SPL 是否开启了 debug uart？CONFIG_SPL_SERIAL 是否指向正确的 UART 实例？
3. SPL 镜像头：是否正确加头（mkimage / rkbin / bootrom header）并被 ROM 认可？
4. 加载地址：SPL 的 text base / stack 是否和 ROM 约定一致？是否踩到保留内存？
5. 存储读取：SPL 能否读到 U-Boot？读偏移/分区表对不对？
验证手段：JTAG/OpenOCD halt 看 PC；或打开 SPL 的 early print。
""",
    "atf": """\
[ATF / OP-TEE] SPL 之后、U-Boot 之前
1. BL31 是否正确加载到安全内存？入口地址与 SPL 里的跳转是否一致？
2. DRAM 安全区域划分是否和 kernel 的 reserved-memory 冲突？
3. 串口在 ATF 里是否有输出（很多平台 ATF 默认静默）？
4. PSCI / CPU 上下电是否正常？多核 bringup 卡在哪个核？
5. 安全启动：是否因为签名校验失败而静默复位？（看复位原因寄存器）
""",
    "uboot": """\
[U-Boot] 能进 U-Boot，但起不到内核
1. `printenv`：bootargs / bootcmd 是否指向正确的分区和地址？
2. 存储：`mmc list` / `mmc dev` / `ls` 能否看到 rootfs？分区表对不对？
3. 镜像：Image / Image.gz 的加载地址是否和 kernel 的 TEXT_OFFSET 匹配？dtb 地址是否冲突？
4. 串口没有 kernel 输出：bootargs 里的 console= 是否和实际 UART 一致？earlycon 地址对不对？
5. 直接验证：`booti $kaddr - $fdtaddr` 手动启动，看报错原文。
""",
    "kernel": """\
[Kernel] 内核起来了但挂住 / probe 失败
1. 先看 `earlycon` / `earlyprintk` 有没有开——没有的话你连崩溃都看不到。
2. dmesg 找 `deferred probe`：`cat /sys/kernel/debug/devices_deferred`。
3. 时钟/电源：`clk_summary`、`regulator_summary` 是否给了外设真实的 clock 和电压？
4. pinctrl：引脚是否真的被 mux 成目标功能？看 `debugfs/pinctrl`。
5. 中断：`cat /proc/interrupts`，中断号是否为 0（说明没映射上）。
6. 用 `initcall_debug` 定位卡在哪个 initcall。
""",
    "userspace": """\
[Userspace] 内核起来了，但没有 shell / 服务异常
1. bootargs 的 root= 是否正确？（/dev/mmcblk1p2 vs UUID）
2. rootfs 类型：ext4 / squashfs / overlay 是否编进内核或做了 initramfs？
3. init：/sbin/init 是否存在且可执行？动态库依赖是否齐全（ldd / readelf）？
4. 串口 getty 是否配置（inittab / systemd getty@ttySx）？
5. 只读根文件系统导致 init 写失败：看 `mount` 和内核日志里的 I/O error。
""",
}


if __name__ == "__main__":
    mcp.run()
