"""兼容入口：内部调用新的 scripts.evaluate_rag。

保留拼写错误文件名，README 统一使用 scripts.evaluate_rag。
"""

from scripts.evaluate_rag import main

if __name__ == "__main__":
    main()
