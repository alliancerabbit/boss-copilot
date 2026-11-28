"""聊天页智能回复的回归测试：假模型跑通「意图 → 条件路由 → 分支付约束」，不打真实 API。

覆盖：
1. 6 类意图识别结果映射到 4 条生成分支，各分支用各自的风格三元组；
2. 意图输出无法解析时按关键词兜底，不额外调用模型；
3. 分支级硬约束（字数上限、带问号条数）不满足时带原因重试；
4. 回复里出现"贵司"等套话时自动重试一次；
5. 连续三次都不合规时抛 LLMServiceError（由接口映射成 502）；
6. 没配置简历也能生成，不返回 503；
7. /api/suggest-reply 的出参结构与前端约定一致。
"""

import pytest
from pydantic import ValidationError

import main
import reply_graph
from llm_service import ConfigurationError, LLMServiceError
from schemas import ReplySuggestion, SuggestReplyResponse

JOB = {"title": "AI 应用工程师", "company": "某科技公司", "salary": "20-30K"}
HISTORY = [
    {"role": "system", "content": "对方已查看附件简历"},
    {"role": "me", "content": "我做过 RAG 检索和 Agent 编排，想了解一下这个岗位。"},
    {"role": "hr", "content": "方便发份简历吗？另外你什么时候能到岗？"},
]

# 各分支的合格语料：字数与问号数都必须落在 playbook 的约束内
REPLIES_BY_BRANCH = {
    "etiquette": [
        {"style": "立刻给到", "content": "好的，我整理一份简历发您，也想了解下这个岗位目前最急的事是什么。"},
        {"style": "给到+补匹配点", "content": "我做过 RAG 检索优化，召回从 62% 提到 89%，和 JD 里的知识库建设正好对上。"},
        {"style": "给到+确认下一步", "content": "简历这就发，你们现在更看重落地速度还是效果评测？我按这个方向补材料。"},
    ],
    "hold": [
        {"style": "礼貌收尾", "content": "好的，那我等您消息。"},
        {"style": "留个钩子", "content": "我这边 RAG 检索优化做过一轮，需要的话随时可以细聊。"},
        {"style": "轻问进度", "content": "大概多久能有反馈？"},
    ],
    "screening": [
        {"style": "锚定区间反问预算", "content": "我的期望是 25-30K，想先了解下这个岗位的预算区间？"},
        {"style": "能力对价", "content": "上一份做过 RAG 召回优化，从 62% 提到 89%，这个区间是按能力给的。"},
        {"style": "先聊岗位再谈薪", "content": "薪资可以面谈，想先聊聊岗位现在最卡的是什么？"},
    ],
    "interview": [
        {"style": "确认时间+问形式", "content": "时间可以，周三下午两点后都方便，线上面试的话我用会议室。"},
        {"style": "给两个备选时段", "content": "周三下午或周四上午我都能安排，您看哪个合适？"},
        {"style": "先应下再问细节", "content": "没问题，我可以参加，想先了解下面试大概几轮、需要准备什么？"},
    ],
}

GOOD_REPLIES = REPLIES_BY_BRANCH["etiquette"]


def _intent_payload(intent="REQUEST_INFO", analysis="在收材料，属于推进信号", strategy="先发简历，再确认到岗时间"):
    return {"intent": intent, "confidence": "高", "analysis": analysis, "strategy": strategy}


@pytest.fixture()
def fake_llm(monkeypatch):
    state = {
        "intent": _intent_payload(),
        "replies": [list(REPLIES_BY_BRANCH["etiquette"])],
        "intent_calls": 0,
        "reply_calls": 0,
        "reply_systems": [],
        "intent_users": [],
    }

    def fake_chat_json(system_prompt, user_prompt, *, max_tokens=1500, temperature=0.7, config=None):
        if "分析师" in system_prompt:
            state["intent_calls"] += 1
            state["intent_users"].append(user_prompt)
            return dict(state["intent"])
        if "聊天代笔" in system_prompt:
            state["reply_calls"] += 1
            state["reply_systems"].append(system_prompt)
            queue = state["replies"]
            return {"replies": queue.pop(0) if len(queue) > 1 else queue[0]}
        raise AssertionError("出现了预期外的提示词")

    monkeypatch.setattr(reply_graph, "chat_json", fake_chat_json)
    monkeypatch.setattr(reply_graph, "load_resume", lambda: "简历：5 年后端，做过 RAG 检索优化。")
    return state


def test_request_info_routes_to_etiquette(fake_llm, capsys):
    result = reply_graph.suggest_replies(job_info=JOB, history=HISTORY)

    assert result.intent == "REQUEST_INFO"
    assert result.intent_label == "索要资料"
    assert result.confidence == "高"
    assert result.analysis.startswith("在收材料")
    assert result.strategy
    assert [item.style for item in result.replies] == list(reply_graph.INTENT_PLAYBOOKS["REQUEST_INFO"].styles)
    assert fake_llm["intent_calls"] == 1
    assert fake_llm["reply_calls"] == 1

    logs = capsys.readouterr().out
    assert "intent_node" in logs
    assert "route_by_intent" in logs
    assert "generate_etiquette_replies_node" in logs


