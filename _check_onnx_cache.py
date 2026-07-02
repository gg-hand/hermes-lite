"""检查 Chroma ONNX 模型的缓存路径结构"""
import os, inspect, hashlib

# 看 ONNXMiniLM_L6_V2 的源码
from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

# 打印类的完整源码（前50行关键部分）
lines, start = inspect.getsourcelines(ONNXMiniLM_L6_V2)
for i, l in enumerate(lines):
    if i < 100:  # 只打前100行
        print(f'{start+i}: {l}', end='')

print("\n\n=== 检查本地缓存结构 ===")
cache_base = os.path.expanduser("~/.cache/chroma/onnx_models/all-MiniLM-L6-v2")
print(f"缓存路径: {cache_base}")
for root, dirs, files in os.walk(cache_base):
    for f in files:
        fp = os.path.join(root, f)
        size = os.path.getsize(fp)
        rel = os.path.relpath(fp, cache_base)
        print(f"  {rel} ({size/1024/1024:.1f} MB)")

# 计算文件 sha256 用于验证
print("\n=== 文件校验 ===")
for fname in ['onnx.tar.gz', 'onnx/model.onnx', 'onnx/tokenizer.json', 'onnx/vocab.txt']:
    fp = os.path.join(cache_base, fname)
    if os.path.exists(fp):
        sha = hashlib.sha256()
        with open(fp, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                sha.update(chunk)
        print(f"  {fname}: sha256={sha.hexdigest()[:16]}... ({os.path.getsize(fp)/1024/1024:.1f} MB)")
