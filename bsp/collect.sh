#!/bin/sh
# collect.sh — 板端一次性诊断采集（POSIX sh / busybox 可用，全程只读）
#
# 目的：把"一问一答、一天跑三趟实验室"变成"跑一次脚本、带回一个文件"。
#       人类只负责执行和搬运，采集什么、为什么采，交给 AI 设计。
#
# 用法:
#   sh collect.sh [profile] [-o FILE] [--max BYTES] [--no-stream]
#                 [--i2c-scan] [--mount-debugfs]
#
# profile:
#   boot     启动相关（默认）
#   probe    驱动 probe 失败 / deferred probe
#   crash    oops / panic
#   net      网络不通
#   storage  存储异常
#   i2c      I2C 设备
#   power    电源 / 温度 / 调频
#   all      全部（慢，输出大，建议用 -o 存文件后 scp 出来）
#
# 输出格式:
#   每段用 ===BSPDUMP-SECTION:<名>=== 和 ===BSPDUMP-END:<名> size=N[ TRUNCATED]=== 包裹。
#   这个设计是为了串口：即使传输被截断、丢字，已经收到的段仍然完整可用，
#   bspdump.py 能据此解析出"哪些段拿到了、哪些没有"。

PROFILE=boot
OUT=""
MAX=20000
STREAM=1
I2C_SCAN=0
MOUNT_DBG=0

usage() {
    sed -n '3,25p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        -o|--out)        OUT=$2; shift 2 ;;
        --max)           MAX=$2; shift 2 ;;
        --no-stream)     STREAM=0; shift ;;
        --i2c-scan)      I2C_SCAN=1; shift ;;
        --mount-debugfs) MOUNT_DBG=1; shift ;;
        -h|--help|help)  usage; exit 0 ;;
        -*)              echo "未知参数: $1（用 --help）" >&2; exit 2 ;;
        *)               PROFILE=$1; shift ;;
    esac
done

has() { command -v "$1" >/dev/null 2>&1; }

if [ -z "$OUT" ]; then
    OUT="/tmp/bspdump-$(date +%Y%m%d-%H%M%S 2>/dev/null || echo dump).txt"
fi
: > "$OUT" 2>/dev/null || OUT=/dev/null

# 很多摘要只在 debugfs 里。默认不动板子状态，需要时显式加 --mount-debugfs
if [ "$MOUNT_DBG" -eq 1 ]; then
    mount -t debugfs none /sys/kernel/debug 2>/dev/null
fi

MANIFEST=""

out() {
    if [ "$STREAM" -eq 1 ]; then
        tee -a "$OUT"
    else
        cat >> "$OUT"
    fi
}

# dump_section <名字> <字节上限> <命令字符串>
dump_section() {
    _name=$1; _cap=$2; _cmd=$3
    _tmp="/tmp/.bspdump.$$.sec"
    if has timeout; then
        timeout 15 sh -c "$_cmd" > "$_tmp" 2>&1
    else
        sh -c "$_cmd" > "$_tmp" 2>&1
    fi
    _size=$(wc -c < "$_tmp" 2>/dev/null | tr -d ' ')
    [ -z "$_size" ] && _size=0
    _flag=""
    [ "$_size" -gt "$_cap" ] && _flag=" TRUNCATED"

    printf '===BSPDUMP-SECTION:%s===\n' "$_name" | out
    head -c "$_cap" "$_tmp" | out
    printf '\n===BSPDUMP-END:%s size=%s%s===\n' "$_name" "$_size" "$_flag" | out

    MANIFEST="${MANIFEST}${_name} ${_size}${_flag}
"
    rm -f "$_tmp"
}

# ---------------------------------------------------------------- 各 profile 的段

sections_common() {
    dump_section uname       2000  'uname -a; echo; date; echo; cat /proc/version'
    dump_section cmdline     2000  'cat /proc/cmdline'
    dump_section dmesg-err   8000  'dmesg 2>/dev/null | grep -iE "error|fail|denied|timeout|WARN|BUG:|panic|not found|no such|unable" | tail -n 300'
}

