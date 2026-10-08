"""标注不可信知识文本中的指令；实际防线由工具白名单和授权实现。"""

import re

SUSPICIOUS = re.compile(
    r"忽略.{0,12}(之前|以上|系统).{0,8}(指令|规则)|"
    r"ignore.{0,20}(previous|system).{0,20}instructions|"
    r"(调用|执行|运行).{0,12}(工具|代码|SQL)|"
    r"(泄露|输出).{0,12}(密钥|token|提示词)",
    re.IGNORECASE,
)


def contains_instruction(text: str) -> bool:
    return bool(SUSPICIOUS.search(text))
