from pathlib import Path

PROMPT_DIR = Path(__file__).parent

def load_prompt(name: str):

    path = PROMPT_DIR / name

    with open(path, "r", encoding="utf-8") as f:
        return f.read()