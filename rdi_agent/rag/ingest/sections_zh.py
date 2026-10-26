"""中文标题 -> 章节 code。

**为什么单独一个模块**：切分器（:mod:`rdi_agent.rag.ingest.chunk`）要给每个
标题生成一个稳定的 ``section_code`` —— 它要进数据库、要在检索结果里显示、
要当外键。规则只有两条，单独放一个文件是为了让切分器不用关心
"标题里有哪些前缀要剥掉"这种细节。

**code 的组成**是 ``序号_标题``，序号保证稳定排序，标题保证可读。
原来英文书的 slug 是 ASCII（``013_readiness``），中文标题过不了
``[^0-9a-zA-Z]`` 那套正则 —— 而且**用中文标题当 code 反而更好**：
评测集里写 ``expected_sections`` 时能直接看懂，不用去查一张对照表。
"""

from __future__ import annotations

import re

__all__ = [
    "slug_zh",
    "clean_title",
]

# 标题里要剥掉的前缀："一、" "（一）" "附件 1" 这些
_ENUM_PREFIX = re.compile(
    r"^([一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|\(\s*[一二三四五六七八九十]+\s*\))\s*"
)
_APPENDIX_PREFIX = re.compile(r"^附件\s*\d+\s*")

# 标题里不适合做 code 的字符（code 会进 URL 和外键）
_CODE_UNSAFE = re.compile(r"[^\w一-鿿]+")

# 章节 code 的最大长度。Postgres 里是 VARCHAR(64)，
# 而 code 是 ``序号_``（4 个字符）+ slug，所以 slug 留 50，
# 加上前缀也才 54 —— 顶不到 64。
MAX_CODE_LEN = 50


def slug_zh(title: str) -> str:
    """把中文标题变成可用的 code 片段。

    保留汉字和字母数字，其余换成下划线。空标题返回 ``section``。
    """
    text = title.strip()
    text = _ENUM_PREFIX.sub("", text)
    text = _APPENDIX_PREFIX.sub("", text)
    text = _CODE_UNSAFE.sub("_", text).strip("_").lower()
    return text[:MAX_CODE_LEN] or "section"


def clean_title(title: str) -> str:
    """去掉标题里的编号前缀，但保留原文的完整标题在 ``title`` 里。

    ``title_original`` 保留原样（"（四）针对缺陷行为的干预"），
    ``title_zh`` 存去掉编号的版本 —— 前端和提示词里显示后者更干净。
    """
    cleaned = _ENUM_PREFIX.sub("", title.strip())
    cleaned = _APPENDIX_PREFIX.sub("", cleaned)
    return cleaned or title.strip()
