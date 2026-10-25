# bsp/ — BSP 工具集

三个工具，按"谁跑、要不要碰板子"分工：

| 工具 | 谁跑 | 干什么 |
|---|---|---|
| `collect.sh` | **人**（板端） | 一次性诊断采集，输出可解析的包。把"一天跑三趟"压成"一趟跑全" |
| `bspdump.py` | **AI**（主机，纯离线） | 解析采集包：信号摘要 / 看某段 / A/B 比对。只用标准库 |
| `bspcheck.py` | **AI**（主机，纯离线） | 上板前体检：DTB 结构校验、oops 地址符号化、`.config` 可诊断性 |
| `bspctl.py` | 人 / AI（主机） | 串口抓取、构建、烧写、复位 + 日志分析（`oops`/`boottime`/`diff`） |

前提：AI 不能自动抓日志、不能烧写。所以重心在**一次采集带回全部证据**，
以及**把能离线判定的都搬到主机侧**。

---

## 安装

```bash
cp bsp.conf.example bsp.conf     # 改成你的串口/板子
pip install -r requirements.txt  # pyserial 必需；mcp 只有接 MCP 时才需要
```

只用 `boottime` / `oops` / `diff` 这三个纯日志分析命令的话，**标准库就够，不需要装任何东西**。

---

## 板端采集：collect.sh（人跑，跑一次）

```bash
sh collect.sh probe                       # 驱动 probe 失败
sh collect.sh crash                       # oops / panic
sh collect.sh boot                        # 起不来
sh collect.sh net / storage / i2c / power
sh collect.sh all -o /tmp/d.txt --no-stream   # 全量，之后 scp 出来
```

选项：
- `-o FILE` 指定输出文件（默认 `/tmp/bspdump-<时间>.txt`）
- `--no-stream` 只写文件不打串口（内容大时用，避免串口回放过久）
- `--max N` 每段字节上限，默认 20000
- `--mount-debugfs` 需要时挂 debugfs（默认不动板子状态）
- `--i2c-scan` 真的扫 I2C 总线（**默认关闭**，某些 PMIC 上可能触发异常）

设计上的几个点：

- **只用 POSIX sh + busybox**，不依赖 python / perl。板子上什么都能跑。
- **全程只读**，不改状态。唯一的例外是 `--mount-debugfs` 和 `--i2c-scan`，都要显式开。
- **段级包裹**：`===BSPDUMP-SECTION:名字===` … `===BSPDUMP-END:名字 size=N TRUNCATED===`。
  这个格式是**为串口设计的**：传输被截断、丢字都没关系，已经收完的段仍然可用，
  解析器能据 `size=` 判断哪段被切了。
- **每段有字节上限**，防止 `dmesg` 或 `pinctrl` 把串口糊死。
- **段内提示信息用英文**：板子的 locale 不一定是 UTF-8，中文在里面会变乱码，
  进而干扰解析；面向人的提示才用中文。

---

## 采集包解析：bspdump.py（AI 跑，随便跑，不占往返）

```bash
python bspdump.py stat d.txt                  # 信号摘要：先看这一屏
python bspdump.py list d.txt                  # 有哪些段、多大、有没有被截断
python bspdump.py show d.txt deferred         # 看某段原文
python bspdump.py show d.txt dmesg --grep "mmc|timeout" --head 40
python bspdump.py diff good.txt bad.txt       # A/B 对照
python bspdump.py diff good.txt bad.txt --full
```

`stat` 会提炼成一张表，**不需要把 300 KB 日志贴进上下文**：

```
| 级别 | 项目 | 内容 |
| CRIT | 崩溃信号（dmesg） | OOPS, PANIC — Unable to handle kernel NULL pointer... |
| WARN | deferred probe | 3 个设备未 probe 成功: pmic@48, phy@f1010000, mmc@f0000000 |
| WARN | rootfs 只读 | /dev/mmcblk0p2 on / type ext4 (ro,relatime) |
| WARN | 最高温度 | 88.2°C |
| INFO | regulator 无消费者 | 1 个（dummy regulator 说明 DT 里没配电源） |
| INFO | dmesg 错误行 | 5 行；时空/probe=2 时钟/电源=2 总线=1 |
```

