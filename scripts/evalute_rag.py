from datasets import Dataset
from ragas import evaluate
from ragas.llms import LangchainLLMWrapper
from ragas.metrics import (
    answer_relevancy,
    context_precision,
    context_recall,
    faithfulness,
)

from rag.llms.llm_model import get_llm
from app.services.rag_service import ask_question

QUESTIONS = [
    "RAG系统的整体流程是什么？",
    "向量数据库使用的是哪一个？",
    "如何构建向量索引？",
    "检索流程是怎样的？"
]


def build_evaluation_dataset():
    data = []

    print("\n开始运行RAG系统回答问题...\n")

    for question in QUESTIONS:
        print("问题:", question)
        result = ask_question(question)
        answer = result["answer"]
        contexts = [source["preview"] for source in result["sources"]]
        print("回答:", answer[:120], "...\n")

        data.append({
            "question": question,
            "answer": answer,
            "contexts": contexts,
            "ground_truth": "",
        })

    return Dataset.from_list(data)


def main():
    print("初始化评估模型...")
    ragas_llm = LangchainLLMWrapper(get_llm())
    dataset = build_evaluation_dataset()

    print("\n开始评估...\n")
    score = evaluate(
        dataset,
        metrics=[
            faithfulness,
            answer_relevancy,
            context_precision,
            context_recall,
        ],
        llm=ragas_llm,
    )

    print("\n===== 评估结果 =====")
    print(score)


if __name__ == "__main__":
    main()
