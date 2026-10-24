"""轻语 API 的交互式终端对话客户端。

客户端分为三个模块：

- :mod:`app.cli.render`：输出格式与终端渲染
- :mod:`app.cli.client`：通过 HTTP 或本地引擎进行连续对话
- :mod:`app.cli.commands`：参数解析与 ``main`` 入口

使用 ``python -m app.cli`` 或 ``qingyu-new`` 命令启动。
"""

from app.cli.client import ChatCli, ChatFailed
from app.cli.commands import build_parser, main
from app.cli.render import format_meta, parse_sse_data_line

__all__ = [
    "ChatCli",
    "ChatFailed",
    "build_parser",
    "format_meta",
    "main",
    "parse_sse_data_line",
]