sections_boot() {
    dump_section model       2000  "cat /proc/device-tree/model 2>/dev/null; echo; cat /proc/device-tree/compatible 2>/dev/null | tr '\000' ' '; echo"
    dump_section uptime      1000  'cat /proc/uptime'
    dump_section meminfo     4000  'cat /proc/meminfo'
    dump_section interrupts  20000 'cat /proc/interrupts'
    dump_section iomem       8000  'cat /proc/iomem'
    dump_section modules     4000  'cat /proc/modules 2>/dev/null || lsmod'
    dump_section mounts      4000  'mount; echo; df -h'
    dump_section deferred    4000  'cat /sys/kernel/debug/devices_deferred 2>/dev/null || echo "(no deferred devices or debugfs not mounted)"'
    dump_section clk         20000 'cat /sys/kernel/debug/clk/clk_summary 2>/dev/null || echo "(clk_summary unavailable)"'
    dump_section regulator   20000 'cat /sys/kernel/debug/regulator/regulator_summary 2>/dev/null || echo "(regulator_summary unavailable)"'
    dump_section dmesg       60000 'dmesg 2>/dev/null'
}

sections_probe() {
    dump_section model       2000  "cat /proc/device-tree/model 2>/dev/null; echo"
    dump_section deferred    4000  'cat /sys/kernel/debug/devices_deferred 2>/dev/null || echo "(no deferred devices or debugfs not mounted)"'
    dump_section dmesg-probe 30000 'dmesg 2>/dev/null | grep -iE "probe|bind|driver|defer" | tail -n 400'
    dump_section clk         20000 'cat /sys/kernel/debug/clk/clk_summary 2>/dev/null || echo "(clk_summary unavailable)"'
    dump_section regulator   12000 'cat /sys/kernel/debug/regulator/regulator_summary 2>/dev/null || echo "(regulator_summary unavailable)"'
    dump_section pinctrl     20000 'for f in /sys/kernel/debug/pinctrl/*/pinmux-pins; do [ -f "$f" ] || continue; echo "--- $f"; cat "$f" 2>/dev/null; done'
    dump_section bus-devices 20000 'for b in /sys/bus/*/devices; do echo "--- $b"; ls "$b" 2>/dev/null | head -n 60; done'
    dump_section interrupts  12000 'cat /proc/interrupts'
    dump_section dmesg       40000 'dmesg 2>/dev/null'
}

sections_crash() {
    dump_section model       2000  "cat /proc/device-tree/model 2>/dev/null; echo"
    dump_section uptime      1000  'cat /proc/uptime'
    dump_section dmesg       120000 'dmesg 2>/dev/null'
    dump_section pstore-list 2000  'ls -l /sys/fs/pstore /var/lib/systemd/pstore 2>/dev/null'
    dump_section pstore      60000 'for f in /sys/fs/pstore/*; do [ -f "$f" ] || continue; echo "--- $f"; cat "$f" 2>/dev/null; done'
    dump_section lastkmsg    60000 '[ -r /proc/last_kmsg ] && cat /proc/last_kmsg || echo "(no /proc/last_kmsg)"'
    dump_section modules     4000  'cat /proc/modules 2>/dev/null'
}

sections_net() {
    dump_section net-if      8000  'ip -o addr 2>/dev/null; echo; ip -o link 2>/dev/null; echo; ip route 2>/dev/null'
    dump_section net-dev     4000  'cat /proc/net/dev'
    if has ethtool; then
        dump_section ethtool 20000 'for i in $(ls /sys/class/net 2>/dev/null); do echo "=== $i"; ethtool -i "$i" 2>&1; ethtool "$i" 2>&1; ethtool -S "$i" 2>&1 | head -n 40; done'
    else
        dump_section ethtool 1000  'echo "(ethtool not found)"'
    fi
    dump_section dmesg-net   20000 'dmesg 2>/dev/null | grep -iE "eth|phy|mdio|link|gmac|dwmac|net" | tail -n 200'
}

