"""
流式摄入适配器
将 DocumentIngestion 核心功能适配为 SSE 流式输出
不修改核心模块，在适配器层实现可观测性
"""

import sys
from pathlib import Path
from typing import Dict, AsyncIterator, Any
import asyncio

# 添加项目根目录到路径
sys.path.append(str(Path(__file__).parent.parent.parent))

from rag import DocumentIngestion
from rag.chunker import chunk_with_metadata
from rag.ingestion import build_document_id, read_document_file
from rag.logger import get_logger

logger = get_logger(__name__)


class StreamingIngestionAdapter:
    """
    流式摄入适配器

    职责：
    - 包装 DocumentIngestion 核心功能
    - 提供 SSE 流式进度输出
    - 不修改核心 RAG 模块
    - 遵循六边形架构原则

    事件类型（12种）：
    1. file_received      - 文件接收
    2. parsing_start      - 开始解析
    3. parsing_done       - 解析完成
    4. chunking_start     - 开始分块
    5. chunking_done      - 分块完成
    6. embedding_start    - 开始 Embedding
    7. embedding_progress - Embedding 进度
    8. embedding_done     - Embedding 完成
    9. storing_start      - 开始存储
    10. storing_done      - 存储完成
    11. indexing_done     - 索引更新完成
    12. upload_complete   - 上传完成
    """

    def __init__(self, ingestion: DocumentIngestion):
        """
        初始化流式摄入适配器

        参数:
            ingestion: DocumentIngestion - 核心摄入器实例
        """
        self.ingestion = ingestion
        logger.info("StreamingIngestionAdapter 初始化完成")

    async def ingest_file_stream(
        self,
        file_path: str,
        filename: str,
        category: str = "uploaded"
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        流式摄入单个文件

        通过调用核心模块的各个独立函数，在它们之间插入 SSE 事件。
        不修改核心模块，保持架构纯净。

        参数:
            file_path: str - 临时文件路径
            filename: str - 原始文件名
            category: str - 文档类别

        返回:
            AsyncIterator[Dict] - SSE 事件流

        异常:
            FileNotFoundError - 文件不存在
            UnicodeDecodeError - 文件编码错误
            Exception - 其他处理错误
        """
        file_path_obj = Path(file_path)

        try:
            # ========== 阶段 1: 文件接收 ==========
            file_size = file_path_obj.stat().st_size
            yield {
                "type": "file_received",
                "data": {
                    "filename": filename,
                    "size": file_size,
                    "category": category
                }
            }
            logger.info(f"[摄入] 文件接收: {filename} ({file_size} bytes)")

            # ========== 阶段 2: 文件解析 ==========
            yield {
                "type": "parsing_start",
                "data": {"filename": filename}
            }
            logger.info(f"[摄入] 开始解析: {filename}")

            # 读文件是阻塞 I/O，扔进线程池
            try:
                content = await asyncio.to_thread(read_document_file, file_path_obj)
            except UnicodeDecodeError as e:
                logger.error(f"[摄入] 编码错误: {filename} - {e}")
                yield {
                    "type": "error",
                    "data": {
                        "stage": "parsing",
                        "message": f"文件编码错误: {str(e)}"
                    }
                }
                return

            char_count = len(content)
            yield {
                "type": "parsing_done",
                "data": {
                    "filename": filename,
                    "chars": char_count
                }
            }
            logger.info(f"[摄入] 解析完成: {filename} ({char_count} 字符)")

            # ========== 阶段 3: 文本分块 ==========
            yield {
                "type": "chunking_start",
                "data": {"filename": filename}
            }
            logger.info(f"[摄入] 开始分块: {filename}")

            # 分块是纯 CPU（正则 + jieba），大文件上不可忽略
            chunk_pairs = await asyncio.to_thread(chunk_with_metadata, content)
            chunks = [pair[0] for pair in chunk_pairs]
            chunk_metas = [pair[1] for pair in chunk_pairs]
            chunk_count = len(chunks)

            if chunk_count == 0:
                logger.warning(f"[摄入] 文件没有可索引内容: {filename}")
                yield {
                    "type": "error",
                    "data": {
                        "filename": filename,
                        "stage": "chunking",
                        "message": "文件没有可索引内容"
                    }
                }
                return

            yield {
                "type": "chunking_done",
                "data": {
                    "filename": filename,
                    "chunk_count": chunk_count
                }
            }
            logger.info(f"[摄入] 分块完成: {filename} ({chunk_count} chunks)")

            # ========== 阶段 4: 向量化 (Embedding) ==========
            yield {
                "type": "embedding_start",
                "data": {
                    "filename": filename,
                    "total_chunks": chunk_count
                }
            }
            logger.info(f"[摄入] 开始 Embedding: {filename} ({chunk_count} chunks)")

            # 分批处理：批既是进度粒度，也是单次占用线程的时长上限
            batch_size = 32
            all_embeddings = []
            for i in range(0, chunk_count, batch_size):
                batch_chunks = chunks[i:i + batch_size]

                # **必须 to_thread。** encode_documents 是同步 CPU 计算
                # （torch 前向传播），直接调会把事件循环占满整批时长，
                # 期间所有其它请求 —— 包括别人的 SSE 流与 /health —— 全在等。
                #
                # 此处原先是 `await asyncio.sleep(0)` 加一行同步调用，
                # 注释写着"让出控制权，避免阻塞事件循环"。那是无效的：
                # sleep(0) 只在调用**之前**让出一次，紧随其后的同步计算
                # 照样霸占循环。让出控制权不能使阻塞调用变成非阻塞。
                batch_embeddings = await asyncio.to_thread(
                    self.ingestion.embedder.encode_documents,
                    batch_chunks,
                    to_list=True,
                )
                all_embeddings.extend(batch_embeddings)

                # 发送进度事件
                current = min(i + batch_size, chunk_count)
                yield {
                    "type": "embedding_progress",
                    "data": {
                        "filename": filename,
                        "current": current,
                        "total": chunk_count,
                        "percentage": round(current / chunk_count * 100, 1)
                    }
                }
                logger.debug(f"[摄入] Embedding 进度: {current}/{chunk_count}")

            embeddings = all_embeddings

            yield {
                "type": "embedding_done",
                "data": {
                    "filename": filename,
                    "chunk_count": chunk_count
                }
            }
            logger.info(f"[摄入] Embedding 完成: {filename}")

            # ========== 阶段 5: 存储到向量数据库 ==========
            yield {
                "type": "storing_start",
                "data": {"filename": filename}
            }
            logger.info(f"[摄入] 开始存储: {filename}")

            # 准备数据：base 元数据 + 每块的 header 元数据 + 定位元数据
            # doc_key / seq / total_chunks 与 ingestion.ingest_text 保持一致，
            # 两条摄入路径（同步、流式）写入的 metadata 结构必须相同。
            doc_id = build_document_id(filename, category)
            ids = [f"{doc_id}_chunk_{i}" for i in range(chunk_count)]
            metadatas = [
                {
                    "file": filename,
                    "category": category,
                    **chunk_metas[i],
                    "doc_key": doc_id,
                    "seq": i,
                    "total_chunks": chunk_count,
                }
                for i in range(chunk_count)
            ]

            # 先删后插，两步都是阻塞 I/O。
            # 这个顺序也是摄入幂等的来源：重复摄入同一文件不会留下旧切片，
            # 最坏只是白算一次 embedding —— 移除分布式锁的依据就在这。
            await asyncio.to_thread(self.ingestion.vectordb.delete_by_file, filename)
            await asyncio.to_thread(
                self.ingestion.vectordb.add,
                ids=ids,
                embeddings=embeddings,
                documents=chunks,
                metadatas=metadatas,
            )

            yield {
                "type": "storing_done",
                "data": {
                    "filename": filename,
                    "chunk_count": chunk_count
                }
            }
            logger.info(f"[摄入] 存储完成: {filename}")

            # ========== 阶段 6: 索引更新 ==========
            total_docs = await asyncio.to_thread(self.ingestion.vectordb.count)

            yield {
                "type": "indexing_done",
                "data": {
                    "filename": filename,
                    "total_docs_in_db": total_docs
                }
            }
            logger.info(f"[摄入] 索引更新完成: 数据库共 {total_docs} 个文档")

            # ========== 阶段 7: 上传完成 ==========
            yield {
                "type": "upload_complete",
                "data": {
                    "filename": filename,
                    "chunk_count": chunk_count,
                    "success": True
                }
            }
            logger.info(f"[摄入] 上传完成: {filename}")

        except FileNotFoundError as e:
            logger.error(f"[摄入] 文件不存在: {file_path} - {e}")
            yield {
                "type": "error",
                "data": {
                    "stage": "file_access",
                    "message": f"文件不存在: {filename}"
                }
            }

        except Exception as e:
            logger.error(f"[摄入] 处理失败: {filename} - {e}", exc_info=True)
            yield {
                "type": "error",
                "data": {
                    "stage": "unknown",
                    "message": f"处理失败: {str(e)}"
                }
            }

    def __repr__(self):
        return f"StreamingIngestionAdapter(ingestion={self.ingestion})"
