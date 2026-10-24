"""Mock 后端 —— 只给前端手动测试用。

**为什么需要它**：Docker 的 Postgres 挂掉时，前端就没法验证。
这个 mock 用**真实抓下来的响应**（真后端跑出来的形状，不是手编的），
按状态机回放，所以前端能独立验证渲染逻辑对不对。

它**不做**任何业务判断：不会真的调 LLM、不会真的检索。
只保证"前端拿到的 JSON 形状和真后端一致"。

跑法：
    .venv/bin/python frontend_rdi/mock_server.py --port 8010

前端把 vite 代理指到 8010 就能脱离数据库跑。
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

# ---------------------------------------------------------------- 抓下来的真实数据
# 来自真后端的一次完整会话（2026-10-22），字段名和结构都照抄响应。
REAL_EVIDENCE: list[dict[str, Any]] = [
    {
        "evidence_id": "c5515f87-7fd8-448f-a8af-a4923e56b6b6",
        "source_type": "book",
        "title": "Readiness",
        "section_title": "Readiness",
        "pdf_page_start": 232,
        "pdf_page_end": 232,
        "summary": "Teaching sign language to an older child who has not been able to use oral language.",
        "relevance_reason": "语义相似：与孩子的准备状态相关",
        "review_status": "unreviewed",
        "similarity": 0.6123,
        "may_quote_to_user": False,
    },
    {
        "evidence_id": "a1b2c3d4-0000-4000-8000-000000000002",
        "source_type": "book",
        "title": "Shared Attention",
        "section_title": "共同注意",
        "pdf_page_start": 118,
        "pdf_page_end": 119,
        "summary": "共同注意是两个人同时关注同一个对象的那一刻，是语言和社交的基础。",
        "relevance_reason": "术语桥接：共同注意 → Shared Attention",
        "review_status": "reviewed",
        "similarity": 0.7011,
        "may_quote_to_user": False,
    },
]

REAL_PLANS: list[dict[str, Any]] = [
    {
        "plan_id": "plan_1",
        "title": "进园前 10 分钟的固定过渡流程",
        "observed_facts": [
            "到幼儿园门口就大哭，抱起来能好",
            "试过提前十分钟出门，也试过抱着她走过去，都没什么用",
        ],
        "tentative_interpretations": [
            "可能是在分离处难以承受与家长的注意分离（尚未证实）"
        ],
        "explanation": "把「离开家」和「到幼儿园门口」之间拆成固定的几步，减少每次要面对的新变化。",
        "parent_actions": [
            {
                "action": "在家演练一次「穿鞋 → 挥手 → 走出门」",
                "context": "早上出门前",
                "steps": ["固定顺序，每一步只做一件事", "每次结束都有一句告别语"],
                "observe": ["孩子在挥手后是否愿意松手"],
                "reduce_or_stop_if": ["孩子连续三天在这个环节崩溃，就退回只做告别语"],
            }
        ],
        "success_indicators": ["到门口时哭的时间比这次短"],
        "stop_conditions": ["孩子出现持续的呕吐或身体不适时立即停止"],
        "source_citations": [
            {
                "source_id": "a1b2c3d4-0000-4000-8000-000000000002",
                "source_type": "book",
                "title": "Shared Attention",
                "chapter": "共同注意",
                "pdf_page_start": 118,
                "pdf_page_end": 119,
                "claim_supported": "共同注意需要两个人在同一个活动上聚焦",
            }
        ],
        "application_label": "book_summary",
        "uncertainty_notes": ["目前只有一次观察，无法判断是否稳定"],
        "follow_up_questions": ["放学接的时候状态怎么样？"],
        "recommended": True,
    },
    {
        "plan_id": "plan_2",
        "title": "用共同注意拉回注意力",
        "observed_facts": ["一直抱着我腿不说话"],
        "tentative_interpretations": [],
        "explanation": "用孩子感兴趣的东西，把注意力从分离情境上挪开。",
        "parent_actions": [
            {
                "action": "出门前先玩一个孩子喜欢的活动，再进入出门流程",
                "context": "出门前 5 分钟",
                "steps": ["让孩子挑一个", "家长跟随孩子的注意"],
                "observe": ["孩子是否主动指向或看着你"],
                "reduce_or_stop_if": ["孩子拒绝互动时不要强拉回来"],
            }
        ],
        "success_indicators": ["出门前的互动比之前多"],
        "stop_conditions": ["孩子明显烦躁时停止"],
        "source_citations": [],
        "application_label": "ai_adaptation",
        "uncertainty_notes": ["这是应用转化，不是书里的原话"],
        "follow_up_questions": [],
        "recommended": False,
    },
    {
        "plan_id": "plan_3",
        "title": "把告别仪式固定下来",
        "observed_facts": ["到幼儿园门口就大哭"],
        "tentative_interpretations": [],
        "explanation": "告别越简短越可预测，孩子的分离焦虑越容易处理。",
        "parent_actions": [
            {
                "action": "用一句固定的话告别，然后立刻离开",
                "context": "幼儿园门口",
                "steps": ["不超过 5 秒", "不回头"],
                "observe": ["老师是否顺利接手"],
                "reduce_or_stop_if": ["孩子完全无法被老师接手时，先缩短离开前的互动时间"],
            }
        ],
        "success_indicators": ["交接时长比这次短"],
        "stop_conditions": ["孩子出现攻击行为时暂停这个做法并寻求专业帮助"],
        "source_citations": [],
        "application_label": "ai_adaptation",
        "uncertainty_notes": [],
        "follow_up_questions": [],
        "recommended": False,
    },
]

FOLLOWUP_1 = [
    "孩子在幼儿园里是否有特别喜欢的活动或老师？",
    "孩子在家里是否有类似的情绪表现？",
]
FOLLOWUP_2 = [
    "孩子在幼儿园里是否有特别喜欢的老师或同学？",
    "孩子在幼儿园里是否有特别不喜欢的老师或同学？",
]
FOLLOWUP_3 = [
    "孩子在家和幼儿园里的情绪表现是否一致？",
    "孩子在其他场合（如去公园、朋友家）是否有类似哭闹的情况？",
]


class State:
    """每个会话一份状态。"""

    def __init__(self) -> None:
        self.rounds = 0
        self.trial_id = ""
        self.selected = ""


class Handler(BaseHTTPRequestHandler):
    states: dict[str, State] = {}

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[mock] {fmt % args}")

    # ------------------------------------------------------------ 工具
    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    # ------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(
                200,
                {
                    "status": "ok",
                    "database": {"ok": True, "server": "PostgreSQL 16.15 (mock)"},
                    "pgvector": {"ok": True, "version": "0.8.6"},
                    "fulltext": {"ok": True},
                    "keyword_backend": "postgres_fts",
                    "embedding_model": "BAAI/bge-m3",
                    "rerank_enabled": True,
                    "rag_enabled": True,
                    "memory_enabled": True,
                    "llm": {
                        "mode": "live",
                        "model": "Qwen/Qwen2.5-7B-Instruct",
                        "timeout_s": 60.0,
                        "has_key": True,
                        "llm_ok": True,
                        "error": "",
                    },
                },
            )
            return
        if self.path.startswith("/api/conversations/"):
            cid = self.path.rsplit("/", 1)[-1]
            st = self.states.get(cid)
            if st is None:
                self._send(404, {"detail": "会话不存在"})
                return
            self._send(
                200,
                {
                    "conversation_id": cid,
                    "family_id": "mock",
                    "child_id": "mock",
                    "status": "awaiting_plan_selection" if st.rounds < 3 else "completed",
                    "pending_interrupt": None,
                    "interactions": [],
                    "plans": [],
                    "evidence": REAL_EVIDENCE,
                    "evidence_decision": {
                        "evidence_status": "direct",
                        "can_support_personalized_advice": True,
                        "missing_information": [],
                    },
                },
            )
            return
        self._send(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        body = self._read()

        if self.path == "/api/conversations":
            self._send(
                200,
                {
                    "conversation_id": f"mock-{uuid.uuid4().hex[:12]}",
                    "thread_id": f"mock-{uuid.uuid4().hex[:12]}",
                    "child_id": "mock-child",
                    "family_id": "mock-family",
                },
            )
            return

        if self.path.endswith("/messages"):
            cid = self.path.split("/")[3]
            st = self.states.setdefault(cid, State())
            st.rounds += 1
            time.sleep(0.6)  # 假装在算，让 loading 状态看得见

            if st.rounds <= 3:
                qs = {1: FOLLOWUP_1, 2: FOLLOWUP_2, 3: FOLLOWUP_3}[st.rounds]
                self._send(
                    200,
                    {
                        "conversation_id": cid,
                        "turn_id": f"turn_mock_{st.rounds}",
                        "status": "awaiting_followup",
                        "questions": qs,
                        "plans": [],
                        "message": "\n".join(qs),
                        "trial_id": None,
                        "evidence": [],
                        "evidence_decision": {
                            "evidence_status": None,
                            "can_support_personalized_advice": None,
                            "missing_information": [],
                        },
                        "evidence_gaps": [],
                    },
                )
                return

            self._send(
                200,
                {
                    "conversation_id": cid,
                    "turn_id": f"turn_mock_{st.rounds}",
                    "status": "awaiting_plan_selection",
                    "questions": [],
                    "plans": REAL_PLANS,
                    "message": "",
                    "trial_id": None,
                    "evidence": REAL_EVIDENCE,
                    "evidence_decision": {
                        "evidence_status": "direct",
                        "can_support_personalized_advice": True,
                        "missing_information": [],
                    },
                    "evidence_gaps": ["缺少孩子在园内的具体表现细节"],
                },
            )
            return

        if self.path.endswith("/select-plan"):
            cid = self.path.split("/")[3]
            st = self.states.setdefault(cid, State())
            st.selected = body.get("plan_id", "")
            st.trial_id = f"trial_mock_{uuid.uuid4().hex[:8]}"
            self._send(
                200,
                {
                    "conversation_id": cid,
                    "status": "completed",
                    "selected_plan_id": st.selected,
                    "selected_plan_db_id": "db-mock-1",
                    "trial_id": st.trial_id,
                    "message": "方案已经记下了。这周按这个流程做，做完回来告诉我们发生了什么。",
                },
            )
            return

        if "/feedback" in self.path:
            self._send(
                200,
                {
                    "trial_id": body.get("trial_id", ""),
                    "status": "completed",
                    "feedback_created": True,
                    "analysis": {
                        "actual_execution": "在门口只坚持了告别就离开了",
                        "observed_changes": ["今天没有哭", "但进教室后抓了老师的手"],
                        "execution_difficulties": ["早上时间紧张，流程被压缩"],
                        "open_questions": ["放学接的时候是否也这样？"],
                        "relation_to_plan": "执行了一部分，尚未看到稳定变化",
                        "confidence_note": "只有一次观察，不下结论",
                    },
                    "next_step": {
                        "acknowledgment": "收到，谢谢你把过程记下来。",
                        "next_steps": ["这周保持同样的流程，不要同时改两件事", "记录每天到门口哭的时长"],
                        "observe_this_week": ["哭闹时长", "是否愿意和老师交接"],
                    },
                    "message": "收到，谢谢你把过程记下来。这周保持同样的流程，做完再回来。",
                },
            )
            return

        self._send(404, {"detail": "not found"})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8010)
    args = ap.parse_args()
    srv = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mock 后端在 http://127.0.0.1:{args.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()