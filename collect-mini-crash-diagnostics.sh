#!/usr/bin/env bash
#
# Read-only diagnostics collector for the mini-server crash observed on
# 2026-07-30 around 15:59 UTC.
#
# Run:
#   sudo ./collect-mini-crash-diagnostics.sh
#

set -u
set -o pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this script as root: sudo $0" >&2
    exit 1
fi

SCRIPT_PATH=$(readlink -f -- "${BASH_SOURCE[0]}")
SCRIPT_DIR=$(dirname -- "${SCRIPT_PATH}")
REPORT_OWNER_UID=${SUDO_UID:-$(stat -c '%u' -- "${SCRIPT_PATH}")}
REPORT_OWNER_GID=${SUDO_GID:-$(stat -c '%g' -- "${SCRIPT_PATH}")}
STAMP=$(date -u +'%Y%m%dT%H%M%SZ')
REPORT_BASE="${SCRIPT_DIR}/.diagnostics"
REPORT_DIR="${REPORT_BASE}/mini-crash-${STAMP}"
LATEST_FILE="${REPORT_BASE}/LATEST"
CRASH_FROM='2026-07-30 15:30:00 UTC'
CRASH_TO='2026-07-30 16:20:00 UTC'
SYSSTAT_FILE='/var/log/sysstat/sa30'

mkdir -p -- "${REPORT_DIR}"
chmod 0700 -- "${REPORT_DIR}"

finish() {
    printf '%s\n' "${REPORT_DIR}" > "${LATEST_FILE}"
    chown -R "${REPORT_OWNER_UID}:${REPORT_OWNER_GID}" -- "${REPORT_DIR}" "${LATEST_FILE}" 2>/dev/null || true
    chmod -R u=rwX,go= -- "${REPORT_DIR}" 2>/dev/null || true
    chmod 0600 -- "${LATEST_FILE}" 2>/dev/null || true
}
trap finish EXIT

have() {
    command -v "$1" >/dev/null 2>&1
}

heading() {
    printf '\n===== %s =====\n' "$1"
}

{
    heading "COLLECTOR"
    printf 'collected_utc=%s\n' "$(date -u --iso-8601=seconds)"
    printf 'report_dir=%s\n' "${REPORT_DIR}"
    printf 'script=%s\n' "${SCRIPT_PATH}"
    printf 'crash_window=%s .. %s\n' "${CRASH_FROM}" "${CRASH_TO}"

    heading "HOST AND BOOTS"
    hostnamectl 2>&1 || true
    uname -a
    uptime
    who -b 2>&1 || true
    last -x -F 2>&1 | head -n 80 || true
    journalctl --list-boots --no-pager 2>&1 || true

    heading "CURRENT MEMORY, PRESSURE, LOAD"
    free -h
    swapon --show --bytes 2>&1 || true
    vmstat 1 5 2>&1 || true
    for pressure in /proc/pressure/cpu /proc/pressure/memory /proc/pressure/io; do
        printf '%s\n' "--- ${pressure}"
        cat -- "${pressure}" 2>&1 || true
    done

    heading "CURRENT FILESYSTEMS"
    df -hT
    df -hi
    findmnt 2>&1 || true

    heading "CURRENT TOP PROCESSES BY CPU"
    ps -eo pid,ppid,user,stat,%cpu,%mem,rss,vsz,etimes,comm --sort=-%cpu 2>&1 | head -n 40 || true

    heading "CURRENT TOP PROCESSES BY RSS"
    ps -eo pid,ppid,user,stat,%cpu,%mem,rss,vsz,etimes,comm --sort=-rss 2>&1 | head -n 40 || true

    heading "FAILED SYSTEM SERVICES"
    systemctl --failed --no-pager --plain 2>&1 || true

    heading "WATCHDOG AND PANIC SETTINGS"
    wdctl 2>&1 || true
    systemctl status watchdog.service systemd-oomd.service thermald.service --no-pager -l 2>&1 || true
    sysctl kernel.panic kernel.panic_on_oops kernel.softlockup_panic kernel.hardlockup_panic vm.panic_on_oom 2>&1 || true

    heading "JOURNAL STORAGE"
    journalctl --disk-usage 2>&1 || true
    du -sh /var/log/journal /run/log/journal 2>&1 || true
} > "${REPORT_DIR}/00-summary.txt" 2>&1

