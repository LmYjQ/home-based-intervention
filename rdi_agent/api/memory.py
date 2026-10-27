"""长期记忆的管理接口（计划 §10.6 的"API：列出、确认、纠正、删除"）。

**这几个接口是"家长可以查看、纠正、删除记忆"那条验收的实现**。
所以它们的设计目标是：

- **看得懂**：每条记忆都带原话（``source.quote``）和状态，
  家长能判断"系统为什么记了这个"。
- **改得动**：纠正不是覆盖，是新建一条 + 旧的那条标作废 ——
  历史留在审计里，但不再参与回答。
- **删得掉**：软删除，内容立刻从 prompt 和列表里消失。

**归属校验**：每个请求都要带 ``family_id``，和孩子的归属对不上就当不存在
（404 而不是 403 —— 不确认某个 id 是否存在，避免探测）。
这不是真正的访问控制（阶段 5 再说），但对防呆和测试够用。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from rdi_agent.memory import service as memory_service

router = APIRouter(prefix="/api", tags=["memory"])


class MemoryActionRequest(BaseModel):
    family_id: str
    conversation_id: str | None = None
    turn_id: str | None = None


class CorrectMemoryRequest(MemoryActionRequest):
    # 纠正后的内容。家长自己写的字就是新的原话。
    content: str


@router.get("/children/{child_id}/memories")
async def list_memories(
    child_id: str,
    # 用 ``Annotated`` 而不是 ``= Query(...)``：
    # 后者会让**直接调用**这个函数时拿到的是 Query 对象而不是默认值，
    # 而仓库里的接口测试就是直接调函数（见 tests/test_api_*）。
    family_id: Annotated[str, Query(description="孩子所属家庭，用于校验归属")],
    types: Annotated[
        str | None,
        Query(
            description=(
                "逗号分隔：summary,note,attempt"
            )
        ),
    ] = None,
    include_history: Annotated[
        bool, Query(description="是否返回已作废/已删除的记忆")
    ] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    """列出这个孩子的全部记忆。

    默认**不返回**已作废/已删除的条目 —— 那是审计用的，
    日常查看时它们只会让家长困惑"为什么这里有一条不对的"。
    """
    wanted = (
        {x.strip() for x in types.split(",") if x.strip()} if types else None
    )
    result = await memory_service.list_for_child(
        child_id=child_id,
        family_id=family_id,
        types=wanted,
        include_history=include_history,
        limit=limit,
    )
    if not result:
        raise HTTPException(404, "孩子档案不存在")
    return result


@router.post("/children/{child_id}/memories/{memory_id}/confirm")
async def confirm_memory(
    child_id: str, memory_id: str, req: MemoryActionRequest
) -> dict[str, Any]:
    """确认一条待确认的记忆。幂等：重复确认不会写第二条审计。

    **2026-10-27 起基本不用了** —— 记忆改成自动维护，
    系统不再产生 ``pending_confirmation`` 状态。
    这个接口留着，是为了处理自动维护之前攒下的历史数据
    （那些条目确实还挂在待确认状态上）。
    """
    result = await memory_service.confirm_memory(
        child_id=child_id,
        family_id=req.family_id,
        memory_id=memory_id,
        conversation_id=req.conversation_id,
        turn_id=req.turn_id,
    )
    if result is None:
        raise HTTPException(404, "记忆不存在")
    return {"memory": result}


@router.patch("/children/{child_id}/memories/{memory_id}")
async def correct_memory(
    child_id: str, memory_id: str, req: CorrectMemoryRequest
) -> dict[str, Any]:
    """纠正一条记忆。

    **不覆盖历史**：新建一条，旧的标 ``superseded`` 并互相链接。
    下一次回答只看得见新的那条。

    **类目小结（``summ_``）不支持纠正** —— 它是派生产物，
    由该类目下的原始记录归纳而来，下一轮合并会按类目全量重算。
    要改小结的内容，该改的是它归纳自的那几条原始记录
    （家长在对话里说"不是这样"就会走那条链路）。

    **2026-10-27 起前端不再调这个接口** —— 纠正走对话更自然，
    家长不用先来面板里逐条点一遍。
    """
    if not req.content.strip():
        raise HTTPException(400, "纠正后的内容不能为空")
    result = await memory_service.correct_memory(
        child_id=child_id,
        family_id=req.family_id,
        memory_id=memory_id,
        content=req.content,
        conversation_id=req.conversation_id,
        turn_id=req.turn_id,
    )
    if result is None:
        raise HTTPException(404, "记忆不存在")
    return {"memory": result}


@router.post("/children/{child_id}/memories/{memory_id}/delete")
async def delete_memory(
    child_id: str, memory_id: str, req: MemoryActionRequest
) -> dict[str, Any]:
    """软删除一条记忆。重复删除不报错。"""
    result = await memory_service.delete_memory(
        child_id=child_id,
        family_id=req.family_id,
        memory_id=memory_id,
        conversation_id=req.conversation_id,
        turn_id=req.turn_id,
    )
    if result is None:
        raise HTTPException(404, "记忆不存在")
    return {"memory": result}
