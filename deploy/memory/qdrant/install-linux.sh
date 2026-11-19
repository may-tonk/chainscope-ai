#!/bin/bash
# 用途：在已准备的服务目录部署 Qdrant；由用户在 Linux 终端以 sudo 显式执行。
# 仓库模板位于 deploy/memory/qdrant；Linux 服务安装位置沿用既有目录。
# 只调整 Docker 下载代理并部署本服务，启用现有 PostgreSQL 自动启动，不升级软件包。
# 输入是固定服务目录中的部署材料；输出是运行状态，失败返回非零码。
# 本脚本会影响整个 Docker 服务的下载代理，首次应用代理时重启 Docker；不是只读检查。
set -euo pipefail
umask 077

readonly service_dir=/home/hadoop/chainscope-services/qdrant
readonly proxy_dropin=/etc/systemd/system/docker.service.d/zz-chainscope-memory-proxy.conf
readonly image=qdrant/qdrant:v1.20.1@sha256:d18c8e5061be3eaea2f53fbee1f0bf0382dc59cbb9b7112e41ffcc937ec14220

if [[ $EUID -ne 0 || $# -ne 0 ]]; then
    printf 'Run: sudo bash /home/hadoop/chainscope-services/qdrant/install-linux.sh\n' >&2
    exit 1
fi
# 固定路径和 UID 是本环境部署合同；不接受任意目录，避免在错误位置创建或接管服务。
if [[ $(readlink -f "$(dirname "$0")") != "$service_dir" || $(id -u hadoop) != 1000 ]]; then
    printf 'Unexpected service directory or runtime UID; stop before changing services.\n' >&2
    exit 1
fi
cd "$service_dir"
for name in compose.yaml config.yaml zz-chainscope-memory-proxy.conf secrets/qdrant.env certificates/ca.crt certificates/server.crt certificates/server.key; do
    [[ -f "$name" ]] || { printf 'Missing prepared file: %s\n' "$name" >&2; exit 1; }
done
# 只允许一个安装器操作同一服务；退出时文件描述符关闭，内核自动释放锁。
exec 9>/run/lock/chainscope-memory-install.lock
flock -n 9 || { printf 'An installation is already running.\n' >&2; exit 1; }

# 先验证转发和容器归属，避免缺少前置条件时中断已有服务。
# 官方仓库对未登录请求返回 401 是正常挑战，也证明代理与 TLS 通路已经到达仓库。
status=$(curl -sS -o /dev/null --proxy http://127.0.0.1:17890 --connect-timeout 5 --max-time 20 -w '%{http_code}' https://registry-1.docker.io/v2/)
[[ "$status" == 401 || "$status" == 200 ]] || { printf 'Registry proxy is not ready.\n' >&2; exit 1; }
# 同名容器必须属于本 Compose 项目；不能用安装命令覆盖其他服务。
if docker container inspect chainscope-memory-qdrant >/dev/null 2>&1; then
    owner=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' chainscope-memory-qdrant)
    [[ "$owner" == chainscope-memory ]] || { printf 'Container name belongs to another deployment.\n' >&2; exit 1; }
fi
# --quiet 避免把 env_file 中已经展开的 API Key 打印到终端。
docker compose -f compose.yaml config --quiet

# systemd 的环境变更需要重启 Docker；记录原有运行容器并核对恢复情况。
# 旧代理 drop-in 仍保留，新文件按名称顺序覆盖其环境变量；不改 Redis 配置。
if ! cmp -s zz-chainscope-memory-proxy.conf "$proxy_dropin"; then
    mapfile -t original_containers < <(docker ps -q)
    [[ ! -f "$proxy_dropin" ]] || cp -p "$proxy_dropin" "$proxy_dropin.backup-$(date +%Y%m%d%H%M%S)"
    install -m 0644 zz-chainscope-memory-proxy.conf "$proxy_dropin"
    systemctl daemon-reload
    printf 'Applying Docker proxy; existing containers will briefly restart.\n'
    systemctl restart docker
    for container in "${original_containers[@]}"; do
        if [[ $(docker inspect -f '{{.State.Running}}' "$container") != true ]]; then
            docker start "$container" >/dev/null
        fi
    done
fi

# 固定摘要决定实际内容，避免同名镜像标签变化；数据目录仅创建，不清空已有数据。
docker pull "$image"
install -d -o 1000 -g 1000 -m 0700 storage snapshots
docker compose -f compose.yaml up -d --pull never

# 仅改变现有集群的启动策略；不更新 PostgreSQL 包，不重启或重建数据库。
readonly pg_start=/etc/postgresql/18/main/start.conf
if grep -Eq '^[[:space:]]*manual[[:space:]]*$' "$pg_start"; then
    # 只将手动启动改成自动，先保存原文件；不把 disabled 状态擅自改成自动。
    cp -p "$pg_start" "$service_dir/postgresql-start.conf.before-memory"
    sed -i -E 's/^[[:space:]]*manual[[:space:]]*$/auto/' "$pg_start"
    systemctl daemon-reload
fi
systemctl enable postgresql.service >/dev/null

printf 'Qdrant container state:\n'
docker inspect -f 'name={{.Name}} status={{.State.Status}} image={{.Image}}' chainscope-memory-qdrant
printf 'PostgreSQL remains: '
psql --version
printf 'Installation finished; the application-side authenticated verification follows.\n'
