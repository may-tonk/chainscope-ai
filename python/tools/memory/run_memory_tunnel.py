"""维持 ChainScope 开发机与 Linux 之间的 SSH 转发，供 pythonw 无窗口启动。

只使用既有 SSH 身份文件；私钥由 OpenSSH 读取，本工具不读取或记录其内容。
日志有容量上限；虚拟机或网络恢复后自动重连。
Windows 登录启动项运行本文件在私有运行目录中的副本，修改仓库源文件不会热更新已运行进程。
本进程只提供开发连接，不能代替 Linux 服务本身的健康检查或生产网络部署。
仓库源文件统一放在 python/tools/memory，私有运行副本继续使用原有路径。
"""

import logging
import msvcrt
import os
import subprocess
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


def main():
    """从 Windows 环境定位 SSH 材料，持续运行转发子进程，直到进程被停止。

    无业务返回值；会写锁文件和轮转日志、占用两个回环端口并在断线后重连。
    用操作系统文件锁防止登录启动项与手动启动产生重复实例，进程退出后锁自动释放。
    """
    runtime = Path(os.environ["LOCALAPPDATA"]) / "ChainScope/memory"
    runtime.mkdir(parents=True, exist_ok=True)
    # 锁的意义是持有打开的文件句柄，不以文件是否存在判断进程存活。
    with (runtime / "tunnel.lock").open("a+b") as lock:
        lock.seek(0)
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return
        logger = logging.getLogger("memory-tunnel")
        logger.setLevel(logging.INFO)
        # 最多保留当前日志和两个轮转文件，避免虚拟机长期离线时无限累积重连错误。
        handler = RotatingFileHandler(
            runtime / "tunnel-service.log",
            maxBytes=2**20,
            backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        key = Path(os.environ["USERPROFILE"]) / ".ssh/id_ed25519_hadoop_lab_auto"
        known_hosts = Path("D:/Linux2/vmware_known_hosts")
        if not key.is_file() or not known_hosts.is_file():
            logger.error("Verified SSH connection files are missing")
            return
        # StrictHostKeyChecking 固定服务端身份；BatchMode 避免隐藏窗口中等待密码输入。
        # ServerAlive 发现失联后退出，由下方循环重建连接；不会修改 known_hosts。
        command = [
            str(Path(os.environ["WINDIR"]) / "System32/OpenSSH/ssh.exe"),
            "-N",
            "-T",
            "-i",
            str(key),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            # 本机 6333 只转到 Linux 的回环 Qdrant，不为局域网其他机器代理数据库。
            "-L",
            "127.0.0.1:6333:127.0.0.1:6333",
            # Linux 回环代理复用 Windows 已有代理，保持原代理不对 LAN 开放。
            "-R",
            "127.0.0.1:17890:127.0.0.1:7890",
            "hadoop@192.168.229.10",
        ]
        while True:
            # pythonw 与 CREATE_NO_WINDOW 配合，避免每次重连弹出控制台窗口。
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            logger.info("SSH forwarding process started pid=%s", process.pid)
            try:
                for line in process.stdout:
                    logger.warning("%s", line.strip())
                logger.warning("SSH exited code=%s; reconnecting", process.wait())
            finally:
                # 回收当前子进程及管道后才重连，避免留下仍占用转发端口的旧实例。
                process.stdout.close()
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
            time.sleep(5)


if __name__ == "__main__":
    main()
