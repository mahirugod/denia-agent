"""精确修复 denia_llm.py 第 59 行：把被反引号分隔符拆断的思维链正则重建为单行。"""
import ast
import io
import os

P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "denia_llm.py")

with io.open(P, "r", encoding="utf-8") as f:
    lines = f.readlines()

# 定位：上一行含 "剥思维链" 且当前行 strip 后为 "text = re.sub(r'"
idx = None
for i, ln in enumerate(lines):
    if i > 50 and i < 100 and ln.strip() == "text = re.sub(r'" and i > 0 and "剥思维链" in lines[i - 1]:
        idx = i
        break

if idx is None:
    print("PATTERN NOT FOUND, abort")
    raise SystemExit(1)

B = chr(96) * 3
# 目标单行：text = re.sub(r'
# 拼接时避免在源码里写  字面量
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

# 验证：第 59 行
with io.open(P, "r", encoding="utf-8") as f:
    ls2 = f.readlines()
print("VERIFY line 59:", repr(ls2[58]))
# 验证正则确实包含反引号
assert B in ls2[58], "backticks missing"
assert ls2[58].strip().startswith("text = re.sub("), "line format wrong"
print("ASSERT_OK")

os.remove(__file__)
print("SELF_REMOVED")
