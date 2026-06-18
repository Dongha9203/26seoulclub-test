import json
import logging
from pathlib import Path
from typing import List, Dict

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.json"


def load_categories(config_path: Path = _CONFIG_PATH) -> List[Dict]:
    """config.json에서 카테고리 목록을 로드합니다."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        return config.get("categories", [])
    except FileNotFoundError:
        logger.warning(f"config.json을 찾을 수 없습니다: {config_path}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"config.json 파싱 오류: {e}")
        return []


def tag_category(title: str, content: str, config_path: Path = _CONFIG_PATH) -> str:
    """
    제목과 본문에 카테고리 키워드가 포함되면 해당 카테고리를 반환합니다.
    여러 카테고리에 매칭되면 먼저 매칭된 카테고리를 우선합니다.
    매칭 없으면 "미분류"를 반환합니다.
    """
    categories = load_categories(config_path)
    if not categories:
        return "미분류"

    combined = title + " " + content

    for cat in categories:
        name = cat.get("name", "")
        keywords: List[str] = cat.get("keywords", [])
        for kw in keywords:
            if kw in combined:
                return name

    return "미분류"
