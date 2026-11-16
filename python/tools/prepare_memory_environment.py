"""准备 Memory 服务的私有凭证、TLS 文件和安装材料，不自动执行管理员命令。

输入是仓库中的无秘密部署模板和当前 Windows 用户环境；输出是 AppData 下的部署材料。
所有秘密写到当前用户 AppData 的 ChainScope/memory，仓库只保存无秘密模板。
本工具不读取 DashScope 密钥、不创建业务记忆，也不修改现有 Redis 数据。
它只在 Windows 准备文件；上传到 Linux、设置目录 ACL 和运行 sudo 安装是独立步骤。
"""

import ipaddress
import json
import os
import secrets
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parents[2]


def write_private(path, data):
    """把字节 data 写入新路径 path，成功时无返回值，目标已存在时直接失败。

    独占创建避免重复执行时替换正在使用的私钥。chmod 是文件权限标记，
    Windows 的目录隔离仍由部署步骤设置 NTFS ACL，不能靠 chmod 代替。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as output:
        output.write(data)
    path.chmod(0o600)


def make_certificates(runtime):
    """在私有目录 runtime 生成 CA、服务器密钥与证书，成功时无返回值。

    已有服务器密钥时只检查证书是否配套存在并复用，不擅自轮换身份。
    发现材料不完整时停止，交由操作者检查；CA 私钥不在 Linux 上传目录内。
    """
    certs = runtime / "qdrant/certificates"
    private = runtime / "private"
    if (certs / "server.key").exists():
        for name in ("server.crt", "ca.crt"):
            if not (certs / name).is_file():
                raise ValueError(
                    "Incomplete existing TLS material; refusing to replace keys"
                )
        return
    # 证书时间使用 UTC，向前预留五分钟容忍时钟小幅偏差；不会关闭客户端时间校验。
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    ca_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "ChainScope Memory Local CA")]
    )
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        # 此 CA 只签发最终服务器证书，不允许继续签发下级 CA。
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [x509.NameAttribute(NameOID.COMMON_NAME, "chainscope-memory-qdrant")]
            )
        )
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        # SAN 决定客户端可校验的主机名，不表示这些地址已对外开放端口。
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    x509.IPAddress(ipaddress.ip_address("192.168.229.10")),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    # 签发私钥保存在 private，服务器运行材料在 qdrant；部署时只上传后者。
    write_private(
        private / "authority.key",
        ca_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    write_private(
        certs / "server.key",
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    write_private(certs / "ca.crt", ca_cert.public_bytes(serialization.Encoding.PEM))
    write_private(
        certs / "server.crt", server_cert.public_bytes(serialization.Encoding.PEM)
    )


def main():
    """读取当前用户目录，准备部署文件并输出状态 JSON，不输出任何凭证值。

    会写入私有运行目录，复用既有凭证，只刷新无秘密模板；不会连接数据库或 Docker。
    """
    runtime = Path(os.environ["LOCALAPPDATA"]) / "ChainScope/memory"
    runtime.mkdir(parents=True, exist_ok=True)
    make_certificates(runtime)
    connection = runtime / "connections.json"
    # 两种密钥服务于不同客户端：站点检索使用只读 Key，索引 Worker 使用读写 Key。
    # 重跑准备命令必须复用它们，否则已运行容器与客户端会使用不同的身份。
    if connection.exists():
        credentials = json.loads(connection.read_text(encoding="utf-8"))
    else:
        credentials = {
            "qdrant_url": "https://127.0.0.1:6333",
            "qdrant_ca_file": str(runtime / "qdrant/certificates/ca.crt"),
            "qdrant_write_key": secrets.token_urlsafe(40),
            "qdrant_read_key": secrets.token_urlsafe(40),
        }
        write_private(connection, json.dumps(credentials, ensure_ascii=True).encode())
    env_file = runtime / "qdrant/secrets/qdrant.env"
    # 仅把 Qdrant 自身的凭证交给容器，DashScope 和数据库密码不进入此文件。
    if not env_file.exists():
        data = (
            "QDRANT__SERVICE__API_KEY=" + credentials["qdrant_write_key"] + "\n"
            "QDRANT__SERVICE__READ_ONLY_API_KEY="
            + credentials["qdrant_read_key"]
            + "\n"
        )
        write_private(env_file, data.encode())
    for name in (
        "compose.yaml",
        "config.yaml",
        "install-linux.sh",
        "zz-chainscope-memory-proxy.conf",
    ):
        # 统一 LF，避免 PowerShell/SSH 与 Linux shell 的换行差异。
        content = (ROOT / "deploy/qdrant" / name).read_text(encoding="utf-8")
        (runtime / "qdrant" / name).write_text(content, encoding="utf-8", newline="\n")
    shutil.copyfile(
        ROOT / "python/tools/run_memory_tunnel.py", runtime / "run_memory_tunnel.py"
    )
    # 只打印路径和布尔状态；私钥和连接字典不能整体进入日志或提交记录。
    print(
        json.dumps(
            {
                "prepared": True,
                "runtime_directory": str(runtime),
                "qdrant_version": "1.20.1",
                "tls_prepared": True,
                "credentials_prepared": True,
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()
