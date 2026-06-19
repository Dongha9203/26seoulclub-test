"""
Claude API 기반 한국어 프롬프트 빌더.

가드레일: 문서 기반 답변 강제 / 신뢰도 낮을 시 불확실성 명시 / 출처(딥링크) 표시.
Claude의 tool use(강제 호출)로 {"answer": str, "sentiment_score": float}를
하나의 API 호출 안에서 함께 받습니다 (추가 API 호출 없음).
"""

import os
from typing import List, Optional, Tuple

import anthropic

from hybrid_search import SearchResult

ANSWER_TOOL = {
    "name": "provide_answer",
    "description": (
        "사용자 질문에 대한 한국어 답변과, 사용자 질문 문장 자체에 담긴 감정 점수를 함께 반환합니다. "
        "answer는 제공된 문서 내용에 근거한 최종 응답 텍스트이고, sentiment_score는 사용자의 "
        "질문 톤이 얼마나 부정적(-1.0)이거나 긍정적(+1.0)인지를 나타내는 값입니다(중립은 0.0)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {
                "type": "string",
                "description": "사용자에게 보여줄 최종 한국어 답변 텍스트",
            },
            "sentiment_score": {
                "type": "number",
                "description": "사용자 질문의 감정 점수, -1.0(매우 부정)에서 1.0(매우 긍정) 사이",
            },
        },
        "required": ["answer", "sentiment_score"],
        "additionalProperties": False,
    },
    "strict": True,
}


def get_anthropic_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")
    return anthropic.Anthropic(api_key=api_key)


def _format_context(search_results: List[SearchResult]) -> str:
    blocks = []
    for i, r in enumerate(search_results, start=1):
        block = f"[문서 {i}] (카테고리: {r.category})\n{r.content}"
        if r.source_type == "notion":
            link = r.deep_link_url()
            block += f"\n(딥링크: {link}, 페이지명: {r.title})"
        blocks.append(block)
    return "\n\n".join(blocks)


def build_system_prompt(tone_instruction: str, search_results: List[SearchResult],
                         low_confidence: bool, operation_team_contact: Optional[str] = None) -> str:
    """가드레일 + 톤 지침 + 검색된 문서 컨텍스트를 합쳐 시스템 프롬프트를 생성합니다.

    operation_team_contact를 넘기면(정보부재 상황) 운영팀 실제 연락처를 가드레일에
    그대로 박아 넣고 절대 다른 연락처를 지어내지 못하게 합니다.
    """
    has_notion_source = any(r.source_type == "notion" for r in search_results)

    guardrails = [
        "[가드레일]",
        "- 반드시 아래 제공된 문서 내용에만 근거해 답변하세요. 문서에 없는 내용은 추측하지 마세요.",
        "- 답변에는 어떤 문서를 참고했는지 알 수 있도록 자연스럽게 출처를 언급하세요.",
    ]
    if low_confidence:
        guardrails.append("- 제공된 문서가 질문과 완전히 일치하지 않을 수 있습니다. "
                           "확실하지 않은 부분은 불확실성을 명시적으로 밝히세요.")
    if operation_team_contact:
        guardrails.append(
            "- 운영팀 연락처를 안내해야 합니다. 아래 연락처를 한 글자도 바꾸지 말고 정확히 "
            "그대로 사용하세요. 절대 다른 연락 채널을 지어내지 마세요:\n" + operation_team_contact
        )
    if has_notion_source:
        guardrails.append(
            "- 참고한 문서 중 노션 소스가 있다면, 답변의 맨 마지막에 반드시 "
            "\"(자세한 내용: [페이지명] 바로가기)\" 형식으로 해당 문서의 딥링크를 "
            "마크다운 링크로 포함하세요. 이는 선택이 아니라 필수입니다. "
            "예: (자세한 내용: [FAQ 바로가기](딥링크주소))"
        )

    parts = [
        tone_instruction,
        "\n".join(guardrails),
        "[참고 문서]\n" + (_format_context(search_results) if search_results else "(없음)"),
    ]
    return "\n\n".join(parts)


def call_claude(client: anthropic.Anthropic, model: str, system_prompt: str,
                 question: str) -> Tuple[str, float]:
    """Claude API를 1회 호출해 (answer, sentiment_score)를 반환합니다."""
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": question}],
        tools=[ANSWER_TOOL],
        tool_choice={"type": "tool", "name": "provide_answer"},
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "provide_answer":
            data = block.input
            return data["answer"], float(data["sentiment_score"])

    raise RuntimeError("Claude 응답에서 provide_answer tool_use 블록을 찾지 못했습니다.")
