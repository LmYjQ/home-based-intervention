"""给中文指南入库做准备。

**为什么需要这一版迁移**：知识库从英文书换成中文指南之后，有三处
数据库层面的东西必须改，光改 Python 代码不够。

1. **``content_tsv`` 的分词配置**。原来��
   ``to_tsvector('english', content_normalized)`` —— 英文配置把一整段
   中文当成**一个词**（实测 ``to_tsvector('english', '孤独症儿童干预康复指南')``
   只出一个 token），FTS 关键词路对中文完全失效。

   改成 ``to_tsvector('simple', content_normalized)`` 也不够 ——
   ``simple`` 只是不做词干还原，中文仍会整段变一个 token。

   **真正的解法是把中文先分词**。入库时用 jieba 把内容切成空格分隔的词，
   再交给 ``to_tsvector('simple', ...)`` —— 空格是 ``simple`` 配置的天然
   分隔符，所以每个词都会成为独立的 lexeme。

   这就要求 ``content_normalized`` 里**存的就是分词后的文本**。

2. **``source_type`` 的取值**。原来只有 ``book``，现在是``guideline``。

3. **``application_label`` 的取值**。``book_summary`` 这个名字对着一份
   指南就说不通了，改成 ``source_summary``（"这份建议有知识库原文依据"）。
   存量数据要一起改，不然老卡读不出来。

**存量数据迁移**：``plans.plan_json`` 里嵌着卡片 JSON，
``evidence_bundles.bundle_json`` 里嵌着证据 JSON ——
都要把 ``book_summary`` → ``source_summary``、``"book"`` → ``"guideline"`` 一起替换。

用 ``jsonb`` 递归替换而不是正则刷字符串：正则会连
``claim_supported`` 里的正文一起改掉（"这本书说…" → "这份指南说…" 是对的，
但"书里提到 book" 这种示例文本也会被误改）。递归替换只动结构化的字段值。
"""

from __future__ import annotations

import json

from alembic import op

revision: str = "004_chinese_guideline_source"
down_revision: str | None = "003_evidence_bundle_unique"


def _replace_in_jsonb(doc: dict) -> dict:
    """递归替换 JSON 里的两个字符串取值。

    只改**字段值等于** ``book_summary`` / ``book`` 的地方 ——
    嵌在句子里的"书"字一律不动。
    """
    if isinstance(doc, dict):
        out = {}
        for key, value in doc.items():
            if key == "application_label" and value == "book_summary":
                out[key] = "source_summary"
            elif key == "source_type" and value == "book":
                out[key] = "guideline"
            else:
                out[key] = _replace_in_jsonb(value)
        return out
    if isinstance(doc, list):
        return [_replace_in_jsonb(item) for item in doc]
    return doc


# ---- 存量 JSON 的取值迁移 ----
#
# ``plans.plan_json`` 存的是**一张卡**（不是数组）——
# ``application_label`` 在卡对象上，``source_citations`` 是卡里的数组。
# ``evidence_bundles.bundle_json`` 的 ``evidence`` 是数组。
#
# 三个取值都要改：
#   application_label: book_summary -> source_summary
#   source_type:      book         -> guideline
#
# 用 ``jsonb_set`` 精确改字段，不做正则刷字符串 ——
# 正则会连 ``claim_supported`` 里的正文一起改掉。

_PLAN_APPLICATION_LABEL = """
    UPDATE plans
    SET plan_json = jsonb_set(
            plan_json,
            '{application_label}',
            to_jsonb('source_summary'::text),
            true
        ),
        application_label = 'source_summary'
    WHERE plan_json->>'application_label' = 'book_summary'
"""

_PLAN_SOURCE_TYPE = """
    UPDATE plans
    SET plan_json = jsonb_set(
            plan_json,
            '{source_citations}',
            (
                SELECT COALESCE(jsonb_agg(
                    CASE
                        WHEN elem->>'source_type' = 'book'
                        THEN jsonb_set(elem, '{source_type}', to_jsonb('guideline'::text), true)
                        ELSE elem
                    END
                ), '[]'::jsonb)
                FROM jsonb_array_elements(
                    COALESCE(plan_json->'source_citations', '[]'::jsonb)
                ) AS elem
            ),
            true
        )
    WHERE jsonb_typeof(plan_json->'source_citations') = 'array'
      AND jsonb_array_length(plan_json->'source_citations') > 0
"""

