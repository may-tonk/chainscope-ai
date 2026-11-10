"""研究运行公开错误；不把供应商异常、密钥和原始模型输出带到用户界面。"""


class ResearchRuntimeError(Exception):
    """带稳定错误码的安全异常；内部原因需要另行设计受保护的日志通道。"""

    def __init__(self, code: str, public_message: str):
        """输入安全码与公开消息，构造可发送给调用方的异常。"""
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message
