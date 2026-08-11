#!/usr/bin/env python3
"""打包项目文件为 tar.gz"""
import os, tarfile, sys

src_dir = r"E:\Java\webser\web_app\webme\teage-liu"
out_path = r"E:\Java\webser\web_app\webme\teage-liu-deploy.tar.gz"

exclude_dirs = {'.venv', '__pycache__', '.pytest_cache', '.git', 'data', 'node_modules', '_onnx_model'}
exclude_suffixes = {'.pyc', '.pyo'}
exclude_files = {'$null', '.server.pid', 'server_out.txt', 'server_err.txt', 'test_ssh.py'}

with tarfile.open(out_path, 'w:gz') as tar:
    for root, dirs, files in os.walk(src_dir):
        # 排除目录
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        
        rel_root = os.path.relpath(root, src_dir)
        if rel_root == '.':
            rel_root = ''
        
        for f in files:
            ext = os.path.splitext(f)[1]
            if ext in exclude_suffixes:
                continue
            if f in exclude_files:
                continue
            
            fpath = os.path.join(root, f)
            arcname = os.path.join('teage-liu', rel_root, f).replace('\\', '/')
            tar.add(fpath, arcname=arcname)

size = os.path.getsize(out_path)
print(f"[OK] 打包完成: {out_path}")
print(f"     大小: {size / 1024:.1f} KB")
