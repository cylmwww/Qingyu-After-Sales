import asyncio
import sys

# psycopg 异步连接在 Windows 上不支持 ProactorEventLoop。
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
