"""聊天页智能回复的回归测试：假模型跑通意图 → 生成 → 校验，不打真实 API。

覆盖：
1. 正常返回 intent / strategy / 3 条不同风格回复；
2. 回复里出现"贵司"等套话时自动重要一次；
3. 连续三次都不合规时抛 LLMServiceError（由接口映射成 502）；
4. 没配置简历也能生成，不返回 503；
5. /api/suggest-reply 的出参结构与前端约定一致。
"""

import pytest

import main
import reply_graph
from llm_service import ConfigurationError, LLMServiceError
from schemas import ReplySuggestion, SuggestReplyResponse

JOB = {"title": "AI 应用工程师", "company": "某科技公司", "salary": "20-30K"}
HISTORY = [
    {"role": "system", "content": "对方已查看简历"},
    {"role": "me", "content": "我做过 RAG 检索和 Agent 编排，想了解一下这个岗位。"},
    {"role": "hr", "content": "方便发份简历吗？另外你什么时候能到岗？"},
]

GOOD_REPLIES = [
    {"style": "稳重礼貌", "content": "好的，我整理一份简历发您，也想了解下这个岗位目前最急的事是什么。"},
    {"style": "强化匹配", "content": "我做过 RAG 检索优化，召回从 62% 提到 89%，和 JD 里的知识库建设正好对上。"},
    {"style": "高情商反问", "content": "简历这就发，你们现在更看重落地速度还是效果评测？我按这个方向补材料。"},
]


def _intent_payload(intent="索要简历", strategy="先发简历，再确认到岗时间"):
    return {"intent": intent, "strategy": strategy, "confidence": 0.9}


@pytest.fixture()
def fake_llm(monkeypatch):
    state = {"intent": _intent_payload(), "replies": [list(GOOD_REPLIES)], "intent_calls": 0, "reply_calls": 0}

    def fake_chat_json(system_prompt, user_prompt, *, max_tokens=1500, temperature=0.7, config=None):
        if "分析师" in system_prompt:
            state["intent_calls"] += 1
            return dict(state["intent"])
        if "聊天代笔" in system_prompt:
            state["reply_calls"] += 1
            queue = state["replies"]
            return {"replies": queue.pop(0) if len(queue) > 1 else queue[0]}
        raise AssertionError("出现了预期外的提示词")

    monkeypatch.setattr(reply_graph, "chat_json", fake_chat_json)
    monkeypatch.setattr(reply_graph, "load_resume", lambda: "简历：5 年后端，做过 RAG 检索优化。")
    return state


def test_suggest_replies_success(fake_llm, capsys):
    result = reply_graph.suggest_replies(job_info=JOB, history=HISTORY)

    assert result.intent == "索要简历"
    assert result.strategy
    assert [item.style for item in result.replies] == ["稳重礼貌", "强化匹配", "高情商反问"]
    assert fake_llm["intent_calls"] == 1
    assert fake_llm["reply_calls"] == 1

    logs = capsys.readouterr().out
    assert "intent_node" in logs and "generate_replies_node" in logs


def test_cliche_reply_triggers_retry(fake_llm):
    fake_llm["replies"] = [
        [{"style": "稳重礼貌", "content": "贵司这个岗位我很感兴趣，希望能有机会聊聊。"}],
        list(GOOD_REPLIES),
    ]

    result = reply_graph.suggest_replies(job_info=JOB, history=HISTORY)

    assert fake_llm["reply_calls"] == 2
    assert result.replies[0].style == "稳重礼貌"


def test_two_bad_attempts_raise(fake_llm):
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
    assert result.intent == "索要简历"
    assert len(result.replies) == 3


def test_ellipsis_is_not_a_sentence_break():
    """模型常用「……」当停顿，不能被误判成第三句。"""
    item = ReplySuggestion(
        style="高情商反问",
        content="简历发您了。想先问下……是偏 Agent 还是模型工程？",
    )
    assert item.content.startswith("简历发您了")


def test_endpoint_contract(client, monkeypatch):
    monkeypatch.setattr(
        main,
        "suggest_replies",
        lambda **kwargs: SuggestReplyResponse(
            intent="索要简历",
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
    assert set(data) == {"intent", "strategy", "replies"}
    assert len(data["replies"]) == 3
    assert set(data["replies"][0]) == {"style", "content"}


def test_endpoint_rejects_empty_history(client):
    response = client.post("/api/suggest-reply", json={"job_info": JOB, "chat_history": []})
    assert response.status_code == 422