`diff` 先把时间戳和地址归一化再逐段比，所以好板/坏板的差异会直接浮出来，
不会被 `[ 1.234567]` 和 `0xffff...` 淹没。

**A/B 采集纪律**：每次改动，都抓改前和改后两份。一份观察只能描述现象，
一组对照才能定位原因。

---

## 上板前体检：bspcheck.py（纯离线，不依赖 dt-schema / QEMU）

厂商 SDK 没有 binding YAML，所以这里不做 schema 校验，只查**结构自洽性**——
这些错误编译能过、`dtc` 也不报，但上板必炸，而且现象躲得很深。

```bash
python bspcheck.py dtb  output/images/myboard.dtb
python bspcheck.py sym  --log logs/oops.log --map System.map --vmlinux vmlinux
python bspcheck.py config buildroot/output/build/linux-*/.config
```

### dtb —— 直接解析 FDT，查 8 类问题

| 检查 | 为什么值得查 |
|---|---|
| `reg` 长度 vs 父节点 `#address-cells`/`#size-cells` | 长度不对时内核按错误边界切分，地址和长度**全错**，但不报错 |
| `interrupts` 长度 vs `interrupt-parent` 的 `#interrupt-cells` | 中断号错位，表现为"中断一直不来" |
| 兄弟节点重名（unit-address 冲突） | `dtc` 不报，内核只认一个，另一个静默消失 |
| `clocks`/`resets`/`dmas` 条数 vs `*-names` 数量 | 按 provider 的 `#xxx-cells` **精确**解析，不是简单数数字 |
| phandle 悬空（`*-supply`、`phy-handle` 等单值属性） | 典型现象是 probe 无限 `-EPROBE_DEFER`，很难查 |
| `reserved-memory` 重叠 / 超出 `/memory` | 后者会被直接拿去当普通内存，随机踩内存 |
| 祖先 `disabled` 但子节点 `okay` | 永不 probe，现象是"设备不存在"而不是报错 |
| I2C 从地址越界 / 保留段 | 4 字节 reg 忘了 `#address-cells=1` 时最常见 |

```bash
# 有用选项
--ignore "/soc@0/spi"     # 忽略厂商 SDK 里那些改不了的旧节点
--max 80                  # 默认只显示 60 条
```

**它查不到什么**：属性名拼错（需要 binding 才知道合法属性名）。这类只能靠
`grep Documentation/devicetree/bindings/` 或者看驱动源码。所以它不能替代
`dtbs_check`（如果有的话），只是补上"没有 YAML 也能查"的那一块。

### sym —— 把 oops 里的裸地址变成函数名

```bash
python bspcheck.py sym --log logs/oops.log --map System.map --vmlinux vmlinux
python bspcheck.py sym ffffffc00812a3c4 --map System.map
```

- 自动读日志里的 `Kernel Offset:`，KASLR 开着也能正确换算
- 有 `addr2line` 就给 `file:line`，没有就只给 `符号+偏移`（通常也够用）
- `addr2line` 找不到时，用 `ADDR2LINE=/path/to/aarch64-linux-gnu-addr2line` 指定
- **模块地址查不了**（模块符号本来就带在 oops 里了，不用查）

### config —— 检查"出问题时能不能查"

12 项和可诊断性相关的选项：`KALLSYMS`、`DEBUG_INFO`、`IKCONFIG_PROC`、
`PSTORE*`、`MAGIC_SYSRQ`、`DYNAMIC_DEBUG`、`FRAME_POINTER`…
缺了不影响功能，但会让你在出问题时白跑好几趟。**按往返成本算，提前打开很便宜。**

---

## bspctl.py 命令一览

