"""结构化确认字段校验，供 API 与内部检索共同使用。"""
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question: str = Field(min_length=1, max_length=8000)
    index_id: UUID | None = None
    category_id: str | None = None
    purchased_at: datetime | None = None
    after_sales_applied_at: datetime | None = None

    @field_validator('question')
    @classmethod
    def question_nonempty(cls, value):
        if not value.strip():
            raise ValueError('问题不能为空')
        return value.strip()

    @field_validator('category_id')
    @classmethod
    def category_valid(cls, value):
        if value is not None and value not in {'apparel_footwear', 'digital_accessories', 'customized'}:
            raise ValueError('未知商品品类')
        return value

    @field_validator('purchased_at', 'after_sales_applied_at')
    @classmethod
    def timestamp_timezone(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError('时间需要明确时区')
        return value