journalctl -k -b -1 --no-pager -o short-iso-precise \
    > "${REPORT_DIR}/10-previous-boot-kernel.txt" 2>&1 || true

journalctl -b -1 --no-pager -o short-iso-precise \
    --since "${CRASH_FROM}" --until "${CRASH_TO}" \
    > "${REPORT_DIR}/11-previous-boot-crash-window.txt" 2>&1 || true

journalctl -b -1 --no-pager -o short-iso-precise -p warning \
    > "${REPORT_DIR}/12-previous-boot-warnings.txt" 2>&1 || true

journalctl -b -1 --no-pager -o short-iso-precise \
    | grep -Ei \
        'oom|out of memory|killed process|memory cgroup|watchdog|soft lockup|hard lockup|hung task|blocked for more than|i/o error|buffer i/o|ext4-fs error|nvme|ata[0-9].*error|reset|thermal|overheat|thrott|mce|machine check|edac|segfault|panic|call trace|power|shutdown|reboot' \
    > "${REPORT_DIR}/13-previous-boot-signals.txt" 2>&1 || true

journalctl -k -b 0 --no-pager -o short-iso-precise \
    > "${REPORT_DIR}/14-current-boot-kernel.txt" 2>&1 || true

{
    heading "CURRENT DMESG"
    dmesg -T 2>&1 || true

    heading "PSTORE INDEX"
    find /sys/fs/pstore /var/lib/systemd/pstore -maxdepth 1 -type f \
        -printf '%p %s bytes\n' 2>&1 || true

    while IFS= read -r pstore_file; do
        heading "PSTORE FILE: ${pstore_file}"
        cat -- "${pstore_file}" 2>&1 || true
    done < <(
        find /sys/fs/pstore /var/lib/systemd/pstore -maxdepth 1 -type f \
            -readable -print 2>/dev/null
    )

    heading "COREDUMPS NEAR THE EVENT"
    if have coredumpctl; then
        coredumpctl list --no-pager --since "${CRASH_FROM}" --until "${CRASH_TO}" 2>&1 || true
    else
        echo "coredumpctl not installed"
    fi

    heading "APPORT CRASH FILES"
    find /var/crash -maxdepth 1 -type f -printf '%TY-%Tm-%Td %TH:%TM:%TS %s %p\n' 2>&1 || true
} > "${REPORT_DIR}/15-crash-artifacts.txt" 2>&1

{
    heading "LSBLK"
    lsblk -o NAME,KNAME,TYPE,SIZE,FSTYPE,FSVER,MOUNTPOINTS,MODEL,SERIAL,ROTA,TRAN 2>&1 || true

    heading "SDA SMART"
    if have smartctl; then
        timeout 60 smartctl -x /dev/sda 2>&1 || true
    else
        echo "smartctl not installed"
    fi

    heading "SDB SMART"
    if have smartctl; then
        timeout 60 smartctl -x /dev/sdb 2>&1 || true
    else
        echo "smartctl not installed"
    fi

    heading "MD RAID"
    cat /proc/mdstat 2>&1 || true
} > "${REPORT_DIR}/20-storage.txt" 2>&1

