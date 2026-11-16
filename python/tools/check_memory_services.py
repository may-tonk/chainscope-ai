"""显式初始化并验收 Memory 检索基础设施，不访问真实用户的记忆。

关键词、向量、融合和权限检查仅使用本工具随机创建的独立验收集合。
结构验证使用合成向量；真实 Embedding 的效果需要云端接口就绪后另行验收。
prepare 会创建开发/测试集合、索引和隔离样例；finish 会验证并删除隔离集合。
两步之间由操作者重启 Qdrant，本工具本身不重启 Docker，也不调用云端模型。
"""

import argparse
import json
import os
import ssl
from pathlib import Path
from uuid import uuid4

import httpx
import yaml
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path(os.environ["LOCALAPPDATA"]) / "ChainScope/memory"
PROBE = RUNTIME / "environment-probe.json"


def client(write=True):
    """按 write 选择读写或只读 Key，返回需由调用方关闭的 Qdrant 客户端。

    从仓库外读取凭证，先以相同 CA 和认证参数核对固定服务版本；只建立连接，不写数据。
    trust_env=False 避免回环请求被系统代理转发，服务器证书仍严格校验。
    """
    values = json.loads((RUNTIME / "connections.json").read_text(encoding="utf-8"))
    context = ssl.create_default_context(cafile=values["qdrant_ca_file"])
    key = values["qdrant_write_key" if write else "qdrant_read_key"]
    # SDK 1.20 的后台版本探测不传递自定义 CA/trust_env，私有 CA 会产生误报警。
    # 先以完全相同的 TLS 与认证参数核对固定版本，再关闭 SDK 的重复探测。
    expected = yaml.safe_load(
        (ROOT / "config/memory_services.yaml").read_text(encoding="utf-8")
    )["qdrant"]["server_version"]
    with httpx.Client(verify=context, trust_env=False, timeout=20) as transport:
        response = transport.get(values["qdrant_url"] + "/", headers={"api-key": key})
        response.raise_for_status()
        if response.json().get("version") != expected:
            raise ValueError(
                "Qdrant server does not match the pinned environment version"
            )
    return QdrantClient(
        url=values["qdrant_url"],
        api_key=key,
        verify=context,
        timeout=20,
        trust_env=False,
        check_compatibility=False,
    )


def ensure_collection(connection, name):
    """为指定集合 name 创建缺失的集合或索引，无返回值，会产生服务端写操作。

    既有向量维度或距离不匹配时停止，不能删除集合重建。已有索引不自动迁移，
    索引配置变更需要单独设计重建流程，不能把本函数当作完整 schema 迁移器。
    """
    if not connection.collection_exists(name):
        connection.create_collection(
            collection_name=name,
            vectors_config={
                "dense": models.VectorParams(
                    size=1024, distance=models.Distance.COSINE, on_disk=True
                )
            },
            hnsw_config=models.HnswConfigDiff(on_disk=True),
        )
    found = connection.get_collection(name)
    dense = found.config.params.vectors.get("dense")
    if dense is None or dense.size != 1024 or dense.distance != models.Distance.COSINE:
        raise ValueError("Existing collection has an incompatible vector contract")
    # user_id 的 tenant 标记用于检索优化，不替代后端身份验证或显式查询过滤。
    # 文本全文索引负责 BM25；地址等结构化字段不依赖分词或向量近似匹配。
    indexes = {
        "user_id": models.KeywordIndexParams(
            type=models.KeywordIndexType.KEYWORD, is_tenant=True
        ),
        "memory_id": models.PayloadSchemaType.KEYWORD,
        "memory_version": models.PayloadSchemaType.INTEGER,
        "status": models.PayloadSchemaType.KEYWORD,
        "chains": models.PayloadSchemaType.KEYWORD,
        "addresses": models.PayloadSchemaType.KEYWORD,
        "entity_keys": models.PayloadSchemaType.KEYWORD,
        "entity_tags": models.PayloadSchemaType.KEYWORD,
        "index_revision": models.PayloadSchemaType.KEYWORD,
        "memory_text": models.TextIndexParams(
            type=models.TextIndexType.TEXT,
            tokenizer=models.TokenizerType.MULTILINGUAL,
            lowercase=True,
            scoring=True,  # 开启 BM25 排序，仅创建文本过滤索引不足以支持词法排名。
        ),
    }
    for field, schema in indexes.items():
        if field not in found.payload_schema:
            connection.create_payload_index(
                name, field_name=field, field_schema=schema, wait=True
            )


