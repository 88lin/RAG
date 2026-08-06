"""
向量化（Embedding）模块
负责将文本转换为向量表示

查询与文档走两条不同的编码路径：bge 中文系列在训练时对查询侧加了指令前缀、
文档侧不加，该非对称性必须复现，否则检索收益大幅下降。
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

from sentence_transformers import SentenceTransformer
from config import (
    EMBEDDING_MODEL_NAME,
    BATCH_SIZE,
    BGE_ZH_QUERY_PREFIX,
    needs_query_prefix,
)
from typing import List, Union
from .logger import get_logger

# 初始化logger
logger = get_logger(__name__)


class Embedder:
    """
    文本向量化器
    封装 SentenceTransformer，提供查询侧与文档侧两个编码入口
    """

    def __init__(self, model_name=None):
        """
        初始化 Embedder

        参数:
            model_name: str - 模型名称，默认从 config 读取
        """
        self.model_name = model_name or EMBEDDING_MODEL_NAME
        self.use_query_prefix = needs_query_prefix(self.model_name)
        logger.info(
            f"加载 Embedding 模型: {self.model_name}"
            f"（查询前缀: {'启用' if self.use_query_prefix else '不启用'}）"
        )
        self.model = SentenceTransformer(self.model_name)
        logger.info(f"Embedding 模型加载完成，维度 {self.get_embedding_dim()}")

    def encode_query(self, text: str, to_list: bool = True):
        """
        编码检索查询（单条）。

        bge 中文模型需要指令前缀，前缀只加在查询侧，不加在文档侧。
        检索时必须用本方法，不要用 encode_documents。

        参数:
            text: str - 查询文本
            to_list: bool - 是否转为 Python list（ChromaDB 需要）

        返回:
            list[float] 或 ndarray - 单条向量
        """
        payload = f"{BGE_ZH_QUERY_PREFIX}{text}" if self.use_query_prefix else text
        embedding = self.model.encode(
            [payload],
            batch_size=1,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )[0]
        return embedding.tolist() if to_list else embedding

    def encode_documents(
        self,
        texts: Union[str, List[str]],
        to_list: bool = True,
        batch_size: int = None,
    ):
        """
        编码入库文档（批量）。不加查询前缀。

        参数:
            texts: str 或 List[str] - 单个文本或文本列表
            to_list: bool - 是否转为 Python list（ChromaDB 需要）
            batch_size: int - 批处理大小

        返回:
            单个输入返回单条向量，列表输入返回向量列表
        """
        if batch_size is None:
            batch_size = BATCH_SIZE

        is_single = isinstance(texts, str)
        if is_single:
            texts = [texts]

        embeddings = self.model.encode(
            texts,
            batch_size=batch_size,
            show_progress_bar=len(texts) > 10,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )

        if to_list:
            embeddings = embeddings.tolist()

        return embeddings[0] if is_single else embeddings

    def get_embedding_dim(self):
        """获取向量维度"""
        return self.model.get_sentence_embedding_dimension()

    def __repr__(self):
        return f"Embedder(model={self.model_name}, dim={self.get_embedding_dim()})"


if __name__ == "__main__":
    print("=" * 70)
    print("Embedding 模块测试")
    print("=" * 70)

    embedder = Embedder()
    print(f"\n{embedder}\n")

    # 查询侧与文档侧编码同一句话，向量应当不同（前缀生效的证据）
    text = "可以带宠物来公司吗？"
    print(f'测试文本: "{text}"')

    import numpy as np

    q_vec = np.array(embedder.encode_query(text))
    d_vec = np.array(embedder.encode_documents(text))

    print(f"查询向量维度: {len(q_vec)}, L2 范数: {np.linalg.norm(q_vec):.6f}")
    print(f"文档向量维度: {len(d_vec)}, L2 范数: {np.linalg.norm(d_vec):.6f}")

    # 注意：不要在这里输出非 ASCII 符号（如对勾、叉号），
    # Windows 控制台默认 GBK 编码会抛 UnicodeEncodeError。
    if embedder.use_query_prefix:
        cos = float(q_vec @ d_vec)
        print(f"查询/文档向量余弦相似度: {cos:.4f}")
        verdict = "两侧编码不同，前缀已生效" if cos < 0.999 else "两侧编码几乎相同，前缀未生效"
        print(f"结论: {verdict}")
    else:
        print("当前模型不需要查询前缀，两侧编码一致")

    # 批量文档编码
    texts = [
        "员工可以在每周五带宠物来办公室",
        "远程办公政策允许每周3天在家工作",
        "公司提供全面的健康保险",
    ]

    print(f"\n批量文档编码 ({len(texts)} 条):")
    doc_vecs = np.array(embedder.encode_documents(texts))
    print(f"  结果形状: {doc_vecs.shape}")

    # 检索场景：查询 vs 文档
    query = "宠物能不能带到公司"
    q = np.array(embedder.encode_query(query))
    sims = doc_vecs @ q  # 已归一化，点积即余弦相似度

    print(f'\n查询: "{query}"')
    for i, (t, s) in enumerate(zip(texts, sims), 1):
        marker = " <-- 最相关" if s == sims.max() else ""
        print(f"  {i}. {s:.4f}  {t[:24]}...{marker}")

    print("\n[OK] 测试完成")
