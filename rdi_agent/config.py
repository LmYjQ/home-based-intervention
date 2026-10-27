"""全局配置。

所有可调项集中在这里：环境变量 -> Settings，代码里不再散落 os.getenv。
默认值全部指向"本地 Docker Compose 起一个 Postgres"的最小可跑配置。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
    llm_max_attempts: int = 2
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

    # ---- 追问轮数上限（两处，分开配）----
    #
    # **两个地方是两条独立的追问链**：
    #
    # 1. ``followup_max_rounds`` —— **新会话开始时**的追问。
    #    家长第一次描述孩子的情况，系统判断信息够不够，够就出方案。
    #    这一轮追问最"值钱"（问对了，后面方案直接能用），也最烦
    #    （家长还没进入状态）。默认 1。
    #
    # 2. ``feedback_followup_max_rounds`` —— **选了方案卡、录完反馈之后**
    #    的追问。家长已经执行过了，这时候追问"你做的时候具体怎么样"
    #    是为了让下一步建议更准。默认 1。
    #
    # **为什么分开**：两个场景家长承受的耐心不一样 ——
    # 开始时他对系统还不熟，反馈时他已经跑完一轮了。
    # 用一个常量就必然有一头不合适。
    #
    # 调大之后风险不变：重复提问靠 ``asked_qa_pairs`` 透传防住，
    # 不靠轮数（见计划 §8.6.1）。
    followup_max_rounds: int = 1
    feedback_followup_max_rounds: int = 1

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

# 运行时覆盖（前端改的）。
#
# **为什么要有这一层**：模型名和超时是最常被临时改的两项 ——
# provider 排队时把超时调短试试、换个模型对比一下效果。
# 改 ``.env`` 要重启服务，改代码更糟。
#
# **只覆盖白名单里的字段**：不能拿它改数据库地址之类的东西。
_LLM_OVERRIDE_FIELDS = ("llm_model", "llm_timeout_s")

# 覆盖值。没被覆盖的字段是 None。
_llm_overrides: dict[str, Any] = {}


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        if _llm_overrides:
            _settings = _settings.model_copy(update=dict(_llm_overrides))
    return _settings


def _reset_client() -> None:
    """丢掉缓存的 LLM client。

    **为什么要重置**：client 是按超时缓存的（``llm._CLIENTS``），
    超时变了还拿旧 client，等于没改。

    **这里 import 而不是顶层 import**：``llm`` 顶层 import ``config``，
    反过来顶层 import 会成循环。函数内 import 时模块已经加载完，安全。
    """
    from rdi_agent.llm import reset_client

    reset_client()


def set_llm_overrides(
    *, llm_model: str | None = None, llm_timeout_s: float | None = None
) -> dict[str, Any]:
    """改模型名和超时（前端可调），返回生效后的值。

    **传 None 表示"不改"** —— 前端只填了一个字段时，另一个要保持原样。

    **改完要 ``reset_client()``**：client 是按超时缓存的，
    超时变了还拿旧 client，等于没改。
    """
    if llm_model is not None:
        model = llm_model.strip()
        if not model:
            raise ValueError("模型名不能为空")
        _llm_overrides["llm_model"] = model
    if llm_timeout_s is not None:
        timeout = float(llm_timeout_s)
        if timeout <= 0:
            raise ValueError("超时必须大于 0")
        _llm_overrides["llm_timeout_s"] = timeout

    global _settings
    _settings = None  # 下一个 get_settings() 按新值重建
    _reset_client()
    s = get_settings()
    return {
        "llm_model": s.llm_model,
        "llm_timeout_s": s.llm_timeout_s,
        "llm_max_attempts": s.llm_max_attempts,
    }


def reset_llm_overrides() -> dict[str, Any]:
    """清掉运行时覆盖，回到 ``.env`` 说的那个值。

    **为什么要有这个**：覆盖是进程内存里的，重启服务就没了 ——
    但"我想临时改一下再改回来"是常态，没有这个按钮就得靠重启。
    """
    _llm_overrides.clear()
    global _settings
    _settings = None
    _reset_client()
    return llm_overrides()


def llm_overrides() -> dict[str, Any]:
    """当前生效的 LLM 配置（前端回显用）。"""
    s = get_settings()
    return {
        "llm_model": s.llm_model,
        "llm_timeout_s": s.llm_timeout_s,
        "llm_max_attempts": s.llm_max_attempts,
    }


def reset_settings() -> None:
    """测试用：清掉缓存的 Settings，让改过的环境变量生效。

    **连运行时覆盖一起清**：测试要的是"回到环境变量说的那个状态"，
    留着前一次调用改的值会让测试互相影响。
    """
    global _settings
    _settings = None
    _llm_overrides.clear()
