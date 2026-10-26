"""日志配置。

**为什么以前没有日志**：整个项目一条 logger 都没有 ——
LLM 调用失败时只有一句 ``LLMError`` 冒到 API，
"第几次尝试、finish_reason 是什么、模型到底吐了什么"全丢了。
线上只能看到一句 ``LLMError: sufficiency 加到 6750 tokens 仍被截断``，
连"是截断还是 schema 不合规"都分不出来。

现在有了：每次 LLM 重试写一条 warning，
最终失败写一条 error（含原始输出），都落 ``logs/rdi.log``，
同时打到 stderr（uvicorn 的 console 也能看到）。
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from rdi_agent.config import REPO_ROOT

LOG_DIR = REPO_ROOT / "logs"
LOG_FILE = LOG_DIR / "rdi.log"

# 20MB × 3 份。LLM 失败时会把原始输出整段写进去，
# 不滚动的话几天就能把磁盘吃满。
_MAX_BYTES = 20 * 1024 * 1024
_BACKUP_COUNT = 3

_configured = False


def setup_logging(*, level: int = logging.INFO) -> None:
    """配置根 logger。重复调用只会生效一次。

    幂等是有必要的：uvicorn ``--reload`` 会反复重新导入模块，
    每加一个 handler 就会多写一份同样的日志。
    """
    global _configured
    if _configured:
        return
    _configured = True

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("rdi")
    root.setLevel(level)
    root.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # stderr：和 uvicorn 的输出混在一起，本地开发时一眼能看到。
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)


def get_logger(name: str) -> logging.Logger:
    """拿一个 logger。名字统一以 ``rdi.`` 开头。"""
    if not name.startswith("rdi"):
        name = f"rdi.{name}"
    return logging.getLogger(name)
