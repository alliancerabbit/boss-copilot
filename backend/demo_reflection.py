"""在终端直接跑一遍「生成 → 评审 → 重写」循环，观察每个节点的流转。

用法（在 backend 目录执行）：
    .venv\\Scripts\\python.exe demo_reflection.py              # 真实跑一次
    .venv\\Scripts\\python.exe demo_reflection.py --force-fail  # 强制首轮评审不通过，演示重写闭环

--force-fail 只伪造"第一次评审"的结论，重写和复审仍然走真实模型，
用来确认打回重写这条路径在你的机器上确实能跑通。
"""

from __future__ import annotations

import argparse

import agent_graph

SAMPLE_JD = (
    "岗位职责：1. 面向客户交付大模型应用，负责需求澄清、方案设计与落地；"
    "2. 搭建 RAG 知识库与 Agent 工作流，使用 Python、FastAPI 开发后端服务；"
    "3. 负责 Prompt 设计与效果评测，能用指标量化回答质量；"
    "4. 对接客户业务系统 REST API，处理鉴权、限流与重试。"
    "任职要求：3 年以上开发经验，熟悉 LLM 应用、Agent 编排、向量检索，有 To B 交付经验优先。"
)

FORCED_FAIL_REVIEW = {
    "passed": False,
    "score": 52,
    "ai_cliche": ["大模型应用落地最常卡在效果没法量化"],
    "covered_keywords": ["效果评测"],
    "missing_keywords": ["RAG", "向量检索"],
    "issues": ["三条都没有点名 RAG 或向量检索，JD 的核心要求没接住"],
    "suggestions": ["把第 1 条改成 RAG 召回率的具体数字，把第 2 条换成向量库选型"],
}


def main() -> int:
    parser = argparse.ArgumentParser(description="跑一遍招呼语反思循环")
    parser.add_argument("--force-fail", action="store_true", help="强制首轮评审不通过，演示重写")
    parser.add_argument("--title", default="AI 解决方案工程师（FDE）")
    parser.add_argument("--jd", default="", help="自定义 JD，默认用内置样例")
    args = parser.parse_args()

    if args.force_fail:
        real_chat_json = agent_graph.chat_json
        calls = {"review": 0}

        def wrapped(system_prompt, user_prompt, **kwargs):
            if "审核员" in system_prompt:
                calls["review"] += 1
                if calls["review"] == 1:
                    print("[demo] 已注入一次不合格评审，模拟首轮被打回", flush=True)
                    return dict(FORCED_FAIL_REVIEW)
            return real_chat_json(system_prompt, user_prompt, **kwargs)

        agent_graph.chat_json = wrapped  # type: ignore[assignment]

    try:
        result = agent_graph.analyze_with_review(args.title, args.jd or SAMPLE_JD)
    except Exception as exc:  # 终端脚本，直接把原因打全
        print(f"\n[demo] 失败：{type(exc).__name__}: {exc}", flush=True)
        return 1

    print("\n===== 最终结果 =====")
    print(f"匹配度：{result.match_score}")
    for index, text in enumerate(result.greetings, start=1):
        print(f"{index}. （{len(text)} 字）{text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
