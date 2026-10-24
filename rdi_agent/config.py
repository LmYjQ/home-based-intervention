"""全局配置。

所有可调项集中在这里：环境变量 -> Settings，代码里不再散落 os.getenv。
默认值全部指向"本地 Docker Compose 起一个 Postgres"的最小可跑配置。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 数据库 ----
    # 宿主端口是 **5433**，不是 5432 —— 5432 常被本机别的项目占着
    # （比如另一个 pgvector 容器），所以 compose 把 ganyu 映射到 5433。
    # 容器内部仍然是 5432。
    database_url: str = "postgresql://rdi:rdi@localhost:5433/rdi"

    # ---- LLM ----
    llm_base_url: str = "https://api.siliconflow.cn/v1"
    # 兼容旧 .env 的 SILICONFLOW_API_KEY / SILICONFLOW_BASE_URL
    llm_api_key: str = Field(
        default="", validation_alias=AliasChoices("llm_api_key", "siliconflow_api_key")
    )
    llm_base_url: str = Field(
        default="https://api.siliconflow.cn/v1",
        validation_alias=AliasChoices("llm_base_url", "siliconflow_base_url"),
    )
    llm_model: str = "Qwen/Qwen2.5-7B-Instruct"
    llm_timeout_s: float = 20.0
    llm_max_attempts: int = 3
    # live / replay / capture
    demo_mode: str = "live"
    replay_dir: Path = REPO_ROOT / "rdi_agent" / "fixtures" / "responses"

    # ---- Embedding / Rerank ----
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024
    rerank_model: str = "BAAI/bge-reranker-v2-m3"

    # ---- 功能开关 ----
    rag_enabled: bool = True
    memory_enabled: bool = True
    rerank_enabled: bool = True

    # 图里要不要用重排。
    #
    # **默认关**：重排要把书里的片段发给外部 API（bge-reranker-v2-m3）。
    # 2B 的评测是用户手动跑带重排的版本的，图是自动化链路 ——
    # 自动化权限分类器不读对话里的授权，所以这里默认不外发书稿内容。
    # 想看重排的效果时显式打开。
    rerank_in_graph: bool = False

    # 要不要为进证据包的片段生成中文摘要（写回 knowledge_chunks.summary_zh）。
    #
    # **默认关**：每次开都要多一次 LLM 调用（6–8 块合并成一次），
    # 而收益只是让证据面板更好看。摘要是**转述**不是原文引用，
    # 版权上可以给家长看（may_quote_to_user 仍然恒为 False）。
    summary_zh_enabled: bool = False

    # ---- 关键词检索后端 ----
    # postgres_fts（默认，Postgres 内置 tsvector + ts_rank_cd）
    # paradedb_bm25（可选扩展，启用前需确认目标 Postgres 装得上 pg_search）
    keyword_backend: str = "postgres_fts"

    # ---- 路径 ----
    documents_dir: Path = REPO_ROOT / "data" / "documents"
    index_dir: Path = REPO_ROOT / "data" / "index"
    # 指南的 OCR 结果（分页 Markdown）。入库源在这里 ——
    # 指南是扫描件，PDF 没有文字层，页码靠"一个文件一页"保留。
    guideline_md_dir: Path = REPO_ROOT / "data" / "ocr" / "guideline2026"

    @property
    def has_llm_key(self) -> bool:
        return bool(self.llm_api_key.strip())


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """测试用：清掉缓存的 Settings，让改过的环境变量生效。"""
    global _settings
    _settings = None
