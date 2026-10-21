"""图节点。

按副作用分三个模块，import 边界就是约束：

- ``chat_nodes``：纯逻辑，可以调 LLM，但**不碰数据库**。两个 ``interrupt`` 在这里。
- ``write_nodes``：可以写数据库，但**没有 interrupt**。
- ``feedback_nodes``：反馈入口，不中断。

LangGraph 恢复时会把含 ``interrupt()`` 的节点整个重跑，所以规则是
"有 interrupt 的地方不能有副作用"。放在两个模块里，就不用靠代码评审来保证。
"""