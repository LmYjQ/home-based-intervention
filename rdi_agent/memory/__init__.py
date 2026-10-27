"""长期记忆（阶段 3，计划 §10）。

模块分工：

- :mod:`rdi_agent.memory.ids`       —— 归一化、内容哈希、记忆 id 方案
- :mod:`rdi_agent.memory.categories` —— 七个类目，以及"什么时候该合并"的判定
- :mod:`rdi_agent.memory.summaries`  —— ``children.memory_summaries_json`` 的读写视图
- :mod:`rdi_agent.memory.relevance` —— 按当前问题挑相关的记忆（只给 prompt 用）
- :mod:`rdi_agent.memory.merge`     —— 纯判定逻辑，不碰 DB、不调 LLM
- :mod:`rdi_agent.memory.service`   —— 抽取 / 合并 / 提交的编排

**拆这么细是为了让"最该被怀疑的那部分"可以脱离数据库单测**：
合并规则决定了"什么能自动记、什么要问家长"，它错了整层记忆就不可信，
而它本身是纯函数，不该需要起一个 Postgres 才能验。
"""