def test_pending_review_routes_to_hold(fake_llm, capsys):
    fake_llm["intent"] = _intent_payload("PENDING_REVIEW", "在走内部流程，别催", "简短收尾，等消息")
    fake_llm["replies"] = [list(REPLIES_BY_BRANCH["hold"])]

    result = reply_graph.suggest_replies(job_info=JOB, history=HISTORY)

    assert result.intent == "PENDING_REVIEW"
    assert result.intent_label == "评估挂起"
    assert [item.style for item in result.replies] == ["礼貌收尾", "留个钩子", "轻问进度"]
    assert "generate_hold_replies_node" in capsys.readouterr().out

    system_prompt = fake_llm["reply_systems"][0]
    assert "绝不长篇大论" in system_prompt
    assert "最多 1 条带问号" in system_prompt


def test_screening_routes_and_offers_three_price_strategies(fake_llm, capsys):
    fake_llm["intent"] = _intent_payload("SCREENING", "在摸你的价格和底线", "给区间并反问预算")
    fake_llm["replies"] = [list(REPLIES_BY_BRANCH["screening"])]

    result = reply_graph.suggest_replies(job_info=JOB, history=HISTORY)

    assert result.intent == "SCREENING"
    assert [item.style for item in result.replies] == ["锚定区间反问预算", "能力对价", "先聊岗位再谈薪"]
    assert "generate_screening_replies_node" in capsys.readouterr().out
    assert "锚定区间反问预算" in fake_llm["reply_systems"][0]


def test_interview_invite_routes_and_asks_format(fake_llm, capsys):
    fake_llm["intent"] = _intent_payload("INTERVIEW_INVITE", "已过初筛，只看时间", "确认时间并问形式")
    fake_llm["replies"] = [list(REPLIES_BY_BRANCH["interview"])]

    result = reply_graph.suggest_replies(job_info=JOB, history=HISTORY)

    assert result.intent == "INTERVIEW_INVITE"
    assert [item.style for item in result.replies] == ["确认时间+问形式", "给两个备选时段", "先应下再问细节"]
    assert "generate_interview_replies_node" in capsys.readouterr().out
    assert "线上面" not in fake_llm["reply_systems"][0]  # 提示词只约束，不预写答案
    assert "面试流程" in fake_llm["reply_systems"][0]


def test_chinese_intent_alias_is_normalized(fake_llm, capsys):
    fake_llm["intent"] = _intent_payload("约面试", "已过初筛", "确认时间")
    fake_llm["replies"] = [list(REPLIES_BY_BRANCH["interview"])]

    result = reply_graph.suggest_replies(job_info=JOB, history=HISTORY)
    assert result.intent == "INTERVIEW_INVITE"
    assert result.intent_label == "约面试"


def test_unknown_intent_uses_keyword_fallback(fake_llm):
    fake_llm["intent"] = {"intent": "看不出来", "confidence": 0.9, "analysis": "", "strategy": ""}
    fake_llm["replies"] = [list(REPLIES_BY_BRANCH["hold"])]  # 婉拒也是短句收尾

    history = [
        {"role": "me", "content": "您好，想了解下这个岗位。"},
        {"role": "hr", "content": "抱歉，这个岗位已经招满了。"},
    ]
    result = reply_graph.suggest_replies(job_info=JOB, history=history)

    assert result.intent == "REJECTED"
    assert result.confidence == "低"
    assert result.analysis.startswith("流程性婉拒")
    assert fake_llm["intent_calls"] == 1


def test_empty_hr_with_view_hint_falls_back_to_pending(fake_llm):
    fake_llm["intent"] = {"intent": "", "confidence": "", "analysis": "", "strategy": ""}
    fake_llm["replies"] = [list(REPLIES_BY_BRANCH["hold"])]

    history = [
        {"role": "system", "content": "对方已查看附件简历"},
        {"role": "me", "content": "简历发您了，方便的话看下。"},
    ]
    result = reply_graph.suggest_replies(job_info=JOB, history=history)

    assert result.intent == "PENDING_REVIEW"
    assert result.confidence == "低"


def test_fallback_prefers_specific_pending_keyword(fake_llm):
    """「简历收到了」含「简历」，但更具体的「收到」应判挂起，不能误判成索要资料。"""
    fake_llm["intent"] = {"intent": "看不出来", "confidence": "", "analysis": "", "strategy": ""}
    fake_llm["replies"] = [list(REPLIES_BY_BRANCH["hold"])]

    history = [{"role": "hr", "content": "简历收到了，我先看看。"}]
    result = reply_graph.suggest_replies(job_info=JOB, history=history)

    assert result.intent == "PENDING_REVIEW"