def scope():
    """返回样例用户和 active 状态的固定过滤器，不产生外部副作用。

    这是环境验收范围；正式网站必须从可信登录上下文取得 user_id，不能复用固定样例用户。
    """
    return models.Filter(
        must=[
            models.FieldCondition(
                key="user_id", match=models.MatchValue(value="environment-user-a")
            ),
            models.FieldCondition(
                key="status", match=models.MatchValue(value="active")
            ),
        ]
    )


def queries(connection, name, expected):
    """只读检索样例集合，要求各路结果等于 expected 点，再返回布尔验收结果。

    使用人工构造的单位向量排除模型随机因素，验证的是索引、融合与过滤结构。
    同词条的其他用户和已删除点不得混入；本结果不代表真实语义召回质量。
    """
    dense = [1.0] + [0.0] * 1023
    keyword = connection.query_points(
        name,
        query=models.TextQuery(text="Uniswap V3"),
        using="memory_text",
        query_filter=scope(),
        limit=5,
    )
    semantic = connection.query_points(
        name, query=dense, using="dense", query_filter=scope(), limit=1
    )
    # 两个 prefetch 独立召回并各自过滤，主查询再融合；不是向量 Top-K 后才筛关键词。
    hybrid = connection.query_points(
        name,
        prefetch=[
            models.Prefetch(
                query=models.TextQuery(text="Uniswap V3"),
                using="memory_text",
                filter=scope(),
                limit=10,
            ),
            models.Prefetch(query=dense, using="dense", filter=scope(), limit=10),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        query_filter=scope(),
        limit=1,
    )
    # 复合实体键将链与地址绑定，避免两个独立数组的交叉匹配产生错误组合。
    entity = connection.scroll(
        name,
        scroll_filter=models.Filter(
            must=scope().must
            + [
                models.FieldCondition(
                    key="entity_keys",
                    match=models.MatchValue(
                        value="eip155:1:address:0x0000000000000000000000000000000000000001"
                    ),
                ),
            ]
        ),
        limit=10,
    )[0]
    for label, points in (
        ("keyword", keyword.points),
        ("dense", semantic.points),
        ("hybrid", hybrid.points),
        ("entity", entity),
    ):
        if [str(point.id) for point in points] != [expected]:
            raise ValueError(f"Unexpected {label} candidates")
    return {
        "keyword": True,
        "synthetic_dense": True,
        "hybrid_rrf": True,
        "entity_match": True,
        "user_and_status_filter": True,
    }


def prepare_probe(connection, settings):
    """输入客户端与非秘密配置，准备索引并返回重启前验证结果。

    会初始化持久集合结构，但样例仅写入随机隔离集合；本地标记供重启后的 finish 定位。
    遗留标记存在时拒绝覆盖，避免重跑后失去之前样例的清理依据。
    """
    for name in settings["qdrant"]["collections"].values():
        ensure_collection(connection, name)
    if PROBE.exists():
        raise ValueError(
            "An earlier restart probe is pending; finish it before creating another"
        )
    name = "chainscope_memory_envcheck_" + uuid4().hex
    expected = str(uuid4())
    # 先登记集合标识，过程中失败也能定位并清理本工具创建的集合。
    PROBE.write_text(
        json.dumps({"collection": name, "expected": expected}), encoding="utf-8"
    )
    ensure_collection(connection, name)
    # 前三条使用相同内容和向量，仅归属或状态不同，专门暴露遗漏过滤的错误。
    # 第四条使用正交向量作为无关背景，所有文本均为公开固定样例。
    points = []
    for position, (user, state, text) in enumerate(
        [
            ("environment-user-a", "active", "Uniswap V3 路由合约，用户偏好北京时间"),
            ("environment-user-b", "active", "Uniswap V3 路由合约，用户偏好北京时间"),
            (
                "environment-user-a",
                "deleted_by_user",
                "Uniswap V3 路由合约，用户偏好北京时间",
            ),
            ("environment-user-a", "active", "用户偏好详细解释 PostgreSQL 事务"),
        ]
    ):
        vector = [0.0] * 1024
        vector[0 if position < 3 else 1] = 1.0
        points.append(
            models.PointStruct(
                id=expected if position == 0 else str(uuid4()),
                vector={"dense": vector},
                payload={
                    "user_id": user,
                    "status": state,
                    "memory_id": str(uuid4()),
                    "memory_version": 1,
                    "memory_text": text,
                    "entity_keys": [
                        "eip155:1:address:0x0000000000000000000000000000000000000001"
                    ]
                    if position < 3
                    else [],
                    "index_revision": settings["qdrant"]["index_revision"],
                },
            )
        )
    # 等待写入应用后再验证，避免把尚未可读的异步写入误判为检索失败。
    connection.upsert(name, points=points, wait=True)
    result = queries(connection, name, expected)
    read_only = client(write=False)
    try:
        read_only.get_collection(name)
        try:
            # 故意在隔离样例上尝试写入，只有明确的 403 才算权限检查成功。
            # 网络错误或其他失败不能冒充“只读权限已生效”。
            read_only.set_payload(
                name, payload={"read_only_probe": True}, points=[expected], wait=True
            )
        except UnexpectedResponse as error:
            if error.status_code != 403:
                raise
            result["read_key_write_denied"] = True
        else:
            raise ValueError("Read-only API key was unexpectedly allowed to write")
    finally:
        read_only.close()
    result["restart_probe_prepared"] = True
    return result


def finish_probe(connection):
    """读取本地标记，只读复查重启后的原样例，再删除该隔离集合并返回结果。

    仅接受本工具专属前缀；服务端删除成功后才删除本地标记，失败时保留定位依据。
    容器重启动作由操作者在 prepare 与 finish 之间完成，本函数不自行判断重启时刻。
    """
    saved = json.loads(PROBE.read_text(encoding="utf-8"))
    name = saved["collection"]
    if not name.startswith("chainscope_memory_envcheck_"):
        raise ValueError("Unexpected probe collection; refusing cleanup")
    result = queries(connection, name, saved["expected"])
    connection.delete_collection(name)
    PROBE.unlink()
    result["restart_persistence"] = True
    result["probe_cleaned"] = True
    return result


def main():
    """解析 prepare/finish，执行对应有界验收并把非秘密结果保存到私有目录。

    失败用退出码和安全错误类型报告；HTTP 正文、请求头和凭证不进入输出。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "finish"))
    args = parser.parse_args()
    connection = None
    try:
        settings = yaml.safe_load(
            (ROOT / "config/memory_services.yaml").read_text(encoding="utf-8")
        )
        connection = client()
        result = (
            prepare_probe(connection, settings)
            if args.action == "prepare"
            else finish_probe(connection)
        )
        print(json.dumps(result))
        (RUNTIME / f"check-{args.action}.json").write_text(
            json.dumps(result), encoding="utf-8"
        )
    except Exception as error:  # noqa: BLE001 -- 运维工具边界，不输出 HTTP/认证原文。
        print(
            json.dumps(
                {
                    "success": False,
                    "error_type": type(error).__name__,
                    "status": getattr(error, "status_code", None),
                }
            )
        )
        raise SystemExit(1) from None
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    main()
