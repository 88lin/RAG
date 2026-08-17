"""
数据模型定义（Pydantic v2）
"""

from pydantic import BaseModel, Field
from typing import List, Dict, Optional, Any
from datetime import datetime

# ==================== 请求模型 ====================

class QueryRequest(BaseModel):
    """对话查询请求"""
    question: str = Field(..., description="用户问题", min_length=1, max_length=1000)
    session_id: Optional[str] = Field(None, description="会话ID（前端生成，用于多轮对话）")
    use_retrieval: bool = Field(True, description="是否使用知识库检索（false则只用通用知识回答）")
    enable_multi_query: bool = Field(True, description="是否启用多查询扩展")
    enable_rerank: bool = Field(False, description="是否启用重排序")
    enable_hybrid: bool = Field(True, description="是否启用混合检索")
    enable_citation: bool = Field(True, description="是否启用引用追踪")

    model_config = {
        "json_schema_extra": {
            "examples": [{
                "question": "什么是宠物政策？",
                "use_retrieval": True,
                "enable_multi_query": True,
                "enable_rerank": False,
                "enable_hybrid": True,
                "enable_citation": True
            }]
        }
    }

# ==================== 响应模型 ====================

class ChunkInfo(BaseModel):
    """文档切片信息"""
    id: str = Field(..., description="切片ID")
    content: str = Field(..., description="切片内容")
    index: int = Field(..., description="切片索引")

class DocumentInfo(BaseModel):
    """文档信息（含切片）"""
    file: str = Field(..., description="文件名")
    category: str = Field(..., description="分类")
    chunks: Optional[List[ChunkInfo]] = Field(None, description="文档切片列表（可选）")
    chunk_count: Optional[int] = Field(None, description="文档切片数量")

class CitationInfo(BaseModel):
    """引用信息"""
    chunk_id: Optional[str] = Field(None, description="向量库切片ID")
    doc_id: str = Field(..., description="文档ID")
    file: str = Field(..., description="文件名")
    category: str = Field(..., description="分类")
    content: str = Field(..., description="引用内容")
    relevance: float = Field(
        ..., ge=0.0, le=1.0,
        description="相关性，[0,1] 越大越相关，由 rag.scoring 统一计算"
    )

class QueryResponse(BaseModel):
    """对话查询响应"""
    session_id: str = Field(..., description="会话ID")
    question: str = Field(..., description="原始问题")
    resolved_question: Optional[str] = Field(None, description="消解后的问题")
    answer: str = Field(..., description="回答内容")
    citations: List[CitationInfo] = Field(default_factory=list, description="引用列表")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="元数据")

class DocumentListResponse(BaseModel):
    """文档列表响应"""
    documents: List[DocumentInfo] = Field(..., description="文档列表")
    total: int = Field(..., description="总数")


# ==================== 阈值配置 ====================

class ThresholdConfig(BaseModel):
    """相关性阈值。前端据此给分数配色与分档，**不再自己硬编码**。

    此前两个 Vue 组件各自写死了一份，且值互不相同（50 与 0.75），
    而后端实际生效的是第三个值 —— 同一个 relevance 在仪表盘显示蓝色
    "足以支撑回答"、在引用卡片显示橙色，后端却判定为不可答。

    跨进程的常量副本没有任何编译器会报警，唯一可靠的办法是只有一份、
    运行时下发。
    """

    retrieval_min: float = Field(
        ..., description="低于此相关性的片段不进 prompt 上下文", ge=0.0, le=1.0
    )
    answerable_min: float = Field(
        ..., description="top1 低于此相关性则判定知识库无答案", ge=0.0, le=1.0
    )

    model_config = {
        "json_schema_extra": {
            "example": {"retrieval_min": 0.35, "answerable_min": 0.75}
        }
    }

# ==================== 错误响应模型 ====================

class ErrorDetail(BaseModel):
    """错误详情"""
    message: str = Field(..., description="错误消息")
    type: str = Field(..., description="错误类型")
    detail: Optional[str] = Field(None, description="详细信息")

class ErrorResponse(BaseModel):
    """统一错误响应"""
    error: ErrorDetail = Field(..., description="错误详情")
    status_code: int = Field(..., description="HTTP状态码")
    path: Optional[str] = Field(None, description="请求路径")
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat(), description="时间戳")

    model_config = {
        "json_schema_extra": {
            "example": {
                "error": {
                    "message": "Rate limit exceeded",
                    "type": "RateLimitError",
                    "detail": "Too many requests. Please try again later."
                },
                "status_code": 429,
                "path": "/api/v1/chat/message",
                "timestamp": "2026-02-11T12:00:00Z"
            }
        }
    }
