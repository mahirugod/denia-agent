"""把第 59 行的占位正则 r'``` ``` ```' 替换为真正的思维链剥离正则（单行）。"""
import ast
import io
import os

P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "denia_llm.py")

with io.open(P, "r", encoding="utf-8") as f:
    lines = f.readlines()

# 定位第 59 行（索引 58）：上一行含 "剥思维链"，当前行含 "``` ``` ```"
idx = None
for i, ln in enumerate(lines):
    if i > 50 and i < 100 and "``` ``` ```" in ln and i > 0 and "剥思维链" in lines[i - 1]:
        idx = i
        break

if idx is None:
    print("PATTERN NOT FOUND")
    raise SystemExit(1)

B = chr(96) * 3
# 目标：text = re.sub(r'
# 用 chr 拼接避免在源码中写  字面量
target = "    text = re.sub(r'" + B + " " + B + " " + B + "', '', text, flags=re.DOTALL)\n"

print("before:", repr(lines[idx]))
lines[idx] = target
print("after: ", repr(lines[idx]))

with io.open(P, "w", encoding="utf-8") as f:
    f.writelines(lines)

with io.open(P, "r", encoding="utf-8") as f:
    src = f.read()
ast.parse(src)
print("SYNTAX_OK")

# 功能验证：模拟思维链剥离
import re
ns = {}
exec(compile(io.open(P, encoding="utf-8").read(), P, "exec"), ns)
# 直接调用 _clean_reply_tags
sample = (
    "思考中...  \n"
    "好的，用户问天气。让我分析一下。\n"
    "嗯？我刚才走神了…再说一遍嘛"
)
cleaned = ns["_clean_reply_tags"](sample)
print("CLEANED:", repr(cleaned))
assert B not in cleaned, "think chain not stripped"
assert "思考中" not in cleaned, "thinks leaked"
assert "嗯？我刚才走神了" in cleaned, "final reply lost"
print("ASSERT_OK")

os.remove(__file__)
print("SELF_REMOVED")
