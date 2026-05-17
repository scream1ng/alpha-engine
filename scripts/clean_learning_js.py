"""Remove duplicate JS function definitions from docs/index.html.

Keeps only the LAST definition of each duplicated function.
Also removes renderLearningSidebar and all replayLearningTrigger copies (dead code).
"""
import re

SRC = "docs/index.html"

with open(SRC, "r", encoding="utf-8") as f:
    lines = f.readlines()

print(f"Input: {len(lines)} lines")


def find_func_end(lines, start):
    depth = 0
    in_str = False
    str_ch = None
    i = start
    while i < len(lines):
        line = lines[i]
        j = 0
        while j < len(line):
            c = line[j]
            if in_str:
                if c == "\\" :
                    j += 2
                    continue
                if c == str_ch:
                    in_str = False
            else:
                if c in ('"', "'", "`"):
                    in_str = True
                    str_ch = c
                elif c == "/" and j + 1 < len(line) and line[j + 1] == "/":
                    break  # line comment
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0 and i > start:
                        return i
            j += 1
        i += 1
    return len(lines) - 1


func_pat = re.compile(r"^(?:async\s+)?function (\w+)\(")
funcs = {}
i = 0
while i < len(lines):
    m = func_pat.match(lines[i])
    if m:
        name = m.group(1)
        start = i
        end = find_func_end(lines, start)
        funcs.setdefault(name, []).append((start, end))
        i = end + 1
    else:
        i += 1

# Lines to delete
dead = set()

# Duplicated: remove all but last
dupes = {k: v for k, v in funcs.items() if len(v) > 1}
for name, ranges in dupes.items():
    for start, end in ranges[:-1]:
        for ln in range(start, end + 1):
            dead.add(ln)
    kept = ranges[-1]
    print(f"  {name}: removed {len(ranges)-1} copy/copies, kept L{kept[0]+1}-{kept[1]+1}")

# Fully dead: renderLearningSidebar (not called in active player)
for name in ("renderLearningSidebar", "replayLearningTrigger"):
    if name in funcs:
        for start, end in funcs[name]:
            for ln in range(start, end + 1):
                dead.add(ln)
        print(f"  {name}: removed entirely ({len(funcs[name])} def(s))")

cleaned = [line for i, line in enumerate(lines) if i not in dead]

with open(SRC, "w", encoding="utf-8") as f:
    f.writelines(cleaned)

print(f"Output: {len(cleaned)} lines (removed {len(dead)} lines)")
