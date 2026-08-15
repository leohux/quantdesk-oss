#!/usr/bin/env python3
"""
check_enum_consistency.py — 验证 failure_category 枚举在所有文件中是否一致

用法：python check_enum_consistency.py
退出码：0 = 一致，1 = 不一致
"""
import re
import sys
from pathlib import Path

# 1. 从 REGISTRY_CONVENTIONS.md 提取枚举
CONVENTIONS_PATH = Path(__file__).parent.parent / "REGISTRY_CONVENTIONS.md"
AGENT_ENUM_PATH = Path(__file__).parent / "post_mortem_agent.py"

# Canonical list（单一真相来源）
CANONICAL = [
    "RegimeShift", "Overfit", "LowSample", "Cost", "Lookahead",
    "Execution", "Capacity", "DataIssue", "NoEdgeFound", "Other",
]


def extract_from_conventions(path: Path) -> list[str]:
    """从 markdown 表格中提取 failure_category 枚举值。"""
    text = path.read_text()
    # 匹配 | `Value` | 格式的行（在 failure_category 段落内）
    in_section = False
    values = []
    for line in text.split("\n"):
        if "failure_category" in line.lower() and ("仅" in line or "必填" in line):
            in_section = True
            continue
        if in_section and line.startswith("|"):
            m = re.search(r"`(\w+)`", line)
            if m:
                values.append(m.group(1))
        elif in_section and not line.startswith("|") and line.strip() and not line.startswith("-"):
            break
    return values


def extract_from_agent(path: Path) -> list[str]:
    """从 Python 代码中提取 KNOWN_FAILURE_CATEGORIES 列表。"""
    text = path.read_text()
    m = re.search(r"KNOWN_FAILURE_CATEGORIES\s*=\s*\[(.*?)\]", text, re.DOTALL)
    if not m:
        return []
    return re.findall(r'"(\w+)"', m.group(1))


def main():
    errors = []

    conv = extract_from_conventions(CONVENTIONS_PATH)
    agent = extract_from_agent(AGENT_ENUM_PATH)

    print(f"CANONICAL:                {CANONICAL}")
    print(f"REGISTRY_CONVENTIONS.md:  {conv}")
    print(f"post_mortem_agent.py:     {agent}")
    print()

    if set(CANONICAL) != set(conv):
        errors.append(f"CANONICAL vs CONVENTIONS mismatch: "
                      f"missing={set(CANONICAL)-set(conv)}, "
                      f"extra={set(conv)-set(CANONICAL)}")

    if set(CANONICAL) != set(agent):
        errors.append(f"CANONICAL vs AGENT mismatch: "
                      f"missing={set(CANONICAL)-set(agent)}, "
                      f"extra={set(agent)-set(CANONICAL)}")

    if set(conv) != set(agent):
        errors.append(f"CONVENTIONS vs AGENT mismatch: "
                      f"missing={set(conv)-set(agent)}, "
                      f"extra={set(agent)-set(conv)}")

    if errors:
        print("❌ ENUM INCONSISTENCY DETECTED:")
        for e in errors:
            print(f"   {e}")
        sys.exit(1)
    else:
        print("✅ All three sources are consistent.")
        sys.exit(0)


if __name__ == "__main__":
    main()
