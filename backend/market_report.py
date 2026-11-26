"""批量市场趋势分析：把 jobs.jsonl 里积累的岗位"读薄"成一份可执行的简历修改清单。

流程：
1. 逐条读取历史岗位的 JD（并发调用，单条失败不影响整体）
2. 让 DeepSeek 把每条 JD 归一化成标准技能标签
3. 汇总高频技能 Top N，对照 backend/resume.md 找出没覆盖的关键词
4. 让模型给出针对性的简历修改建议，落盘成 Markdown

用法：
    python backend/market_report.py
    python backend/market_report.py --top 15 --concurrency 4
    python backend/market_report.py --input other.jsonl --output other_report.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

try:  # 作为包导入（python -m backend.market_report）
    from .llm_service import (
        RESUME_PATH,
        ConfigurationError,
        LLMServiceError,
        chat_json,
        extract_skills,
        load_resume,
    )
except ImportError:  # 直接运行脚本（python backend/market_report.py）
    from llm_service import (
        RESUME_PATH,
        ConfigurationError,
        LLMServiceError,
        chat_json,
        extract_skills,
        load_resume,
    )

BACKEND_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = BACKEND_DIR / "data" / "jobs.jsonl"
DEFAULT_OUTPUT = BACKEND_DIR / "data" / "market_report.md"
MIN_JD_LEN = 20

SUGGESTION_SYSTEM_PROMPT = """你是简历顾问，服务对象是正在找技术岗的候选人。

只输出 JSON：{"suggestions": ["...", "..."]}

要求：
1. 给 3-5 条建议，每条 ≤80 字，必须具体到"往简历里加什么"或"把哪句改写成什么样"，例如"把『负责客服知识库』改成『用 Milvus + 混合检索把答案准确率从 61% 提到 78%』"。
2. 优先围绕简历缺失的高频技能给建议，但不要建议编造经历；缺经历时建议用最小可交付项目补，并说明能写进简历的形式。
3. 按"改哪里 → 怎么改 → 为什么"的顺序表达，不要空话。
4. 全部简体中文，不要 emoji。"""

SUGGESTION_USER_TEMPLATE = """【高频技能及出现次数】
{top_skills}

【简历中缺失的高频技能】
{missing}

【当前简历（节选）】
{resume}

