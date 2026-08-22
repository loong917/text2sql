"""Build SQL generation and repair prompts."""

from .constants import REFUSAL_TOKEN


def build_generation_prompt(
    question: str,
    grounded_prompt: str,
    attempt: int,
    previous_sql: str | None,
    previous_error: str | None,
) -> str:
    prefix = f"{grounded_prompt}\n\n" if grounded_prompt else ""
    if attempt and previous_error:
        previous = f"上一次 SQL：{previous_sql}\n" if previous_sql else ""
        return (
            f"{prefix}上一次为问题“{question}”生成的 SQL 未通过校验或执行。\n"
            f"{previous}错误：{previous_error}\n请修正 SQL；若真实结构不足，返回 "
            f"{REFUSAL_TOKEN}。只输出单行 SQL。"
        )
    return (
        f"{prefix}【输出要求】只返回单行只读 T-SQL，不要返回 JSON、Markdown 或解释；"
        f"上下文不足时返回 {REFUSAL_TOKEN}。\n\n【用户问题】\n{question}"
    )
