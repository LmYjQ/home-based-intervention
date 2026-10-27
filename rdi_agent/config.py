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
    # 超时要留够余量。
    #
    # **实测**：同一个 prompt、同一批参数，模型延迟在 2.8s ~ 55s 之间跳
    # （provider 侧的排队，不是我们的 prompt 大）。20s 的默认值碰上
    # 一次慢的就被打断；60s 也只是刚好压在慢的那条线上 ——
    # 连续三次慢就整轮 500 了。
    #
    # 给到 120s：单次慢调用能扛住，三次重试最坏 6 分钟，
    # 但换来的是"慢"而不是"挂"。宁可慢，不要把家长的问题判成模型挂了。
    llm_timeout_s: float = 120.0
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

    # ---- 长期记忆（阶段 3）----
    #
    # **抽取超时给得比主模型短**：记忆抽取是"锦上添花"，
    # 它慢不该拖住家长等回答。超时就这一轮不记，下一轮还有机会。
    memory_extract_timeout_s: float = 30.0
    memory_extract_max_attempts: int = 2
    # 一轮最多处理几条候选。多了说明模型在凑数。
    memory_max_candidates_per_turn: int = 6
    # 进 prompt 的**类目小结**条数（按与当前问题的相关度挑）。
    #
    # **为什么是小结不是短句**：短句只留在库里，
    # 参与方案制定的只有每个类目一条归纳后的小结。
    memory_context_max_summaries: int = 8
    # 进 prompt 的**系统推断**条数上限。推断是猜测，给得越少越好。
    memory_context_max_inferences: int = 4
    # prompt 里记忆部分的字符预算。中文 1 token ≈ 1–1.5 字，2000 字 ≈ 1.5k token。
    memory_context_max_chars: int = 2000
    # 合并（归纳）要不要开。关了的话记忆照常抽取，只是没有小结。
    memory_consolidation_enabled: bool = True

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
    #
    # postgres_fts（默认，Postgres 内置 tsvector + ts_rank_cd）
    # paradedb_bm25（**未实现**，见 ``rag/keyword_backends.py`` 里的说明：
    #   1. 本地 Postgres 装不上 pg_search 扩展，写了也验证不了；
    #   2. 2B 评测测出当前瓶颈是中文同义表述召不回，换 BM25 改善不了）
    #
    # 配了不存在的名字**或配了还没实现的后端**（paradedb_bm25），
    # 都会**静默退回** postgres_fts —— 检索链挂掉比"用默认后端跑"糟糕得多。
    # 退化是可见的：``/health`` 同时报 ``keyword_backend``（实际生效的）和
    # ``keyword_backend_configured``（配置里写的），两者不一致就是配错了。
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
