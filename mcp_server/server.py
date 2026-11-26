"""BossCopilot 本地 MCP Server。

把 BossCopilot 已经积累的求职数据通过 Model Context Protocol 暴露给任意
MCP 客户端（Claude Desktop / Cursor / WorkBuddy 等），让模型可以直接回答
「我分析过哪些岗位」「这条 JD 我到底差在哪」「我的简历现在写的什么」。

数据全部留在本机，服务本身不发起任何网络请求：

    backend/data/jobs.jsonl   后端每次 /api/analyze 追加一行分析结果
    backend/resume.md         当前简历（在浏览器插件里配置后由后端写入）

两个路径都基于 __file__ 推导，因此无论从哪个工作目录启动服务，都能定位到
项目里的同一份数据，不依赖 os.getcwd()。

运行方式（默认 stdio，由 MCP 客户端拉起）：
    python mcp_server/server.py

安装依赖：
    pip install -r mcp_server/requirements.txt

注意：stdio 传输下 stdout 是 JSON-RPC 通道，任何调试输出都必须走 stderr，
本文件因此不使用 print()。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

try:  # mcp >= 2.0：FastMCP 更名为 MCPServer，模块迁到 mcp.server.mcpserver
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x：经典的 FastMCP
    from mcp.server.fastmcp import FastMCP as _Server

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------

MCP_SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MCP_SERVER_DIR.parent

JOBS_LOG = PROJECT_ROOT / "backend" / "data" / "jobs.jsonl"
RESUME_PATH = PROJECT_ROOT / "backend" / "resume.md"

# 单次列表最多返回多少条，避免模型一次拉爆上下文
MAX_LIST_LIMIT = 100

mcp = _Server("BossCopilot-MCP")


# ---------------------------------------------------------------------------
# 数据读取与格式化（工具函数的公共底座）
# ---------------------------------------------------------------------------


def _parse_time(record: dict) -> datetime | None:
    """解析记录里的 ISO 8601 时间戳，解析不了就返回 None。"""
    raw = str(record.get("timestamp") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _epoch(record: dict) -> float:
    """把时间戳折成可排序的数字，坏数据统一落到 0，保证排序不抛异常。"""
    dt = _parse_time(record)
    if dt is None:
        return 0.0
    try:
        return dt.timestamp()
    except (OSError, OverflowError, ValueError):
        return 0.0


def _format_time(record: dict) -> str:
    dt = _parse_time(record)
    if dt is not None:
        return dt.strftime("%Y-%m-%d %H:%M")
    return str(record.get("timestamp") or "未知时间")


def _load_records() -> list[dict]:
    """读取 jobs.jsonl，按分析时间正序返回记录列表。

    - 文件不存在返回空列表，由调用方给出提示；
    - 空行、坏 JSON、非对象行一律跳过，不让一行脏数据毁掉整个查询；
    - 每条记录补上 _index（1 为最早一条）与 _lineno（原始行号）。
    """
    if not JOBS_LOG.exists():
        return []

    records: list[dict] = []
    try:
        content = JOBS_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    for lineno, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        record["_lineno"] = lineno
        records.append(record)

    records.sort(key=lambda r: (_epoch(r), r["_lineno"]))
    for index, record in enumerate(records, 1):
        record["_index"] = index
    return records


def _result_of(record: dict) -> dict:
    """取出 result 子对象，缺失或类型不对时给个空 dict。"""
    result = record.get("result")
    return result if isinstance(result, dict) else {}


def _score_of(record: dict) -> int:
    """把 match_score 转成 0-100 的整数，异常值按 0 处理。"""
    raw = _result_of(record).get("match_score", 0)
    try:
        score = int(round(float(raw)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, score))


def _str_list_of(record: dict, key: str) -> list[str]:
    """取出 pros / gaps / greetings 这类字符串数组，过滤掉空项。"""
    items = _result_of(record).get(key) or []
    if not isinstance(items, list):
        return []
    return [str(item).strip() for item in items if str(item).strip()]


def _title_of(record: dict) -> str:
    return str(record.get("job_title") or "（未命名岗位）").strip()


def _missing_log_hint() -> str:
    """jobs.jsonl 不存在或没有有效记录时的提示，区分「没有文件」和「文件是空的」。"""
    relative = JOBS_LOG.relative_to(PROJECT_ROOT).as_posix()
    state = "还是空的" if JOBS_LOG.exists() else "不存在"
    return (
        f"还没有任何分析记录：{relative} {state}。\n"
        "先在 BossCopilot 插件里分析一个岗位，结果会自动追加到这个文件，然后重试。"
    )


# ---------------------------------------------------------------------------
# 短板主题归类
# ---------------------------------------------------------------------------

# 短板是模型自由生成的句子，没有固定枚举，因此按关键词归成可统计的主题。
# 列表顺序即匹配优先级：一句短板只会归入第一个命中的主题，否则「金融行业经验缺失」
# 会同时算进「金融」和「行业经验」两个主题，把计数刷虚。
# 越泛化的主题越往后放，让具体主题优先吃到自己的句子。
GAP_THEMES: list[tuple[str, tuple[str, ...]]] = [
    ("金融 / 保险行业经验", ("金融", "保险", "银行", "证券", "信贷", "理赔", "核保", "风控")),
    ("MCP 协议与 Agent 工具编排", ("MCP", "Skill", "工具编排", "Agent 编排", "LangGraph", "Dify", "智能体")),
    ("RAG 评测与向量检索", ("RAG", "向量", "召回", "Milvus", "FAISS", "pgvector", "embedding", "检索")),
    ("数据仓库与指标 / ETL", ("数仓", "数据仓库", "ETL", "Hive", "Spark", "Flink", "维度建模", "数据治理", "指标口径")),
    ("容器化与云原生运维", ("Kubernetes", "K8s", "Docker", "容器化", "GitOps", "Argo")),
    ("企业级系统集成", ("系统集成", "集成方案", "ERP", "CRM", "OA", "中间件", "消息队列", "RPC")),
    ("高并发与可观测性", ("高并发", "分布式", "可观测", "链路追踪", "监控告警", "灰度", "QPS", "压测")),
    ("To B 客户现场交付", ("To B", "现场", "驻场", "售前", "客户方", "干系人")),
    ("面向业务的报告与洞察", ("分析报告", "洞察", "管理层", "业务归因", "业务结论", "汇报")),
    ("垂直行业经验", ("行业经验", "行业背景", "领域经验", "业务理解", "行业理解")),
]


def _clip(text: str, width: int = 42) -> str:
    """截断过长句子用于示例展示，按字符数截，不切坏语义。"""
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= width else text[:width] + "…"


def _gap_theme_stats(records: list[dict]) -> list[tuple[str, int, str]]:
    """统计每个主题被多少条记录命中，返回 (主题, 命中记录数, 一句示例)，按命中数降序。

    每条短板只归入第一个命中的主题（GAP_THEMES 的顺序即优先级），
    同一记录在同一主题下只计一次，避免一句话被多个泛化主题重复统计。
    """
    counts = [0] * len(GAP_THEMES)
    examples = [""] * len(GAP_THEMES)

    for record in records:
        hit_indexes: set[int] = set()
        for gap in _str_list_of(record, "gaps"):
            lowered = gap.lower()
            for index, (_, keywords) in enumerate(GAP_THEMES):
                if any(keyword.lower() in lowered for keyword in keywords):
                    hit_indexes.add(index)
                    if not examples[index]:
                        examples[index] = _clip(gap)
                    break
        for index in hit_indexes:
            counts[index] += 1

    stats = [
        (label, counts[index], examples[index])
        for index, (label, _) in enumerate(GAP_THEMES)
        if counts[index]
    ]
    stats.sort(key=lambda item: item[1], reverse=True)
    return stats


# ---------------------------------------------------------------------------
# MCP 工具
# ---------------------------------------------------------------------------


@mcp.tool()
def list_saved_jobs(min_score: int = 0, limit: int = 20) -> str:
    """列出历史分析过的岗位摘要（序号、岗位名、匹配分、分析时间）。

    按分析时间倒序返回，最新分析的排在最前面。序号按分析时间正序编排，
    1 代表最早的一条，可用于对照 jobs.jsonl 里的记录。

    参数：
        min_score: 最低匹配分，0-100。低于该分数的记录会被过滤掉，默认 0 即不过滤。
        limit: 最多返回多少条，默认 20，超过 100 按 100 处理。

    想看某条岗位的完整 JD、匹配优势、短板和打招呼语，用 get_job_detail 按岗位名关键词查询。
    """
    records = _load_records()
    if not records:
        return _missing_log_hint()

    try:
        min_score = int(min_score)
    except (TypeError, ValueError):
        min_score = 0
    min_score = max(0, min(100, min_score))

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(MAX_LIST_LIMIT, limit))

    matched = [r for r in records if _score_of(r) >= min_score]
    if not matched:
        best = max(_score_of(r) for r in records)
        return (
            f"没有匹配分 ≥ {min_score} 的记录。\n"
            f"当前共 {len(records)} 条分析记录，最高分是 {best} 分，可以调低 min_score 再试。"
        )

    # 最新的排前面
    shown = sorted(matched, key=lambda r: (_epoch(r), r["_lineno"]), reverse=True)[:limit]

    lines = [
        f"共 {len(records)} 条分析记录，其中 {len(matched)} 条匹配分 ≥ {min_score}，"
        f"按分析时间倒序显示 {len(shown)} 条：",
        "",
    ]
    for record in shown:
        lines.append(
            f"#{record['_index']:<2} | {_score_of(record):>3} 分 | "
            f"{_format_time(record)} | {_title_of(record)}"
        )

    if len(matched) > len(shown):
        lines.append("")
        lines.append(f"还有 {len(matched) - len(shown)} 条未显示，可调大 limit 查看更多。")
    lines.append("")
    lines.append('提示：用 get_job_detail(keyword="岗位名关键词") 查看某条的完整分析。')
    return "\n".join(lines)


@mcp.tool()
def get_job_detail(keyword: str) -> str:
    """按岗位名称关键词搜索，返回该岗位的完整 JD、匹配优势、短板和打招呼语。

    关键词不区分大小写，支持中文或英文片段，例如 "RAG"、"FDE"、"大数据"、"运维"。
    同名岗位可能被分析过多次（例如反复调整简历后重跑），此时返回最新的一条完整内容，
    并在末尾列出其他命中记录的摘要，翻页比较时换个更具体的关键词即可。

    参数：
        keyword: 岗位名称关键词，不能为空。留空会返回当前所有可选岗位名。
    """
    records = _load_records()
    if not records:
        return _missing_log_hint()

    needle = str(keyword or "").strip()
    if not needle:
        titles = sorted({_title_of(r) for r in records})
        return (
            "keyword 不能为空，请传入岗位名称关键词。\n"
            f"当前可查询的岗位（{len(titles)} 个）：\n" + "\n".join(f"- {t}" for t in titles)
        )

    lowered = needle.lower()
    hits = [r for r in records if lowered in _title_of(r).lower()]
    if not hits:
        titles = sorted({_title_of(r) for r in records})
        return (
            f'没有岗位名称包含「{needle}」的记录。\n'
            f"当前可查询的岗位（{len(titles)} 个）：\n" + "\n".join(f"- {t}" for t in titles)
        )

    hits.sort(key=lambda r: (_epoch(r), r["_lineno"]), reverse=True)
    record = hits[0]

    blocks: list[str] = [
        f"岗位：{_title_of(record)}",
        f"匹配分：{_score_of(record)} / 100",
        f"分析时间：{_format_time(record)}（记录序号 #{record['_index']}）",
        "",
        "【JD 原文】",
        str(record.get("jd_text") or "（该记录没有保存 JD 原文）").strip(),
    ]

    for title, key in (("匹配优势", "pros"), ("短板与风险", "gaps"), ("打招呼语", "greetings")):
        items = _str_list_of(record, key)
        blocks += ["", f"【{title}】（{len(items)} 条）"]
        if items:
            blocks += [f"{i}. {item}" for i, item in enumerate(items, 1)]
        else:
            blocks.append("（无）")

    if len(hits) > 1:
        blocks += ["", f"另有 {len(hits) - 1} 条记录同样命中「{needle}」，摘要如下："]
        for other in hits[1:]:
            blocks.append(
                f"#{other['_index']} | {_score_of(other):>3} 分 | "
                f"{_format_time(other)} | {_title_of(other)}"
            )
        blocks.append("提示：换更具体的关键词可以精确命中其中一条。")

    return "\n".join(blocks)


@mcp.tool()
def get_my_resume() -> str:
    """读取并返回当前简历 backend/resume.md 的完整内容。

    无参数。返回的是纯文本原文，可用于对照 JD 分析匹配度、改写打招呼语或准备面试。
    如果文件不存在或内容为空，会提示先在 BossCopilot 浏览器插件里配置简历。
    """
    if not RESUME_PATH.exists():
        return (
            f"还没有配置简历：未找到 {RESUME_PATH.relative_to(PROJECT_ROOT).as_posix()}。\n"
            "请先在 BossCopilot 浏览器插件里填入或上传简历，插件会保存到该路径，然后重试。"
        )

    try:
        content = RESUME_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"读取简历失败：{exc}"

    if not content.strip():
        return (
            f"简历文件存在但内容为空：{RESUME_PATH.relative_to(PROJECT_ROOT).as_posix()}。\n"
            "请在 BossCopilot 浏览器插件里补充简历内容后重试。"
        )
    return content


@mcp.tool()
def get_job_stats() -> str:
    """统计求职进展：共分析了多少个岗位、平均匹配度、最高分岗位，以及高频短板汇总。

    无参数。返回四部分：
    - 分析总量：分析次数、去重后的岗位数；
    - 匹配度：平均分（保留一位小数）；
    - 最高分岗位：分数并列时列出全部岗位名；
    - 高频短板 Top 5：按短板文本里的关键词归类统计。每条短板只归入一个主题，
      同一岗位在一个主题下只计一次，所以「7 条记录提及」表示 7 次分析结果里出现了这类短板。
    """
    records = _load_records()
    if not records:
        return _missing_log_hint()

    scores = [_score_of(r) for r in records]
    total = len(records)
    average = sum(scores) / total
    best = max(scores)
    best_titles: list[str] = []
    for record in records:
        title = _title_of(record)
        if _score_of(record) == best and title not in best_titles:
            best_titles.append(title)

    unique_titles = {_title_of(r) for r in records}
    analyzed_days = {_format_time(r)[:10] for r in records if _format_time(r) != "未知时间"}

    lines = [
        "BossCopilot 求职数据统计",
        "=" * 28,
        f"分析总次数：{total} 次",
        f"去重岗位数：{len(unique_titles)} 个",
    ]
    if analyzed_days:
        if len(analyzed_days) == 1:
            lines.append(f"覆盖日期：{next(iter(analyzed_days))}")
        else:
            lines.append(
                f"覆盖日期：{min(analyzed_days)} ~ {max(analyzed_days)}（{len(analyzed_days)} 天）"
            )
    lines += [
        f"平均匹配度：{average:.1f} 分",
        f"最高匹配度：{best} 分 —— {'、'.join(best_titles)}",
    ]

    themes = _gap_theme_stats(records)
    lines += ["", "高频短板 Top 5（按命中的分析记录数统计）"]
    if not themes:
        lines.append("（暂无可归类的短板记录）")
    else:
        for i, (label, hits, example) in enumerate(themes[:5], 1):
            lines.append(f"{i}. {label} —— {hits}/{total} 条记录提及")
            if example:
                lines.append(f"   例：{example}")

    if len(themes) > 5:
        rest = "、".join(label for label, _, _ in themes[5:])
        lines += ["", f"其余主题：{rest}"]

    return "\n".join(lines)


if __name__ == "__main__":
    # stdio 是 MCP 客户端的默认拉起方式，不需要额外参数
    mcp.run()
