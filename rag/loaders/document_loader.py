"""
文档加载模块
作用：
从指定目录读取所有文档
"""

from langchain_community.document_loaders import (
    DirectoryLoader,
    TextLoader,
    PyPDFLoader,
    UnstructuredMarkdownLoader,
    Docx2txtLoader,
    UnstructuredHTMLLoader
)

"""
def load_documents(doc_path):

    loader = DirectoryLoader(
        doc_path,
        glob="**/*.*",   # 读取所有文件
        loader_cls=UnstructuredFileLoader
    )

    docs = loader.load()

    return docs
"""

# 上面代码也可以使用，可加载所有格式文档。但针对不同格式的文档使用不同的 Loader会更稳定
def load_documents(doc_path):

    loaders = [
        DirectoryLoader(
            doc_path,
            glob="**/*.txt",
            loader_cls=TextLoader,
                loader_kwargs={
                    "encoding": "utf-8",
                    "autodetect_encoding": True
                }
        ),
        DirectoryLoader(
            doc_path,
            glob="**/*.pdf",
            loader_cls=PyPDFLoader
        ),
        DirectoryLoader(
            doc_path,
            glob="**/*.md",
            loader_cls=UnstructuredMarkdownLoader
        ),
        DirectoryLoader(
            doc_path,
            glob="**/*.docx",
            loader_cls=Docx2txtLoader
        ),
                DirectoryLoader(
            doc_path,
            glob="**/*.html",
            loader_cls=UnstructuredHTMLLoader
        ),
    ]

    docs = []
    for loader in loaders:
        try:
            docs.extend(loader.load())
        except Exception as e:
            print("加载失败:", loader, e)

    docs = [doc for doc in docs if doc.page_content.strip()] # 过滤掉内容为空的文档
    return docs