| 命令 | 干什么 | 需要硬件 | 需要配置 |
|---|---|---|---|
| `log` | 抓串口日志，落盘 + 写 meta | 是 | `[serial]` |
| `cmd` | 通过串口执行命令，带退出码 | 是 | `[serial]` |
| `ssh` | 在板子上执行命令 | 是 | `[board] ip/user` |
| `build` | 触发构建，流式落盘 + 提取错误 | 否 | `[build] cmd` |
| `flash` | 烧写镜像 | 是 | `[flash]` |
| `reset` | 复位 / 上下电 | 是 | `[reset]` |
| `up` | reset → log → 启动耗时 | 是 | 以上全部 |
| `smoke` | 板级冒烟测试 | 是 | `smoke.tsv`（可选） |
| `boottime` | 从日志提取阶段耗时 | 否 | 无 |
| `oops` | 从日志抽崩溃现场 | 否 | 无 |
| `diff` | 两份日志归一化比对 | 否 | 无 |

通用参数：`-c/--conf` 指定配置，`-q` 静音（不实时打印串口输出，适合让 AI 调用）。

---

## 常用姿势

### 抓启动日志

```bash
./board.sh log --until "login:" --timeout 90
./board.sh log --reset --timeout 120        # 先复位再抓
./board.sh log --stamp                       # 每行加相对时间戳（看卡在哪最直观）
```

产物：
- `logs/boot-<board>-<时间>.log` —— 原始日志
- 同名 `.meta.json` —— 抓取时长、行数、`until` 是否命中、重连次数

退出码恒为 0（只要串口没坏）。**有没有抓到东西看 meta 和末尾那行 `--- bspctl: OK/TIMEOUT ---`**。

### 在板子上执行命令

```bash
./board.sh cmd "cat /proc/device-tree/model" "dmesg | tail -20"
```

实现方式：发 `命令 ; rc=$? ; echo <哨兵> $rc`，读到哨兵行为止。
好处是**能拿到真实退出码**，比匹配 shell 提示符可靠得多。

这里有两个已经踩过的坑，代码里都处理了：
- **终端回显**：命令被原样回显时，回显行里如果含哨兵会误命中。所以哨兵写成
  `__BSPCTL_EOF_$(echo 12345)__`——回显行里是未展开的形式，永远匹配不上已展开的哨兵。
- **分片**：哨兵和它后面的 rc 可能被切成两个 chunk。所以只在收到**完整行**时才判断。

### 构建

```bash
./board.sh build
./board.sh build linux-rebuild        # 追加 target
```

日志落到 `logs/build/`，结束后自动统计错误行 / warning 行，并打印首批错误。
`[build] cmd` 走当前平台 shell —— Windows 上要跑 Linux 构建链就写
`cmd = wsl -e bash -lc "cd /mnt/f/bsp && make -C buildroot O=output myboard_defconfig all"`。

### 烧写

```bash
./board.sh flash --yes
```

两种模式：
- `mode = sdcard`：`dd` 写 SD 卡。**会检查设备是否是当前根设备、是否仍被挂载**，
  不通过直接拒绝。仅 Linux。
- `mode = cmd`：任意工具的逃生口，`{image}` 会被替换：
  ```ini
  cmd = rkdeveloptool wl 0 {image}     # Rockchip
  cmd = uuu -b emmc_all {image}        # NXP i.MX
  cmd = sunxi-fel -p spsr {image}      # Allwinner
  cmd = fastboot flash boot {image}
  ```

### 复位（做闭环的前提）

```bash
./board.sh reset
```

三种方式，按性价比排序：
1. **`uhubctl`**（推荐）：给板子供电的那个 USB HUB 口做掉电重启，十几块钱的 HUB 就能用。
   ```bash
   sudo apt install uhubctl && uhubctl      # 先看 hub 和端口
   ```
   ```ini
   [reset]
   mode = uhubctl
   hub = 1-1
   port = 2
   ```
