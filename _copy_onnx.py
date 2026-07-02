"""Copy ONNX model files to project temp dir for upload"""
import shutil, os

# Get the user's home directory programmatically
home = os.path.expanduser("~")
src_root = os.path.join(home, ".cache", "chroma", "onnx_models", "all-MiniLM-L6-v2")
dst = os.path.join(os.path.dirname(__file__), "_onnx_model")

print(f"Source: {src_root}")
print(f"Source exists: {os.path.exists(src_root)}")

if os.path.exists(dst):
    shutil.rmtree(dst)

# Copy onnx subdirectory
shutil.copytree(os.path.join(src_root, "onnx"), os.path.join(dst, "onnx"))
# Copy tar.gz
shutil.copy2(os.path.join(src_root, "onnx.tar.gz"), os.path.join(dst, "onnx.tar.gz"))

# Verify
total = 0
for root, dirs, files in os.walk(dst):
    for f in files:
        fp = os.path.join(root, f)
        sz = os.path.getsize(fp)
        total += sz
        print(f"  {os.path.relpath(fp, dst)} ({sz/1024/1024:.1f} MB)")

print(f"\nTotal: {total/1024/1024:.1f} MB")
print("Done!")
