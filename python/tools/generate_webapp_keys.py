"""生成网站后端的 4 把服务端密钥，打印出来，由你复制进项目根目录的 .env。

    python python/tools/generate_webapp_keys.py

每把都是 256 位（32 字节）安全随机数，以 base64url 文本表示。
这个脚本不读取、也不修改 .env，只在屏幕上打印；打印出来的内容不要发到聊天、提交到 Git 或写进日志。

注意：如果 .env 里已经有密钥，替换它们会让所有已登录会话和未完成的注册失效
（相当于全体重新登录）。只有首次配置或需要换钥时才替换；换钥时同时把 AUTH_KEY_VERSION 加 1。
"""

import secrets

from webapp.core.keys import KEY_NAMES, MIN_KEY_BYTES


def main() -> None:
    for name in KEY_NAMES:
        print(f"{name}={secrets.token_urlsafe(MIN_KEY_BYTES)}")


if __name__ == "__main__":
    main()