_BUNDLE_SOURCE_TYPE = """
    UPDATE evidence_bundles
    SET bundle_json = jsonb_set(
            bundle_json,
            '{evidence}',
            (
                SELECT COALESCE(jsonb_agg(
                    CASE
                        WHEN elem->>'source_type' = 'book'
                        THEN jsonb_set(elem, '{source_type}', to_jsonb('guideline'::text), true)
                        ELSE elem
                    END
                ), '[]'::jsonb)
                FROM jsonb_array_elements(
                    COALESCE(bundle_json->'evidence', '[]'::jsonb)
                ) AS elem
            ),
            true
        )
    WHERE jsonb_typeof(bundle_json->'evidence') = 'array'
      AND jsonb_array_length(bundle_json->'evidence') > 0
"""


def upgrade() -> None:
    # ---- 1. content_tsv 换成 simple 配置 ----
    #
    # 生成列的表达式要改，Postgres 允许 ALTER ... DROP/ADD EXPRESSION。
    # 索引要跟着重建 —— GIN 索引建在生成列上，表达式变了索引就失效。
    op.execute(
        "ALTER TABLE knowledge_chunks DROP COLUMN IF EXISTS content_tsv;"
    )
    op.execute(
        """
        ALTER TABLE knowledge_chunks ADD COLUMN content_tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector('simple', coalesce(content_normalized, ''))
            ) STORED;
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_chunks_tsv;")
    op.execute(
        "CREATE INDEX idx_chunks_tsv ON knowledge_chunks USING GIN (content_tsv);"
    )

    # ---- 2. 术语表的列名 ----
    #
    # ``canonical_name_en`` 存的是"规范名"。以前规范名是英文
    # （书是英文的），所以列名带 ``_en``。现在规范名是**中文**
    # （指南术语），列名就不该再带 ``_en`` ——
    # 留着会让下一个人以为这个字段只能装英文。
    #
    # ``aliases_en_json`` 一起删掉：中文流程里没有英文别名可存，
    # 留着是个空列，反而像"还能用"。
    op.execute(
        "ALTER TABLE concept_aliases RENAME COLUMN canonical_name_en TO canonical_name;"
    )
    op.execute("ALTER TABLE concept_aliases DROP COLUMN IF EXISTS aliases_en_json;")

    # ---- 3. 存量 JSON 的取值迁移 ----
    op.execute(_PLAN_APPLICATION_LABEL)
    op.execute(_PLAN_SOURCE_TYPE)
    op.execute(_BUNDLE_SOURCE_TYPE)


def downgrade() -> None:
    op.execute(
        """
        UPDATE plans
        SET application_label = 'book_summary',
            plan_json = jsonb_set(
                plan_json,
                '{application_label}',
                to_jsonb('book_summary'::text),
                true
            )
        WHERE plan_json->>'application_label' = 'source_summary'
        """
    )
    op.execute(
        """
        UPDATE plans
        SET plan_json = jsonb_set(
            plan_json,
            '{source_citations}',
            (
                SELECT COALESCE(jsonb_agg(
                    CASE
                        WHEN elem->>'source_type' = 'guideline'
                        THEN jsonb_set(elem, '{source_type}', to_jsonb('book'::text), true)
                        ELSE elem
                    END
                ), '[]'::jsonb)
                FROM jsonb_array_elements(
                    COALESCE(plan_json->'source_citations', '[]'::jsonb)
                ) AS elem
            ),
            true
        )
        WHERE jsonb_typeof(plan_json->'source_citations') = 'array'
          AND jsonb_array_length(plan_json->'source_citations') > 0
        """
    )
    op.execute(
        """
        UPDATE evidence_bundles
        SET bundle_json = jsonb_set(
            bundle_json,
            '{evidence}',
            (
                SELECT COALESCE(jsonb_agg(
                    CASE
                        WHEN elem->>'source_type' = 'guideline'
                        THEN jsonb_set(elem, '{source_type}', to_jsonb('book'::text), true)
                        ELSE elem
                    END
                ), '[]'::jsonb)
                FROM jsonb_array_elements(
                    COALESCE(bundle_json->'evidence', '[]'::jsonb)
                ) AS elem
            ),
            true
        )
        WHERE jsonb_typeof(bundle_json->'evidence') = 'array'
          AND jsonb_array_length(bundle_json->'evidence') > 0
        """
    )
    op.execute(
        "ALTER TABLE concept_aliases ADD COLUMN IF NOT EXISTS "
        "aliases_en_json JSONB NOT NULL DEFAULT '[]'::jsonb;"
    )
    op.execute(
        "ALTER TABLE concept_aliases RENAME COLUMN canonical_name TO canonical_name_en;"
    )
    op.execute("ALTER TABLE knowledge_chunks DROP COLUMN IF EXISTS content_tsv;")
    op.execute(
        """
        ALTER TABLE knowledge_chunks ADD COLUMN content_tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector('english', coalesce(content_normalized, ''))
            ) STORED;
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_chunks_tsv;")
    op.execute(
        "CREATE INDEX idx_chunks_tsv ON knowledge_chunks USING GIN (content_tsv);"
    )
