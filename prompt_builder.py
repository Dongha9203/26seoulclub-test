"""
Claude API 기반 한국어 프롬프트 빌더.

가드레일: 문서 기반 답변 강제 / 신뢰도 낮을 시 불확실성 명시 / 출처(딥링크) 표시.
Claude의 tool use(강제 호출)로 {"answer": str, "sentiment_score": float}를
하나의 API 호출 안에서 함께 받습니다 (추가 API 호출 없음).
"""

import json
import os
from typing import List, Optional, Tuple

import anthropic

from hybrid_search import SearchResult

ANSWER_TOOL = {
    "name": "provide_answer",
    "description": (
        "사용자 질문에 대한 한국어 답변과, 사용자 질문 문장 자체에 담긴 감정 점수, "
        "그리고 이 답변이 실제로 질문을 해결했는지를 함께 반환합니다. "
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
            "resolution_status": {
                "type": "string",
                "enum": ["해결됨", "검색실패", "지식DB공백", "정책밖요청"],
                "description": (
                    "이 답변이 실제로 질문을 해결했는지 자체 판단. "
                    "'해결됨': 제공된 문서로 질문에 실질적인 정보를 답변함. "
                    "'검색실패': 서울 동아리ON 운영과 명백히 관련된 질문이지만, 제공된 문서에 "
                    "구체적인 답을 찾을 수 없어 운영팀 안내로 답변함. "
                    "'지식DB공백': 서울 동아리ON과 관련은 있어 보이나, 제공된 문서가 전혀 다루지 "
                    "않는 주제라 운영팀 안내로 답변함. "
                    "'정책밖요청': 서울 동아리ON 운영과 무관한 질문(날씨, 요리, 일반 상식, 다른 "
                    "서비스 요청 등)이라 답변 대상이 아니라고 안내함."
                ),
            },
        },
        "required": ["answer", "sentiment_score", "resolution_status"],
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

    operation_team_contact를 넘기면 운영팀 실제 연락처를 가드레일에 그대로 박아 넣고,
    문서 안에 다른(예: 옛 노션 페이지에 남은 오래된) 연락처가 적혀 있어도 이 값을
    우선하도록 강제합니다. 신뢰도와 무관하게 항상 넘겨야 합니다 — 신뢰도가 높아
    문서를 그대로 인용하는 답변이라도, 그 문서 안에 연락처가 섞여 있으면 대시보드
    설정과 어긋난 옛 정보가 노출될 수 있기 때문입니다.
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
            "- 운영팀 연락처(전화/이메일/주소/운영시간)를 답변에 언급해야 하는 경우, "
            "아래 연락처를 한 글자도 바꾸지 말고 정확히 그대로 사용하세요. 절대 다른 "
            "연락 채널을 지어내지 마세요. 문서 안에 다른(예: 오래된) 연락처가 적혀 "
            "있어도 그대로 베끼지 말고 무시하세요:\n" + operation_team_contact
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
                 question: str) -> Tuple[str, float, str]:
    """Claude API를 1회 호출해 (answer, sentiment_score, resolution_status)를 반환합니다."""
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
            return data["answer"], float(data["sentiment_score"]), data["resolution_status"]

    raise RuntimeError("Claude 응답에서 provide_answer tool_use 블록을 찾지 못했습니다.")


def _try_extract_partial_answer(raw_json_buffer: str) -> Optional[str]:
    """누적된 raw JSON 텍스트(아직 닫히지 않았을 수 있음)에서 "answer" 문자열 값을
    최대한 떼어내봅니다. 그대로 파싱이 안 되면(아직 닫는 인용부호 전이라서) 인용부호와
    중괄호를 임시로 붙여 다시 시도합니다 — answer가 ANSWER_TOOL 스키마의 첫 필드라
    스트림 초반에는 거의 항상 이 두 형태 중 하나로 유효하게 닫힙니다."""
    for candidate in (raw_json_buffer, raw_json_buffer + '"}'):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("answer"), str):
            return data["answer"]
    return None


def call_claude_stream(client: anthropic.Anthropic, model: str, system_prompt: str, question: str):
    """call_claude()의 스트리밍 버전.

    답변 텍스트가 생성되는 대로 ("delta", 텍스트조각)을 여러 번 yield하고,
    마지막에 ("done", (answer, sentiment_score, resolution_status))를 한 번 yield합니다.

    강제 tool_choice로 받는 구조화된 JSON({"answer": ..., "sentiment_score": ...,
    "resolution_status": ...}) 중 "answer" 필드만 실시간으로 떼어내 보여줘야 합니다.
    SDK가 제공하는 InputJsonEvent.snapshot은 jiter의 partial_mode=True를 쓰는데,
    이 모드는 아직 닫히지 않은 문자열 값은 누락시켜서(완전히 닫힌 필드만 반영)
    실시간 스트리밍에는 못 씁니다(실측: 스트림 끝나기 전까지 항상 빈 문자열).
    그래서 raw partial_json 텍스트를 직접 누적해 우리가 직접 부분 파싱합니다.
    """
    raw_json_buffer = ""
    emitted = ""
    with client.messages.stream(
        model=model,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": question}],
        tools=[ANSWER_TOOL],
        tool_choice={"type": "tool", "name": "provide_answer"},
    ) as stream:
        for event in stream:
            if event.type != "input_json":
                continue
            raw_json_buffer += event.partial_json
            partial_answer = _try_extract_partial_answer(raw_json_buffer)
            if partial_answer is not None and len(partial_answer) > len(emitted):
                yield ("delta", partial_answer[len(emitted):])
                emitted = partial_answer
        final_message = stream.get_final_message()

    for block in final_message.content:
        if block.type == "tool_use" and block.name == "provide_answer":
            data = block.input
            full_answer = data["answer"]
            if len(full_answer) > len(emitted):
                yield ("delta", full_answer[len(emitted):])
            yield ("done", (full_answer, float(data["sentiment_score"]), data["resolution_status"]))
            return

    raise RuntimeError("Claude 응답에서 provide_answer tool_use 블록을 찾지 못했습니다.")