2. **USB 继电器**：`mode = relay`，串口写十六进制字节。
3. **GPIO**：`mode = gpio`，比如树莓派 `gpioset gpiochip0 17=1; sleep 3; gpioset gpiochip0 17=0`。

**没有复位能力就没有真正的闭环**——每次改完要手动拔插，AI 就没法自己验证。

### 冒烟测试

`smoke.tsv`，一行一个用例，`名称|命令|期望`，期望前加 `!` 表示"不应出现"：

```
dtb-model  | cat /proc/device-tree/model                        | .+
kmsg-clean | dmesg \| grep -E "Unable to handle\|Kernel panic"  | !
eth0       | ip -4 addr show eth0 \| grep -c inet              | [1-9]
```

没有这个文件就用内置的 6 条默认检查。

---

## 日志分析三件套

```bash
./board.sh boottime logs/boot-xxx.log
./board.sh oops     logs/boot-xxx.log --context 60
./board.sh diff     logs/good.log logs/bad.log
```

**boottime**：按启动标记提取内核时间戳，算阶段耗时和最大瓶颈。
U-Boot 阶段没有内核时间戳，会标 `n/a` 或 `≥`，不要拿它当精确值。

**oops**：抽取 panic / oops / BUG / WARN / lockdep / RCU stall / OOM，
输出"崩溃现场摘要 + 关键寄存器与调用栈 + 上下文窗口"，**格式就是给模型吃的**。
直接 `> oops.txt` 然后连 vmlinux 一起发过去。

**diff**：先把 `[ 1.234567]`、`0x...`、裸十六进制地址统一成占位符，再比对。
这样好板/坏板、改前/改后的差异才浮出来，不会被时间戳和地址淹没。

---

## 接 MCP（让 AI 直接调）

```bash
export BSPCTL_CONF=/path/to/bsp.conf    # Windows: $env:BSPCTL_CONF = "F:\...\bsp.conf"
python bsp_mcp.py                       # stdio 传输
```

客户端配置：

```json
{
  "mcpServers": {
    "bsp": {
      "command": "python",
      "args": ["F:/deeseek/workspace/bsp/bsp_mcp.py"],
      "env": { "BSPCTL_CONF": "F:/deeseek/workspace/bsp/bsp.conf" }
    }
  }
}
```

暴露 12 个工具（`board_` 前缀）：`board_list_serial` / `board_capture_log` / `board_run` /
`board_ssh` / `board_boottime` / `board_triage_crash` / `board_diff_logs` / `board_build` /
`board_reset` / `board_flash` / `board_smoke` / `board_bringup_checklist`。

两个刻意的设计：
- **不返回完整日志**，只回"路径 + 尾部 120 行 + 结构化摘要"。一次 3MB 的启动日志
  贴进上下文，模型就废了。
- **`board_flash` 必须 `confirm=True`**，默认拒绝。防止模型"顺手"把你板子重烧一遍。

---

## 疑难

**打开串口失败 `PermissionError` / `Access denied`**
串口助手、minicom、screen、IDE 的串口监视器占着。全都关掉。

**串口有输出但全是乱码**
波特率不对（常见 115200 / 1500000）。或者地线没接，或者 TX/RX 反了。

**`log` 抓到 0 行**
`up` 里专门有这条检查。依次查：板子上电了吗 → 串口接的是调试口吗 →
波特率对吗 → TX/RX 交叉了吗 → 是否需要在串口上拉/下拉。

**日志有隔行空白**
早期版本会把 `\r\r\n` 处理成两个换行。现在的逻辑是攒够整行再规整，
chunk 边界劈开 `\r\n` 也不会出错。

**`cmd` 拿不到输出，一直 TIMEOUT**
板子还没起到 shell（还在 U-Boot 或 initramfs），或者 shell 不支持 `$(...)`
（极少见）。先 `log` 看看现在停在哪。

**Windows 上路径**
配置里的路径用正斜杠或双反斜杠都行；`[build] cmd` 是交给 `cmd.exe` 的，注意引号规则。
