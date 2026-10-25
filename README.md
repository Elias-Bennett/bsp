# AI × Linux BSP 工作台（人在环版）

面向 **ARM SoC / U-Boot + Kernel + Buildroot**。

**前提约束**：AI 碰不到板子。抓日志、烧写、复位、插线都只能人工完成。
所以这里的东西不是"让 AI 自动化上板"，而是 **把每次人工往返的信息密度压到最高**，
以及 **把尽可能多的判定搬到主机侧**。

---

## 一、AI 到底该站在哪

| 环节 | 谁能闭环 | 怎么做 |
|---|---|---|
| 编译 / 链接错误 | **AI 自己** | 自己改到自己编译过为止，不占往返 |
| DTS / binding / 静态分析 | **AI 自己** | `dtc` + `dtbs_check` + `sparse`，主机上就能验 |
| 日志分析 | **AI 自己**（拿到文件之后） | `oops` / `boottime` / `diff` 全是离线的 |
| 文档 / 寄存器表 / errata | **AI 自己** | 读得比人快一个数量级，错误靠页码抽查 |
| git 考古 / upstream 对照 | **AI 自己** | "这个 bug 上游哪个 commit 修的" |
| **采集什么证据** | **AI 设计，人执行** | ← 这是最大杠杆，见第二节 |
| 电气 / 时序 / 真实硬件行为 | 只能人 | 人做，AI 负责设计实验和解读结果 |
| 烧写 / 上电 / 插线 | 只能人 | 人做 |

一句话：**AI 不负责验证，AI 负责让需要验证的次数变少。**

---

## 二、核心指标不是"每轮多快"，是"需要几轮"

成本 ≈ 往返次数 × 单次往返成本。所以只有三条策略：

1. **一次采集带回全部证据。** 不要"你 dmesg 里有没有 X"、"那 /proc/interrupts 呢"——
   那是每次问话都烧掉一趟实验室。让 AI 产出采集方案，人跑一次，一次带全。
2. **能离线判定的绝不占一次往返。** 编译、schema、静态分析、寄存器表机械核对，
   全部自己做穷尽，别拿板上试错当调试手段。
3. **每次往返都带对照组。** 好板/坏板、改前/改后各抓一份，回来做 A/B diff。
   一次观察只能描述现象，一次对照才能定位原因。

---

## 三、不需要板子，现在就能跑

```bash
cd bsp
python bspdump.py stat ../examples/dump-bad.txt          # 一眼看出坏在哪
python bspdump.py show ../examples/dump-bad.txt deferred # 再看细节
python bspdump.py diff ../examples/dump-good.txt ../examples/dump-bad.txt --full

python bspcheck.py dtb ../examples/board-bad.dtb         # 上板前 DTB 结构体检
python bspcheck.py dtb ../examples/board-good.dtb         # 对照组：应该是干净的

python bspctl.py oops  ../examples/boot-bad.log          # 串口日志的崩溃分诊
python bspctl.py boottime ../examples/boot-good.log      # 启动阶段耗时
```

只用标准库，`pip install` 都不用。

---

## 四、上台架时：一次采集，带回一个文件

```bash
# 板子上跑（busybox sh 就行，全程只读）
sh collect.sh probe                       # 驱动 probe 失败
sh collect.sh crash                       # 崩溃
sh collect.sh boot                        # 起不来
sh collect.sh net / storage / i2c / power
sh collect.sh all -o /tmp/d.txt --no-stream   # 全量，之后 scp 出来
```

它会把内容同时打到串口并写入 `/tmp/bspdump-*.txt`，每段用
`===BSPDUMP-SECTION:<名>===` / `===BSPDUMP-END:<名> size=N===` 包起来。
**这个包裹格式是为串口设计的**：传输被截断、丢字都不怕，已经收到的段仍然完整可用。

带回来之后，主机侧随便分析，一次往返可以榨出很多轮的信息：

```bash
python bspdump.py stat d.txt                  # 信号摘要（先看这一屏）
python bspdump.py show d.txt regulator        # 只看某段
python bspdump.py show d.txt dmesg --grep "mmc|timeout"
python bspdump.py diff good.txt bad.txt       # A/B 对照
```

`stat` 会直接告诉你：有没有崩溃、几个 deferred probe、rootfs 是不是只读、
温度多高、dmesg 里的错误集中在哪个子系统——**一行都不用贴全量日志**。

---

## 五、目录

```
workspace/
├── AGENTS.md                  ← 给 AI 看的硬规则（先读这个，里面写了往返约束）
├── prompts/
│   ├── 01-bringup.md            起不来：怎么让 AI 设计一次性采集 + 批量实验
│   ├── 02-oops-triage.md        崩溃现场怎么问
│   └── 03-driver-review.md      驱动 review（纯离线，AI 闭环）
├── examples/                  ← 不需要硬件就能跑的样例
│   ├── boot-good.log / boot-bad.log     串口日志
│   ├── dump-good.txt / dump-bad.txt     采集包
│   └── board-good.dtb / board-bad.dtb   带 bug 的 DTB（用来试 bspcheck）
└── bsp/
    ├── collect.sh             ← 板端一次性采集（人跑）
    ├── bspdump.py             ← 采集包解析/比对（AI 跑，离线）
    ├── bspcheck.py            ← 上板前体检 + oops 符号化（AI 跑，离线）
    ├── bspctl.py              ← 串口/构建/烧写 + 日志分析（人跑或 AI 跑）
    ├── bsp_mcp.py             ← 可选：把 bspctl 暴露成 MCP 工具
    ├── board.sh               ← bash 包装
    ├── bsp.conf.example
    └── README.md              ← 工具详解
```

`bspctl.py` 里的串口和烧写功能仍然有用——它省的是**人的操作成本**（不用手敲
`screen`、不用记 `dd` 参数），但别指望它替你去验证。

---

## 六、30 天落地路径（修订版）

| 阶段 | 做什么 | 成功标准 |
|---|---|---|
| **第 1 天** | 跑通 `bspdump.py stat` 和 `bspctl.py oops`（用 examples/） | 看懂"信号摘要"这一屏 |
| **第 1 周** | 把 `collect.sh` 放到板子上跑通一次 `probe` 和一次 `crash` | 一次采集能带回来一个可解析的完整包 |
| **第 2 周** | 拿一个正在查的 bug，让 AI 只凭采集包给假设；记录它错在哪 | 建立"AI 的推断哪些可信"的边界 |
| **第 2~4 周** | 把编译/DTS/静态检查配成 AI 能自己跑的循环（`dtbs_check`、`sparse`、`-Werror`） | AI 能在不碰板子的情况下自己收敛掉大部分低级错误 |
| **第 2 月** | 建立 A/B 采集纪律：每次改动都带对照组 | 一次往返就能定位，而不是一次只描述一个现象 |
| **第 3 月** | 把历史 bug 做成案例库；`prompts/` 固化成团队流程 | 新人同类问题能自己查到 |

---

## 七、三条红线

1. **寄存器值 / 引脚号 / 时序参数，只信 datasheet 和实测。**
   AI 给的这类数字格式一定对、值可能错，且极难发现。
2. **保密。** 公司代码、未公开 datasheet、客户原理图不上公有云模型。
   `collect.sh` 采集的包里可能带序列号、MAC、内部 IP——外发前先过一遍。
3. **一切改动走 git + review。** 小步提交，每步有验证手段。

细节见 [`bsp/README.md`](bsp/README.md)，规则见 [`AGENTS.md`](AGENTS.md)。
