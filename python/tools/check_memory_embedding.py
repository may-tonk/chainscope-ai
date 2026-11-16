"""验证已配置的百炼向量接口，只发送固定环境样例，不输出密钥或完整向量。

新业务空间密钥使用控制台提供的 API Host；拒绝把云端凭证发给本机或未知域名。
成功后可显式保存非秘密 Base URL，供后续 Memory 客户端引用。
默认执行一次真实 Embedding 请求；--verify-qdrant 额外写入并清理隔离集合。
--save-endpoint 只保存接口地址，不改 API Key；此工具不承担正式记忆提取或用户授权。
"""

import argparse
import json
import math
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import yaml
from check_memory_services import client as qdrant_client
from check_memory_services import ensure_collection, scope
from dotenv import dotenv_values
from qdrant_client import models

ROOT = Path(__file__).resolve().parents[2]


def verify_retrieval(vectors, documents):
    """输入文档向量、末尾查询向量及文档，返回固定样例的三路检索结果。

    会创建随机隔离集合并写入样例，finally 中尝试清理；不会访问真实用户集合。
    这些断言只说明本组样例通过，不构成对全部问题的相关性或召回率承诺。
    """
    connection = qdrant_client()
    collection = "chainscope_memory_envcheck_embedding_" + uuid4().hex
    ids = [str(uuid4()) for _ in documents]
    try:
        ensure_collection(connection, collection)
        # 最后一个向量来自独立提问，只参与查询，避免用文档检索自身造成虚假的成功。
        connection.upsert(
            collection,
            points=[
                models.PointStruct(
                    id=point_id,
                    vector={"dense": vector},
                    payload={
                        "user_id": "environment-user-a",
                        "status": "active",
                        "memory_id": point_id,
                        "memory_version": 1,
                        "memory_text": document,
                    },
                )
                for point_id, vector, document in zip(
                    ids, vectors[:-1], documents, strict=True
                )
            ],
            wait=True,
        )
        dense = connection.query_points(
            collection, query=vectors[-1], using="dense", query_filter=scope(), limit=1
        )
        keyword = connection.query_points(
            collection,
            query=models.TextQuery(text="Uniswap V3"),
            using="memory_text",
            query_filter=scope(),
            limit=1,
        )
        # 关键词与 Dense 各自产生候选，再使用名次融合，避免直接相加不同量纲的分数。
        hybrid = connection.query_points(
            collection,
            prefetch=[
                models.Prefetch(
                    query=vectors[-1], using="dense", filter=scope(), limit=10
                ),
                models.Prefetch(
                    query=models.TextQuery(text="中国标准时间"),
                    using="memory_text",
                    filter=scope(),
                    limit=10,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            query_filter=scope(),
            limit=1,
        )
        if (
            [str(point.id) for point in dense.points] != [ids[0]]
            or [str(point.id) for point in keyword.points] != [ids[2]]
            or [str(point.id) for point in hybrid.points] != [ids[0]]
        ):
            raise ValueError(
                "Real embedding retrieval did not match the fixed environment examples"
            )
        return {
            "real_dense_retrieval": True,
            "keyword_retrieval": True,
            "real_hybrid_retrieval": True,
        }
    finally:
        # 集合名由本次 UUID 生成；只清理这一集合，开发和测试业务集合保持不动。
        if connection.collection_exists(collection):
            connection.delete_collection(collection)
        connection.close()


def endpoint(value):
    """把控制台 Host 或 Base URL 规范化为 HTTPS 兼容接口地址，不发起网络请求。

    拒绝用户信息、查询参数、未知域名和非标准端口，避免把认证头发给错误服务。
    仅补齐省略的协议与标准路径，不猜测业务空间 ID，也不自动切换地域。
    """
    value = value.strip().rstrip("/")
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    allowed = host.endswith(".maas.aliyuncs.com") or host in {
        "dashscope.aliyuncs.com",
        "dashscope-intl.aliyuncs.com",
        "dashscope-us.aliyuncs.com",
    }
    if (
        parsed.scheme != "https"
        or not allowed
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Use the official Alibaba Cloud API Host from your workspace")
    if not parsed.path or parsed.path == "/":
        value += "/compatible-mode/v1"
    elif parsed.path != "/compatible-mode/v1":
        raise ValueError("Expected the OpenAI-compatible /compatible-mode/v1 base path")
    return value


def main():
    """读取环境配置并执行一次真实向量请求，按参数选择写入接口地址及检索验收。

    操作系统同名变量覆盖 .env；仅输出模型、维度、用量和布尔结果。
    API 调用会产生供应商用量，失败时不自动换 Key、模型、地域或重试。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument("--save-endpoint", action="store_true")
    parser.add_argument("--verify-qdrant", action="store_true")
    args = parser.parse_args()
    try:
        # 不展开 ${...}，避免凭证中意外包含的字符被当作环境变量替换。
        values = {**dotenv_values(ROOT / ".env", interpolate=False), **os.environ}
        settings = yaml.safe_load(
            (ROOT / "config/memory_services.yaml").read_text(encoding="utf-8")
        )["embedding"]
        key = (values.get(settings["api_key_env"]) or "").strip()
        base = (
            args.base_url
            or values.get(settings["base_url_env"])
            or values.get("DASHSCOPE_API_HOST")
        )
        if not key or not base:
            print(
                json.dumps(
                    {"success": False, "missing": "API Host" if not base else "API key"}
                )
            )
            raise SystemExit(2)
        base = endpoint(base)
        documents = [
            "用户默认使用北京时间（中国标准时间，UTC+8）。",
            "用户偏好使用中文回答。",
            "Uniswap V3 is a decentralized exchange protocol on Ethereum.",
        ]
        # 三条文档加一条独立问题在一个请求内向量化，样例不包含用户真实对话。
        texts = documents + ["后续时间请按中国标准时间展示。"]
        with httpx.Client(
            timeout=settings["timeout_seconds"], trust_env=False
        ) as connection:
            response = connection.post(
                base + "/embeddings",
                headers={"Authorization": "Bearer " + key},
                json={
                    "model": settings["model"],
                    "input": texts,
                    "dimensions": settings["dimensions"],
                    "encoding_format": "float",
                },
            )
        data = response.json()
        if response.status_code != 200:
            error = data.get("error") or {}
            print(
                json.dumps(
                    {
                        "success": False,
                        "status": response.status_code,
                        "error_code": error.get("code")
                        if isinstance(error, dict)
                        else None,
                    }
                )
            )
            raise SystemExit(1)
        # 按服务端 index 恢复输入顺序，再检查数量、维度和有限数值，不能只看 HTTP 200。
        vectors = [
            item["embedding"]
            for item in sorted(data["data"], key=lambda item: item["index"])
        ]
        if len(vectors) != len(texts) or any(
            len(vector) != settings["dimensions"]
            or not all(
                isinstance(value, (int, float)) and math.isfinite(value)
                for value in vector
            )
            for vector in vectors
        ):
            raise ValueError("Unexpected embedding response contract")
        if args.save_endpoint:
            # 向量返回合同已验证成功，才允许保存 URL；后续 Qdrant 验收独立进行。
            # 仅更新 URL 这一非秘密设置，不回显或替换 API Key 和其他配置。
            path = ROOT / ".env"
            content = path.read_text(encoding="utf-8")
            assignment = settings["base_url_env"] + "=" + base
            pattern = rf"(?m)^{re.escape(settings['base_url_env'])}=.*$"
            content = (
                re.sub(pattern, assignment, content)
                if re.search(pattern, content)
                else content.rstrip("\n") + "\n" + assignment + "\n"
            )
            path.write_text(content, encoding="utf-8", newline="\n")
        result = {
            "success": True,
            "model": settings["model"],
            "dimensions": settings["dimensions"],
            "vectors_checked": len(vectors),
            "usage": data.get("usage"),
            "endpoint_saved": args.save_endpoint,
        }
        if args.verify_qdrant:
            result.update(verify_retrieval(vectors, documents))
        print(json.dumps(result))
        runtime = Path(os.environ["LOCALAPPDATA"]) / "ChainScope/memory"
        (runtime / "check-embedding.json").write_text(
            json.dumps(result), encoding="utf-8"
        )
    except Exception as error:  # noqa: BLE001 -- 不回显 HTTP 认证错误的原文。
        print(json.dumps({"success": False, "error_type": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
