"""检查 chromadb ONNX 模型的缓存位置和所需文件"""
import os, sys, inspect

# 1. 找到 DefaultEmbeddingFunction
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

print("=== DefaultEmbeddingFunction MRO ===")
for cls in DefaultEmbeddingFunction.__mro__:
    print(f"  {cls.__module__}.{cls.__name__}")

# 2. 查看 ONNXMiniLM_L6_V2
onx = None
try:
    from chromadb.utils.embedding_functions.onnx_model import ONNXMiniLM_L6_V2
    onx = ONNXMiniLM_L6_V2
except:
    pass
if not onx:
    try:
        from chromadb.utils.embedding_functions.onnx_embedding import ONNXMiniLM_L6_V2
        onx = ONNXMiniLM_L6_V2
    except:
        pass
if not onx:
    # Search for it
    import chromadb
    pkg_dir = os.path.dirname(chromadb.__file__)
    for root, dirs, files in os.walk(pkg_dir):
        for f in files:
            if f.endswith('.py'):
                fp = os.path.join(root, f)
                with open(fp, 'r', encoding='utf-8') as fh:
                    content = fh.read()
                    if 'ONNXMiniLM_L6_V2' in content:
                        print(f"\nONNXMiniLM_L6_V2 found in: {fp}")
                        # Print relevant lines
                        lines = content.split('\n')
                        for i, line in enumerate(lines):
                            if 'ONNXMiniLM_L6_V2' in line:
                                print(f"  L{i+1}: {line.strip()}")
                        break

if onx:
    print(f"\n=== ONNXMiniLM_L6_V2 ===")
    print(f"Source: {inspect.getfile(onx)}")
    try:
        src = inspect.getsource(onx.__init__)
        print(src[:2000])
    except:
        pass

# 3. 检查本地缓存
print("\n=== 本地缓存 all-MiniLM-L6-v2 ===")
home = os.path.expanduser("~")
for root, dirs, files in os.walk(home):
    # limit scan depth
    rel = os.path.relpath(root, home)
    if rel.count(os.sep) > 5:
        dirs.clear()
        continue
    for f in files:
        if any(x in f.lower() for x in ["onnx", "mini", "tokenizer", "vocab", "model"]):
            fp = os.path.join(root, f)
            size = os.path.getsize(fp)
            if size > 1000:  # skip tiny files
                print(f"  {fp} ({size/1024/1024:.1f} MB)")

# 4. Check site-packages for onnx model data
print("\n=== site-packages/chromadb onnx data ===")
import chromadb
pkg_dir = os.path.dirname(chromadb.__file__)
for root, dirs, files in os.walk(pkg_dir):
    for f in files:
        if any(x in f.lower() for x in [".onnx", ".bin", "model"]):
            fp = os.path.join(root, f)
            size = os.path.getsize(fp)
            print(f"  {fp} ({size/1024/1024:.1f} MB)")