请按要求输出 JSON。"""


def text_mentions(text: str, skill: str) -> bool:
    """技能是否在文本中出现过。纯英文技能加词边界，避免 SQL 命中 MySQL。"""
    if re.search(r"[A-Za-z]", skill):
        pattern = r"(?<![A-Za-z0-9+#.])" + re.escape(skill) + r"(?![A-Za-z0-9+#])"
        return re.search(pattern, text, re.IGNORECASE) is not None
    return skill in text


def find_missing(
    ranked: list[tuple[str, int]],
    resume_skills: list[str],
    resume_text: str,
    *,
    limit: int = 15,
) -> list[tuple[str, int]]:
    """Top 技能里简历没覆盖的部分。

    双重判定：模型抽取的技能集 + 原文直接命中。后者是兜底——
    模型偶尔漏抽（例如简历里写「RAG（LangChain）」却没被标出来），
    只信模型会把已具备的能力误判成缺口，进而给出错误建议。
    """
    known = {skill.casefold() for skill in resume_skills}
    missing: list[tuple[str, int]] = []
    for name, count in ranked[:limit]:
        if name.casefold() in known:
            continue
        if text_mentions(resume_text, name):
            continue
        missing.append((name, count))
    return missing


def load_records(path: Path) -> list[dict]:
    """逐行读取 jsonl，跳过坏行；只保留有 JD 正文的记录。"""
    if not path.exists():
        raise FileNotFoundError(f"找不到输入文件：{path}")

    records: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            print(f"  ! 第 {lineno} 行不是合法 JSON，已跳过", file=sys.stderr)
            continue
        jd_text = (record.get("jd_text") or "").strip()
        if len(jd_text) < MIN_JD_LEN:
            continue
        records.append(record)
    return records


def collect_skills(records: list[dict], concurrency: int) -> tuple[dict[int, list[str]], list[str]]:
    """并发抽取技能。返回 {记录下标: 技能列表} 与失败信息列表。"""
    per_record: dict[int, list[str]] = {}
    failures: list[str] = []
    total = len(records)

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {
            pool.submit(extract_skills, record.get("jd_text", "")): index
            for index, record in enumerate(records)
        }
        for done, future in enumerate(as_completed(futures), 1):
            index = futures[future]
            title = (records[index].get("job_title") or "未命名岗位")[:24]
            try:
                skills = future.result()
                per_record[index] = skills
                print(f"  [{done}/{total}] {title} → {len(skills)} 个技能：{'、'.join(skills[:5])}")
            except (LLMServiceError, ConfigurationError) as exc:
                failures.append(f"{title}：{exc}")
                print(f"  [{done}/{total}] {title} → 失败：{exc}")

    return per_record, failures


def build_suggestions(
    top_skills: list[tuple[str, int]], missing: list[tuple[str, int]], resume_text: str
) -> list[str]:
    """让模型给建议；失败则退化成可执行的模板建议，保证报告不空。"""
    formatted_top = "、".join(f"{name}({count})" for name, count in top_skills) or "（无数据）"
    formatted_missing = "、".join(name for name, _ in missing) or "（无缺口）"

    try:
        payload = chat_json(
            SUGGESTION_SYSTEM_PROMPT,
            SUGGESTION_USER_TEMPLATE.format(
                top_skills=formatted_top,
                missing=formatted_missing,
                resume=resume_text[:3000],
            ),
            max_tokens=900,
            temperature=0.7,
        )
        raw = payload.get("suggestions")
        if isinstance(raw, list):
            suggestions = [item.strip() for item in raw if isinstance(item, str) and item.strip()]
            if suggestions:
                return suggestions[:5]
    except (LLMServiceError, ConfigurationError) as exc:
        print(f"  ! 建议生成失败，改用模板建议：{exc}", file=sys.stderr)

    return [
        f"为「{name}」补一段可验证的经历：哪怕是一个小项目，也写清用什么、怎么做、达到什么指标。"
        for name, _ in missing[:3]
    ] or ["当前简历已覆盖全部高频技能，建议补充量化数据，把「做了什么」改写成「做到了什么程度」。"]


def render_report(
    *,
    input_path: Path,
    records: list[dict],
    per_record: dict[int, list[str]],
    failures: list[str],
    resume_skills: list[str],
    resume_text: str,
    top_n: int,
    suggestions: list[str],
) -> str:
    counter: Counter[str] = Counter()
    for skills in per_record.values():
        counter.update(set(skills))  # 同一岗位内重复只计一次

    success = len(per_record)
    total = len(records)
    ranked = counter.most_common()
    top_skills = ranked[:top_n]

    missing = find_missing(ranked, resume_skills, resume_text)

    def pct(count: int) -> str:
        return f"{count / success * 100:.0f}%" if success else "-"

    lines: list[str] = []
    lines.append("# BossCopilot 市场趋势报告")
    lines.append("")
    lines.append(
        f"> 生成时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}　"
        f"样本：{total} 条岗位（技能抽取成功 {success} 条"
        + (f"，失败 {len(failures)} 条" if failures else "")
        + f"）　数据源：`{input_path}`"
    )
    lines.append("")

    lines.append(f"## 一、高频技能 Top {top_n}")
    lines.append("")
    if top_skills:
        lines.append("| # | 技能 | 出现次数 | 岗位覆盖率 |")
        lines.append("|--:|---|--:|--:|")
        for rank, (name, count) in enumerate(top_skills, 1):
            lines.append(f"| {rank} | {name} | {count} | {pct(count)} |")
    else:
        lines.append("样本不足，暂无可统计的技能。")
    lines.append("")

    lines.append("## 二、简历缺口")
    lines.append("")
    if resume_skills:
        lines.append(f"简历已识别技能（{len(resume_skills)} 个）：{'、'.join(resume_skills)}")
        lines.append("")
    if missing:
        lines.append(f"Top 15 中，以下 {len(missing)} 个关键词未出现在 `resume.md`：")
        lines.append("")
        lines.append("| 技能 | 出现次数 | 岗位覆盖率 |")
        lines.append("|---|--:|--:|")
        for name, count in missing:
            lines.append(f"| {name} | {count} | {pct(count)} |")
    else:
        lines.append("Top 15 高频技能均已在简历中出现，缺的是量化表达和项目深度。")
    lines.append("")

    lines.append("## 三、简历修改建议")
    lines.append("")
    for index, suggestion in enumerate(suggestions, 1):
        lines.append(f"{index}. {suggestion}")
    lines.append("")

    lines.append("## 附：样本岗位")
    lines.append("")
    for record in records[:20]:
        title = record.get("job_title") or "未命名岗位"
        stamp = (record.get("timestamp") or "")[:16].replace("T", " ")
        lines.append(f"- {title}　{stamp}")
    if len(records) > 20:
        lines.append(f"- ……其余 {len(records) - 20} 条已省略")
    lines.append("")

    if failures:
        lines.append("## 附：抽取失败的岗位")
        lines.append("")
        for item in failures:
            lines.append(f"- {item}")
        lines.append("")

    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量分析历史岗位，生成市场趋势报告")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="jobs.jsonl 路径")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="报告输出路径")
    parser.add_argument("--top", type=int, default=10, help="高频技能取前几名，默认 10")
    parser.add_argument("--concurrency", type=int, default=3, help="并发请求数，默认 3")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        records = load_records(args.input)
    except FileNotFoundError as exc:
        print(f"× {exc}\n  先跑几次 /api/analyze 攒点数据再来。", file=sys.stderr)
        return 1

    if not records:
        print(f"× {args.input} 里没有可分析的岗位（JD 需 ≥{MIN_JD_LEN} 字）。", file=sys.stderr)
        return 1

    print(f"读取 {len(records)} 条岗位，开始抽取技能（并发 {args.concurrency}）…")
    per_record, failures = collect_skills(records, args.concurrency)

    if not per_record:
        print("× 全部岗位抽取失败，检查 DEEPSEEK_API_KEY 与网络后重试。", file=sys.stderr)
        return 1

    try:
        resume_text = load_resume()
    except ConfigurationError as exc:
        print(f"× {exc}", file=sys.stderr)
        return 1

    print("解析简历技能…")
    resume_skills: list[str] = []
    try:
        resume_skills = extract_skills(resume_text, max_skills=40)
        print(f"  简历技能（{len(resume_skills)} 个）：{'、'.join(resume_skills) or '（未识别）'}")
    except (LLMServiceError, ConfigurationError) as exc:
        print(f"  ! 简历解析失败，缺口对比将不准确：{exc}", file=sys.stderr)

    counter: Counter[str] = Counter()
    for skills in per_record.values():
        counter.update(set(skills))
    missing = find_missing(counter.most_common(), resume_skills, resume_text)

    print("生成简历修改建议…")
    suggestions = build_suggestions(counter.most_common(args.top), missing, resume_text)

    report = render_report(
        input_path=args.input,
        records=records,
        per_record=per_record,
        failures=failures,
        resume_skills=resume_skills,
        resume_text=resume_text,
        top_n=args.top,
        suggestions=suggestions,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")

    print(f"\n✓ 报告已生成：{args.output}")
    print(f"  样本 {len(records)} 条｜成功 {len(per_record)} 条｜失败 {len(failures)} 条")
    if counter:
        top = "、".join(f"{name}({count})" for name, count in counter.most_common(5))
        print(f"  高频技能前 5：{top}")
    if missing:
        print(f"  简历缺失：{'、'.join(name for name, _ in missing[:5])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
