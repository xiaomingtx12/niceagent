"""
导入知识到 Faiss 向量数据库

使用方法:
    1. 修改下面的配置参数（DASHSCOPE_API_KEY 和 SOURCE_DIR）
    2. 运行: python scripts/import_to_faiss.py
"""

import os
import sys
from pathlib import Path

# 添加项目路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from langchain_core.documents import Document
from langchain_community.document_loaders import (
    DirectoryLoader,
    TextLoader,
    UnstructuredMarkdownLoader,
)
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_community.embeddings import DashScopeEmbeddings


# ============================================================
# 配置参数 - 请修改以下内容
# ============================================================

# DashScope API Key（必需）- 用于生成文本嵌入向量
DASHSCOPE_API_KEY = (
    "sk-6bc14803d53140cdbc4e3755821dba2c"  # 👈 在这里填写你的 DashScope API Key
)

# 知识库源目录（相对于项目根目录）
SOURCE_DIR = "storage/vector_store"

# 文本分块大小
CHUNK_SIZE = 1000

# 文本分块重叠
CHUNK_OVERLAP = 200

# Faiss 向量数据库存储路径（相对于项目根目录）
FAISS_STORE_PATH = "internal/core/vector_store"

# ============================================================


def load_documents(directory_path: str) -> list[Document]:
    """从目录加载文档"""
    documents = []

    print(f"📂 正在扫描目录: {directory_path}")

    # 加载 .txt 文件
    try:
        loader = DirectoryLoader(
            directory_path,
            glob="**/*.txt",
            loader_cls=TextLoader,
            loader_kwargs={"encoding": "utf-8"},
            show_progress=True,
        )
        docs = loader.load()
        documents.extend(docs)
        print(f"  ✓ 加载 .txt 文件: {len(docs)} 个")
    except Exception as e:
        print(f"  ✗ 加载 .txt 文件失败: {e}")

    # 加载 .md 文件
    try:
        loader = DirectoryLoader(
            directory_path,
            glob="**/*.md",
            loader_cls=UnstructuredMarkdownLoader,
            show_progress=True,
        )
        docs = loader.load()
        documents.extend(docs)
        print(f"  ✓ 加载 .md 文件: {len(docs)} 个")
    except Exception as e:
        print(f"  ✗ 加载 .md 文件失败: {e}")

    return documents


def split_documents(documents: list[Document]) -> list[Document]:
    """分割文档为小块"""
    print(f"\n📝 正在分割文档...")

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        length_function=len,
        separators=["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""],
    )

    split_docs = text_splitter.split_documents(documents)
    print(f"  ✓ {len(documents)} 个文档 → {len(split_docs)} 个文档块")

    return split_docs


def import_to_faiss(documents: list[Document], api_key: str, store_path: str):
    """导入文档到 Faiss 向量数据库"""

    print(f"\n🚀 正在初始化嵌入模型...")

    # 初始化 DashScope 嵌入模型
    embeddings = DashScopeEmbeddings(
        model="text-embedding-v1",
        dashscope_api_key=api_key,
    )
    print(f"  ✓ 嵌入模型初始化成功")

    print(f"\n🔍 正在加载现有的 Faiss 向量数据库...")

    # 尝试加载现有的 Faiss 数据库
    try:
        faiss_db = FAISS.load_local(
            folder_path=store_path,
            embeddings=embeddings,
            allow_dangerous_deserialization=True,
        )
        current_count = len(faiss_db.index_to_docstore_id)
        print(f"  ✓ 加载成功，当前有 {current_count} 个文档")
    except Exception as e:
        print(f"  ℹ️  未找到现有数据库，将创建新的: {e}")
        # 创建新的空 Faiss 数据库
        temp_doc = [Document(page_content="", metadata={})]
        faiss_db = FAISS.from_documents(documents=temp_doc, embedding=embeddings)
        faiss_db.delete([faiss_db.index_to_docstore_id[0]])
        current_count = 0
        print(f"  ✓ 创建新的 Faiss 数据库")

    print(f"\n📥 正在添加 {len(documents)} 个文档块...")
    ids = faiss_db.add_documents(documents)
    print(f"  ✓ 成功添加 {len(ids)} 个文档块")

    print(f"\n💾 正在保存到磁盘...")
    os.makedirs(store_path, exist_ok=True)
    faiss_db.save_local(store_path)
    print(f"  ✓ 保存成功: {store_path}")

    new_count = len(faiss_db.index_to_docstore_id)
    print(f"\n✅ 导入完成!")
    print(f"  • 原有文档: {current_count} 个")
    print(f"  • 新增文档: {new_count - current_count} 个")
    print(f"  • 总计文档: {new_count} 个")


def main():
    print("=" * 60)
    print("📚 Faiss 知识库导入工具")
    print("=" * 60)
    print(
        f"API Key: {DASHSCOPE_API_KEY[:20]}..."
        if len(DASHSCOPE_API_KEY) > 20
        else f"API Key: {DASHSCOPE_API_KEY}"
    )
    print(f"源目录: {SOURCE_DIR}")
    print(f"存储路径: {FAISS_STORE_PATH}")
    print(f"分块大小: {CHUNK_SIZE}")
    print(f"分块重叠: {CHUNK_OVERLAP}")
    print("=" * 60)

    # 验证 API Key
    if DASHSCOPE_API_KEY == "your-dashscope-api-key" or not DASHSCOPE_API_KEY:
        print(f"\n❌ 错误: 请先配置 DASHSCOPE_API_KEY")
        print(f"请在脚本中修改 DASHSCOPE_API_KEY 为你的 DashScope API Key")
        sys.exit(1)

    # 验证源目录
    source_dir = os.path.join(project_root, SOURCE_DIR)
    if not os.path.exists(source_dir):
        print(f"\n❌ 错误: 目录不存在: {source_dir}")
        print(f"请检查 SOURCE_DIR 配置")
        sys.exit(1)

    # 准备存储路径
    store_path = os.path.join(project_root, FAISS_STORE_PATH)

    try:
        # 加载文档
        documents = load_documents(source_dir)

        if not documents:
            print("\n⚠️  警告: 没有找到任何文档")
            sys.exit(0)

        # 分割文档
        split_docs = split_documents(documents)

        # 导入到 Faiss
        import_to_faiss(split_docs, DASHSCOPE_API_KEY, store_path)

    except KeyboardInterrupt:
        print("\n\n⚠️  操作已取消")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ 错误: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
