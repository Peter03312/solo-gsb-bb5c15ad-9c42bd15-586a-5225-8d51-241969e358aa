"""请求体的 Pydantic 模型（业务校验在路由内完成以给出明确错误码）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class RectIn(BaseModel):
    x: int
    y: int
    w: int
    h: int
    reason: str = Field(max_length=500)


class AnnotationsPut(BaseModel):
    """整体替换当前标注；revision 必须等于服务端当前修订号（乐观锁）。"""

    revision: int = Field(ge=0)
    rects: list[RectIn] = Field(default_factory=list, max_length=1000)


class BatchItem(BaseModel):
    """批次中的一项：文档 ID 与调用方预期的当前标注修订号。"""

    doc_id: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(ge=0)


class BatchPublishRequest(BaseModel):
    """同批次发布请求：2～20 个不同文档，可携带幂等键防重发。

    数量、重复等语义校验在路由内完成，以返回统一格式的业务错误码。
    idempotency_key 为可选、调用方保证全局唯一的键；键与按请求顺序排列的
    (doc_id, expected_revision) 列表绑定。
    """

    documents: list[BatchItem] = Field(default_factory=list)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class WithdrawalRequest(BaseModel):
    """永久撤回某一已发布版本的请求；reason 必须为非空字符串。

    非空的语义校验（含纯空白）在路由内完成，以返回统一的 reason_required
    业务错误码而非 422。
    """

    reason: str = Field(max_length=500)
