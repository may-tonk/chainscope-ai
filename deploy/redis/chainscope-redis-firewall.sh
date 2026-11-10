#!/bin/bash
# 用途：限制经 VMware 地址发布的 Redis 端口，只允许 Windows VMware 地址访问。
# 安装到 /usr/local/sbin/chainscope-redis-firewall.sh，由 root 执行，并在 Docker 启动前重放。
# 仅管理本脚本的专属链与三个跳转；不清空 INPUT、DOCKER-USER 或其他服务的规则。
# 正常执行会先进行内核规则校验，再以单个 filter 表事务提交；失败时保留原有规则。
set -euo pipefail
umask 077

if [[ $# -ne 0 || $EUID -ne 0 ]]; then
    printf 'Run this firewall manager as root without arguments.\n' >&2
    exit 1
fi

readonly iptables=/usr/sbin/iptables
readonly restore=/usr/sbin/iptables-restore
readonly guard=CS-REDIS-SOURCE
readonly managed=chainscope-redis-source-v1
readonly server_ip=192.168.229.10
readonly windows_ip=192.168.229.1

# 同一脚本的手动执行和 Docker 启动检查不得并发修改专属链。
# 锁文件放在 /run，系统重启后自动清除；它不是需要归档的项目文件。
exec 9>/run/lock/chainscope-redis-firewall.lock
/usr/bin/flock -x 9

# Docker 尚未启动时可能没有 DOCKER-USER；只在不存在时声明，避免碰已有链内容。
rules='*filter'
if ! "$iptables" -w 5 -S DOCKER-USER >/dev/null 2>&1; then
    rules+=$'\n:DOCKER-USER - [0:0]'
fi

# 已存在的同名链必须带有本工具的管理标记，否则拒绝接管。
if "$iptables" -w 5 -S "$guard" >/dev/null 2>&1; then
    existing_rules=$("$iptables" -w 5 -S "$guard")
    if ! /usr/bin/grep -Fq -- "$managed" <<< "$existing_rules"; then
        printf 'Refusing to replace an unrecognized chain: %s\n' "$guard" >&2
        exit 1
    fi
else
    rules+=$'\n:'"$guard"' - [0:0]'
fi

# 只在事务内重建自己的链，避免先清空再逐条追加造成短暂的放行窗口。
# RETURN 表示仍需通过 Docker/主机后续规则，不使用 ACCEPT 绕过它们。
rules+=$'\n-F '"$guard"
rules+=$'\n-A '"$guard"' -s '"$windows_ip"'/32 -m comment --comment '"$managed"' -j RETURN'
rules+=$'\n-A '"$guard"' -m comment --comment chainscope-redis-deny-other-sources-v1 -j DROP'

# Docker 已做 DNAT：两个实例在容器内都是 6379，必须按连接原始目标端口分别匹配。
# 仅检查 ORIGINAL 方向，避免把容器发回 Windows 的响应包当作其他来源拦截。
for port in 6379 6380; do
    jump=(-p tcp -m conntrack --ctdir ORIGINAL --ctorigdst "$server_ip" --ctorigdstport "$port" -m comment --comment "$managed" -j "$guard")
    if ! "$iptables" -w 5 -C DOCKER-USER "${jump[@]}" >/dev/null 2>&1; then
        rules+=$'\n-I DOCKER-USER 1 -p tcp -m conntrack --ctdir ORIGINAL --ctorigdst '"$server_ip"' --ctorigdstport '"$port"' -m comment --comment '"$managed"' -j '"$guard"
    fi
done

# Docker 启用了 userland proxy，同时保护直接到宿主机监听端口的 INPUT 路径。
# 排除 lo，保留 Linux 本机运维访问；其余地址和端口的流量不进入本链。
input_jump=(-p tcp '!' -i lo -d "$server_ip/32" -m multiport --dports 6379,6380 -m comment --comment "$managed" -j "$guard")
if ! "$iptables" -w 5 -C INPUT "${input_jump[@]}" >/dev/null 2>&1; then
    rules+=$'\n-I INPUT 1 -p tcp ! -i lo -d '"$server_ip"'/32 -m multiport --dports 6379,6380 -m comment --comment '"$managed"' -j '"$guard"
fi
rules+=$'\nCOMMIT\n'

# --noflush 保留其他链；--test 只进行校验，第二次调用才正式提交同一份规则。
printf '%s' "$rules" | "$restore" --noflush --test
printf '%s' "$rules" | "$restore" --noflush
printf 'Redis source restriction applied: %s -> %s TCP 6379,6380; other external sources denied.\n' "$windows_ip" "$server_ip"
