"""反思循环的回归测试：用假模型跑通所有分支，不打真实 API、不花钱。

覆盖：
1. 首轮评审通过 → 0 次重写；
2. 首轮不通过 → 重写一次后通过；
3. 一直不通过 → 重写 2 次后强制收尾，不会无限循环；
4. 硬性规则（套话/超长）先拦下，省掉一次评审调用；
5. /api/analyze 的出参结构与改造前完全一致。
"""

import pytest

import agent_graph
import main
from schemas import AnalyzeResponse

JD = "负责大模型应用落地，要求熟悉 RAG、LangGraph、FastAPI，有检索召回优化经验。"

GREETINGS = [
    "你们这个岗位卡在 RAG 召回上吧，我在上一家把召回率从 62% 拉到 89%。",
    "做过 3 个 LangGraph 项目，最长的一条链路跑了 6 个节点。",
    "如果入职，你更想先解决检索延迟还是评测缺失？",
]

REWRITTEN = [
    "JD 里 RAG 召回是硬指标，我把线上召回从 62% 拉到 89%，评测集 1200 条。",
    "LangGraph 搭过 3 条生产链路，最长 6 个节点，P99 稳定在 1.8 秒。",
    "你们现在的 RAG 评测集有多大，召回掉了靠什么定位？",
]

PASS = {"passed": True, "score": 88, "ai_cliche": [], "missing_keywords": [], "issues": [], "suggestions": []}
FAIL = {
    "passed": False,
    "score": 55,
    "ai_cliche": ["希望有机会"],
    "missing_keywords": ["RAG"],
    "issues": ["第 1 条像 AI 套话，没有具体项目"],
    "suggestions": ["把第 1 条改成召回率从 62% 到 89% 的项目细节"],
}


def _fake_analyze(job_title, jd_text, config=None, *, resume=None):
    return AnalyzeResponse(
        match_score=76,
        pros=["做过 RAG 检索优化", "熟悉 LangGraph 状态机", "FastAPI 落地过 3 个服务"],
        gaps=["缺少大规模评测经验", "没有团队管理经历"],
        greetings=list(GREETINGS),
    )


@pytest.fixture()
def fake_llm(monkeypatch):
    """把两个模型调用点换成队列驱动的假实现，并记录调用次数。"""
    state = {"reviews": [], "rewrites": [], "review_calls": 0, "rewrite_calls": 0, "analyze_calls": 0}

    def fake_analyze(job_title, jd_text, config=None, *, resume=None):
        state["analyze_calls"] += 1
        return _fake_analyze(job_title, jd_text, config, resume=resume)

    def fake_chat_json(system_prompt, user_prompt, *, max_tokens=1500, temperature=0.7, config=None):
        if "审核员" in system_prompt:
            state["review_calls"] += 1
            return state["reviews"].pop(0) if state["reviews"] else dict(PASS)
        if "写手" in system_prompt:
            state["rewrite_calls"] += 1
            return {"greetings": state["rewrites"].pop(0)}
        raise AssertionError("出现了预期外的提示词")

    monkeypatch.setattr(agent_graph, "load_resume", lambda: "简历：5 年后端，做过 RAG 检索优化。")
    monkeypatch.setattr(agent_graph, "analyze_jd", fake_analyze)
    monkeypatch.setattr(agent_graph, "chat_json", fake_chat_json)
    return state


def test_pass_on_first_review(fake_llm, capsys):
    fake_llm["reviews"] = [dict(PASS)]

    result = agent_graph.analyze_with_review("大模型应用开发工程师", JD)

    assert result.greetings == GREETINGS
    assert fake_llm["analyze_calls"] == 1
    assert fake_llm["review_calls"] == 1
    assert fake_llm["rewrite_calls"] == 0
    assert "重写 0 次" in capsys.readouterr().out


def test_rewrite_once_then_pass(fake_llm):
    fake_llm["reviews"] = [dict(FAIL), dict(PASS)]
    fake_llm["rewrites"] = [list(REWRITTEN)]

    result = agent_graph.analyze_with_review("大模型应用开发工程师", JD)

    assert result.greetings == REWRITTEN
    assert fake_llm["rewrite_calls"] == 1
    assert fake_llm["review_calls"] == 2


def test_stops_after_two_rewrites(fake_llm, capsys):
    fake_llm["reviews"] = [dict(FAIL), dict(FAIL), dict(FAIL)]
    fake_llm["rewrites"] = [list(REWRITTEN), list(GREETINGS)]

    result = agent_graph.analyze_with_review("大模型应用开发工程师", JD)

    # 首稿 + 2 次重写 = 3 版，评审 3 次，然后强制收尾
    assert fake_llm["rewrite_calls"] == agent_graph.MAX_REWRITES == 2
    assert fake_llm["review_calls"] == 3
    assert result.greetings == GREETINGS
    assert "按上限收尾" in capsys.readouterr().out


def test_hard_rule_skips_review_call(fake_llm, monkeypatch):
    """首稿带寒暄套话时，靠本地规则直接打回，不浪费一次评审调用。"""
    cliche_draft = ["您好，我对贵司的岗位很感兴趣", "做过 RAG 检索优化，召回 62%→89%", "你们评测集多大？"]
    fake_llm["rewrites"] = [list(REWRITTEN)]
    fake_llm["reviews"] = [dict(PASS)]

    class _Stub:
        """模拟"模型输出了套话"的首稿：真实链路上这种稿子过不了 AnalyzeResponse，这里直接注入以覆盖硬规则分支。"""

        def model_dump(self):
            return {
                "match_score": 76,
                "pros": ["a", "b", "c"],
                "gaps": ["d", "e"],
                "greetings": cliche_draft,
            }

    monkeypatch.setattr(agent_graph, "analyze_jd", lambda *a, **kw: _Stub())

    result = agent_graph.analyze_with_review("大模型应用开发工程师", JD)

    assert result.greetings == REWRITTEN
    # 第 1 轮被硬规则拦下（0 次评审），第 2 轮才走模型评审
    assert fake_llm["review_calls"] == 1
    assert fake_llm["rewrite_calls"] == 1


def test_endpoint_contract_unchanged(client, monkeypatch):
    """前端只认这 4 个字段，多一个少一个都不行。"""
    monkeypatch.setattr(main, "append_job_record", lambda record: None)
    monkeypatch.setattr(
        main,
        "analyze_with_review",
        lambda job_title, jd_text, config=None: _fake_analyze(job_title, jd_text, config),
    )

    response = client.post(
        "/api/analyze",
        json={"job_title": "大模型应用开发工程师", "jd_text": JD},
    )

    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"match_score", "pros", "gaps", "greetings"}
    assert isinstance(data["match_score"], int)
    assert len(data["greetings"]) == 3
    assert all(isinstance(item, str) for item in data["greetings"])