{
    heading "CPU"
    lscpu 2>&1 || true

    heading "SENSORS"
    if have sensors; then
        sensors 2>&1 || true
    else
        echo "sensors not installed"
    fi

    heading "THERMAL ZONES"
    for thermal_file in /sys/class/thermal/thermal_zone*/type /sys/class/thermal/thermal_zone*/temp; do
        [[ -r "${thermal_file}" ]] || continue
        printf '%s=%s\n' "${thermal_file}" "$(cat -- "${thermal_file}")"
    done

    heading "HWMON TEMPERATURES AND FANS"
    for hwmon_dir in /sys/class/hwmon/hwmon*; do
        printf '%s name=%s\n' "${hwmon_dir}" "$(cat "${hwmon_dir}/name" 2>/dev/null || true)"
        for sensor_file in \
            "${hwmon_dir}"/temp*_input \
            "${hwmon_dir}"/temp*_max \
            "${hwmon_dir}"/temp*_crit \
            "${hwmon_dir}"/fan*_input \
            "${hwmon_dir}"/fan*_min \
            "${hwmon_dir}"/fan*_max; do
            [[ -r "${sensor_file}" ]] || continue
            printf '%s=%s\n' "$(basename -- "${sensor_file}")" "$(cat -- "${sensor_file}")"
        done
    done

    heading "THERMAL THROTTLE COUNTERS"
    for cpu_dir in /sys/devices/system/cpu/cpu[0-9]*; do
        for counter_file in "${cpu_dir}"/thermal_throttle/*throttle_count; do
            [[ -r "${counter_file}" ]] || continue
            printf '%s=%s\n' "${counter_file}" "$(cat -- "${counter_file}")"
        done
    done

    heading "MACHINE CHECK AND EDAC"
    if have ras-mc-ctl; then ras-mc-ctl --errors 2>&1 || true; fi
    if have edac-util; then edac-util -v 2>&1 || true; fi
    if have mcelog; then mcelog --client 2>&1 || true; fi

    heading "POWER SUPPLIES"
    find /sys/class/power_supply -maxdepth 2 -type f \
        \( -name online -o -name status -o -name health -o -name capacity \) \
        -print -exec cat {} \; 2>&1 || true
} > "${REPORT_DIR}/30-hardware-thermal-power.txt" 2>&1

{
    heading "LINKS"
    ip -s link 2>&1 || true

    heading "ADDRESSES"
    ip address 2>&1 || true

    heading "ROUTES"
    ip route show table all 2>&1 || true

    heading "SOCKET SUMMARY"
    ss -s 2>&1 || true

    heading "LISTENING SOCKETS"
    ss -lntup 2>&1 || true

    heading "ETHERNET DRIVER"
    if have ethtool; then
        ethtool enp1s0f0 2>&1 || true
        ethtool -S enp1s0f0 2>&1 || true
    else
        echo "ethtool not installed"
    fi
} > "${REPORT_DIR}/40-network.txt" 2>&1

{
    heading "SYSSTAT CRASH WINDOW"
    if [[ -r "${SYSSTAT_FILE}" ]] && have sar; then
        for sar_mode in \
            '-r ALL' \
            '-S' \
            '-W' \
            '-B' \
            '-q ALL' \
            '-u ALL' \
            '-d -p' \
            '-n DEV,EDEV' \
            '-w' \
            '-v'; do
            heading "sar ${sar_mode}"
            # shellcheck disable=SC2086
            sar ${sar_mode} -f "${SYSSTAT_FILE}" -s 15:00:00 -e 16:20:00 2>&1 || true
        done
    else
        echo "Missing readable ${SYSSTAT_FILE} or sar is not installed"
    fi
} > "${REPORT_DIR}/50-sysstat.txt" 2>&1

{
    heading "SYSTEM UNITS"
    systemctl list-units --type=service --all --no-pager --plain 2>&1 || true

    heading "FAILED SYSTEM UNITS"
    systemctl --failed --no-pager --plain 2>&1 || true

    heading "BOOT PERFORMANCE"
    systemd-analyze 2>&1 || true
    systemd-analyze blame 2>&1 | head -n 100 || true

    heading "USER 1001 UNIT PROCESSES"
    systemd-cgls /user.slice/user-1001.slice 2>&1 || true
} > "${REPORT_DIR}/60-systemd.txt" 2>&1

finish
trap - EXIT

echo
echo "Diagnostics collected successfully."
echo "Report directory: ${REPORT_DIR}"
echo "Latest-path file: ${LATEST_FILE}"