def test_hold_reply_too_many_questions_triggers_retry(fake_llm):
    fake_llm["intent"] = _intent_payload("PENDING_REVIEW", "在走流程", "收尾等消息")
    fake_llm["replies"] = [
        [
            {"style": "礼貌收尾", "content": "好的，我等您消息。"},
            {"style": "留个钩子", "content": "大概多久有反馈？"},
            {"style": "轻问进度", "content": "方便说下下一步流程吗？"},
        ],
        list(REPLIES_BY_BRANCH["hold"]),
    ]

    result = reply_graph.suggest_replies(job_info=JOB, history=HISTORY)

    assert fake_llm["reply_calls"] == 2
    assert result.replies[0].style == "礼貌收尾"


def test_hold_reply_too_long_triggers_retry(fake_llm):
    fake_llm["intent"] = _intent_payload("PENDING_REVIEW", "在走流程", "收尾等消息")
    too_long = "我这边做过 RAG 检索链路优化和 Agent 编排，也带过两个人，需要的话随时可以细聊。"
    assert len(too_long) > reply_graph.INTENT_PLAYBOOKS["PENDING_REVIEW"].max_chars

    fake_llm["replies"] = [
        [
            {"style": "礼貌收尾", "content": too_long},
            {"style": "留个钩子", "content": "好的，我等您消息。"},
            {"style": "轻问进度", "content": "大概多久有反馈？"},
        ],
        list(REPLIES_BY_BRANCH["hold"]),
    ]

    result = reply_graph.suggest_replies(job_info=JOB, history=HISTORY)

    assert fake_llm["reply_calls"] == 2
    assert result.replies[0].style == "礼貌收尾"


def test_cliche_reply_triggers_retry(fake_llm):
    fake_llm["replies"] = [
        [{"style": "稳重礼貌", "content": "贵司这个岗位我很感兴趣，希望能有机会聊聊。"}],
        list(GOOD_REPLIES),
    ]

    result = reply_graph.suggest_replies(job_info=JOB, history=HISTORY)

    assert fake_llm["reply_calls"] == 2
    assert result.replies[0].style == "立刻给到"


def test_three_bad_attempts_raise(fake_llm):
    bad = [
        {"style": "稳重礼貌", "content": "第一句。第二句。第三句。"},
        {"style": "强化匹配", "content": "我做过 RAG。"},
        {"style": "高情商反问", "content": "你们怎么看？"},
    ]
    fake_llm["replies"] = [list(bad)]  # 队列只有一个，永远返回这份

    with pytest.raises(LLMServiceError):
        reply_graph.suggest_replies(job_info=JOB, history=HISTORY)

    assert fake_llm["reply_calls"] == 3


def test_missing_resume_still_works(fake_llm, monkeypatch):
    def no_resume():
        raise ConfigurationError("未找到简历文件")

    monkeypatch.setattr(reply_graph, "load_resume", no_resume)

    result = reply_graph.suggest_replies(job_info=JOB, history=HISTORY)
    assert result.intent == "REQUEST_INFO"
    assert len(result.replies) == 3


def test_ellipsis_is_not_a_sentence_break():
    """模型常用「……」当停顿，不能被误判成第三句。"""
    item = ReplySuggestion(
        style="高情商反问",
        content="简历发您了。想先问下……是偏 Agent 还是模型工程？",
    )
    assert item.content.startswith("简历发您了")


def test_response_label_is_filled_from_intent():
    result = SuggestReplyResponse(
        intent="PENDING_REVIEW",
        confidence="中",
        analysis="在走内部流程",
        replies=REPLIES_BY_BRANCH["hold"],
    )
    assert result.intent_label == "评估挂起"


def test_unknown_intent_code_is_rejected():
    with pytest.raises(ValidationError):
        SuggestReplyResponse(
            intent="PENDING",
            confidence="中",
            replies=REPLIES_BY_BRANCH["hold"],
        )


def test_endpoint_contract(client, monkeypatch):
    monkeypatch.setattr(
        main,
        "suggest_replies",
        lambda **kwargs: SuggestReplyResponse(
            intent="REQUEST_INFO",
            confidence="高",
            analysis="在收集材料，属于推进信号",
            strategy="先发简历，再确认到岗时间",
            replies=GOOD_REPLIES,
        ),
    )

    response = client.post(
        "/api/suggest-reply",
        json={"job_info": JOB, "chat_history": HISTORY},
    )

    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"intent", "intent_label", "confidence", "analysis", "strategy", "replies"}
    assert data["intent_label"] == "索要资料"
    assert len(data["replies"]) == 3
    assert set(data["replies"][0]) == {"style", "content"}


def test_endpoint_rejects_empty_history(client):
    response = client.post("/api/suggest-reply", json={"job_info": JOB, "chat_history": []})
    assert response.status_code == 422
