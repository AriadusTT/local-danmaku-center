"""本地开发/运行入口：python run.py"""

import uvicorn

from app.config import settings

if __name__ == "__main__":
    # 注意：扫描任务状态与 SQLite 都在进程内，务必保持单 worker。
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
        workers=1,
    )
