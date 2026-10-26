"""在本机启动网站后端（开发环境）。

    python python/tools/run_webapp.py

只在 127.0.0.1:8000 监听，同一局域网的其他电脑访问不到。启动前需要：
- 虚拟机和 PostgreSQL 已经运行，开发库已迁移到最新版本；
- .env 里已填好 WEBAPP_DB_*，以及用 generate_webapp_keys.py 生成的 4 把 AUTH_*_KEY。

启动后可以打开 http://127.0.0.1:8000/docs 查看并试用接口（只在开发环境提供）。
开发模式下，注册验证码会打印在这个窗口里。按 Ctrl+C 停止。
"""

import uvicorn


def main() -> None:
    """用 uvicorn 启动 webapp.app:create_app（工厂模式），只监听本机。"""
    uvicorn.run(
        "webapp.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=8000,
        # 不让 uvicorn 自己根据 X-Forwarded-For 改写来源 IP；
        # 是否相信这个头，由 api/dependencies.py 按 WEBAPP_TRUSTED_PROXIES 决定。
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
