from .base import BaseIngestionAdapter, IngestedDoc
from .sec_edgar import SecEdgarAdapter
from .news_rss import NewsRssAdapter
from .pipeline import IngestionPipeline

__all__ = [
    "BaseIngestionAdapter",
    "IngestedDoc",
    "SecEdgarAdapter",
    "NewsRssAdapter",
    "IngestionPipeline"
]