sections_storage() {
    dump_section partitions  4000  'cat /proc/partitions; echo; ls -l /sys/block'
    dump_section block-attr  12000 'for d in /sys/block/*; do echo "--- $d"; for a in size ro removable; do printf "%s=" "$a"; cat "$d/$a" 2>/dev/null; echo; done; done'
    dump_section mounts      4000  'mount; echo; df -h'
    dump_section dmesg-stor  20000 'dmesg 2>/dev/null | grep -iE "mmc|sdhci|ata|nvme|ufs|I/O error|EXT4-fs|timeout" | tail -n 300'
}

sections_i2c() {
    dump_section i2c-bus     4000  'i2cdetect -l 2>/dev/null || echo "(i2cdetect not found)"'
    dump_section i2c-devices 12000 'for d in /sys/bus/i2c/devices/*; do [ -e "$d/name" ] || continue; echo "$(basename "$d") $(cat "$d/name" 2>/dev/null)"; done'
    dump_section dmesg-i2c   16000 'dmesg 2>/dev/null | grep -iE "i2c|smbus|pmic" | tail -n 200'
    if [ "$I2C_SCAN" -eq 1 ]; then
        # 会真的访问总线：某些 PMIC / 传感器上可能触发异常，所以默认关闭
        dump_section i2c-scan 12000 'for b in $(i2cdetect -l 2>/dev/null | awk "{print \$1}"); do echo "--- bus $b"; i2cdetect -y -r "$b" 2>&1; done'
    else
        echo "### 跳过 i2c-scan（会真的访问总线；需要时加 --i2c-scan）"
    fi
}

sections_power() {
    dump_section regulator   20000 'cat /sys/kernel/debug/regulator/regulator_summary 2>/dev/null || echo "(regulator_summary unavailable)"'
    dump_section thermal     4000  'for z in /sys/class/thermal/thermal_zone*; do [ -d "$z" ] || continue; echo "$z type=$(cat "$z/type" 2>/dev/null) temp=$(cat "$z/temp" 2>/dev/null)"; done'
    dump_section cpufreq     4000  'for c in /sys/devices/system/cpu/cpu*; do [ -d "$c/cpufreq" ] || continue; echo "$c cur=$(cat "$c/cpufreq/scaling_cur_freq" 2>/dev/null) gov=$(cat "$c/cpufreq/scaling_governor" 2>/dev/null)"; done'
    dump_section opp         8000  'cat /sys/kernel/debug/opp/opp_summary 2>/dev/null || echo "(opp_summary unavailable)"'
    dump_section psupply     4000  'for p in /sys/class/power_supply/*; do [ -d "$p" ] || continue; echo "--- $p"; cat "$p/uevent" 2>/dev/null; done'
}

# ---------------------------------------------------------------- 分发

echo "### bspdump v1  profile=$PROFILE"
echo "### 只读采集，不修改板子状态"

sections_common
case "$PROFILE" in
    boot)    sections_boot ;;
    probe)   sections_probe ;;
    crash)   sections_crash ;;
    net)     sections_net ;;
    storage) sections_storage ;;
    i2c)     sections_i2c ;;
    power)   sections_power ;;
    all)
        sections_boot; sections_probe; sections_net
        sections_storage; sections_i2c; sections_power
        ;;
    *) echo "未知 profile: $PROFILE（用 --help）" >&2; exit 2 ;;
esac

# 校验和覆盖"载荷"（不含它自己），用来核对串口搬运有没有丢字符
# 从 stdin 读，避免 sha256sum 因为文件名里的反斜杠加转义前缀
SUM=""
if has sha256sum; then
    SUM="sha256=$(sha256sum < "$OUT" 2>/dev/null | cut -d' ' -f1)"
elif has md5sum; then
    SUM="md5=$(md5sum < "$OUT" 2>/dev/null | cut -d' ' -f1)"
fi

{
    echo ""
    echo "===BSPDUMP-MANIFEST==="
    printf '%s' "$MANIFEST"
    echo "===BSPDUMP-FOOTER==="
    echo "profile=$PROFILE file=$OUT"
    [ -n "$SUM" ] && echo "payload-$SUM"
    echo "===BSPDUMP-END-OF-DUMP==="
} | out
