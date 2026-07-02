"""Update v2 write_file description."""
path = "src/agent/builtin_tools.py"
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

old = (
    '"将内容写入指定路径文件（覆盖写入）。会话内新建/修改的文件将记录"'
    '\n            "到 file_registry，用于 PolicyEngine 决策（会话内创建的文件后续"'
    '\n            "修改/删除享有豁免）。"'
)
new = (
    '"将内容写入指定路径文件（覆盖写入）。写入文件应优先使用此工具，"'
    '\n            "而非通过 execute_command 执行 echo/重定向——本工具自动创建父目录、"'
    '\n            "编码安全、记录操作到审计。会话内新建/修改的文件将记录"'
    '\n            "到 file_registry，用于 PolicyEngine 决策（会话内创建的文件后续"'
    '\n            "修改/删除享有豁免）。"'
)

assert old in content, "old not found"
content = content.replace(old, new)
with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print("OK")
