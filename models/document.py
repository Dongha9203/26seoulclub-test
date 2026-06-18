from dataclasses import dataclass, asdict
from typing import Optional
import uuid
from datetime import datetime, timezone


@dataclass
class Document:
    doc_id: str
    source_type: str        # "notion" | "google_sheet" | "docx" | "pdf" | "excel" | "hwp_converted"
    source_origin: str      # 페이지명 또는 파일명
    title: str
    content: str
    category: str           # 8종 카테고리 또는 "미분류"
    notion_page_url: Optional[str]
    notion_block_id: Optional[str]  # 노션 소스일 때 반드시 존재해야 함 (하이픈 포함 원본 UUID)
    last_updated: str       # ISO 8601
    is_editable: bool       # 노션: False / 나머지: True

    @staticmethod
    def new(
        source_type: str,
        source_origin: str,
        title: str,
        content: str,
        category: str = "미분류",
        notion_page_url: Optional[str] = None,
        notion_block_id: Optional[str] = None,
        is_editable: Optional[bool] = None,
    ) -> "Document":
        return Document(
            doc_id=str(uuid.uuid4()),
            source_type=source_type,
            source_origin=source_origin,
            title=title,
            content=content,
            category=category,
            notion_page_url=notion_page_url,
            notion_block_id=notion_block_id,
            last_updated=datetime.now(timezone.utc).isoformat(),
            is_editable=is_editable if is_editable is not None else (source_type != "notion"),
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["is_editable"] = bool(d["is_editable"])
        return d

    def deep_link_url(self) -> Optional[str]:
        """딥링크 URL: {page_url}#{block_id_without_hyphens}"""
        if self.notion_page_url and self.notion_block_id:
            block_anchor = self.notion_block_id.replace("-", "")
            return f"{self.notion_page_url}#{block_anchor}"
        return None
