from ragas import evaluate
from ragas.metrics import (
    faithfulness,
    answer_relevancy,
    context_precision,
    context_recall
)

from datasets import Dataset

# 本地LLM
from langchain_community.llms import Ollama
from ragas.llms import LangchainLLMWrapper

# 你的RAG
from app.services.rag_service import ask_question

print("初始化本地评估模型...")

# 本地评估LLM
local_llm = Ollama(model="deepseek-coder:1.3b")

ragas_llm = LangchainLLMWrapper(local_llm)

# 评测问题（建议你改成自己的）
questions = [
    "RAG系统的整体流程是什么？",
    "向量数据库使用的是哪一个？",
    "如何构建向量索引？",
    "检索流程是怎样的？"
]

data = []

print("\n开始运行RAG系统回答问题...\n")

for q in questions:

    print("问题:", q)

    result = ask_question(q)

    answer = result["answer"]
    contexts = [s["preview"] for s in result["sources"]]

    print("回答:", answer[:120], "...\n")

    data.append({
        "question": q,
        "answer": answer,
        "contexts": contexts,
        "ground_truth": ""
    })

dataset = Dataset.from_list(data)

print("\n开始评估...\n")

score = evaluate(
    dataset,
    metrics=[
        faithfulness,
        answer_relevancy,
        context_precision,
        context_recall
    ],
    llm=ragas_llm
)

print("\n===== 评估结果 =====")
print(score)