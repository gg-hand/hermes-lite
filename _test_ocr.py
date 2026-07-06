"""测试 PaddleOCR 3.x 正确 API。"""
import sys
import time
import os

image_path = "data/uploads/0cc36a28-0563-43c9-844d-b94fda630926.png"
if not os.path.exists(image_path):
    print(f"图片不存在: {image_path}")
    sys.exit(1)

print(f"图片大小: {os.path.getsize(image_path)} bytes")

import paddle
print(f"paddle 版本: {paddle.__version__}")

import paddleocr
print(f"paddleocr 版本: {paddleocr.__version__}")

from paddleocr import PaddleOCR
import numpy as np
from PIL import Image

# PaddleOCR 3.x API: 移除 use_gpu，use_angle_cls 改名 use_textline_orientation
print("\n=== PaddleOCR 3.x 初始化 ===")
ocr = PaddleOCR(use_textline_orientation=True, lang="ch")
print("初始化成功!")

pil_image = Image.open(image_path)
print(f"图片尺寸: {pil_image.size}, 模式: {pil_image.mode}")
image = np.array(pil_image.convert("RGB"))

# 测试 predict() 方法（3.x 推荐 API）
print("\n=== predict() 测试 ===")
start = time.time()
try:
    result = ocr.predict(image)
    elapsed = time.time() - start
    print(f"predict() 成功，耗时 {elapsed:.2f}s")
    print(f"返回类型: {type(result)}")

    if isinstance(result, list):
        print(f"返回长度: {len(result)}")
        for i, item in enumerate(result):
            print(f"\n--- 项 {i} ---")
            print(f"  type: {type(item).__name__}")

            # 检查 OCRResult 的属性
            if hasattr(item, 'rec_texts'):
                texts = item.rec_texts
                scores = item.rec_scores if hasattr(item, 'rec_scores') else None
                print(f"  rec_texts 数量: {len(texts)}")
                for j, (t, s) in enumerate(zip(texts, scores or [0]*len(texts))):
                    print(f"    [{j}] score={float(s):.3f} text={repr(t)}")
                if scores:
                    avg_conf = sum(float(s) for s in scores) / len(scores)
                    print(f"  平均置信度: {avg_conf:.3f}")
                print(f"\n  合并文本:\n{chr(10).join(texts)}")
            elif hasattr(item, 'json'):
                import json
                j = item.json if isinstance(item.json, str) else json.dumps(item.json, ensure_ascii=False)
                print(f"  json (前500字): {j[:500]}")
            else:
                print(f"  dir: {[a for a in dir(item) if not a.startswith('_')]}")
                print(f"  str (前500字): {str(item)[:500]}")
    else:
        print(f"返回内容: {str(result)[:1000]}")
except Exception as e:
    elapsed = time.time() - start
    print(f"predict() 失败 ({elapsed:.2f}s): {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()

# 也测试 ocr() 向后兼容
print("\n=== ocr() 向后兼容测试 ===")
start = time.time()
try:
    result2 = ocr.ocr(image, cls=True)
    elapsed = time.time() - start
    print(f"ocr() 成功，耗时 {elapsed:.2f}s")
    print(f"返回类型: {type(result2)}")
    if result2:
        print(f"  块数: {len(result2)}")
        for i, line in enumerate(result2[:3]):
            if line is None:
                print(f"  块{i}: None")
                continue
            for j, item in enumerate(line[:3]):
                if item and len(item) >= 2:
                    print(f"  块{i}项{j}: conf={float(item[1][1]):.3f} text={repr(item[1][0])}")
except Exception as e:
    elapsed = time.time() - start
    print(f"ocr() 失败 ({elapsed:.2f}s): {type(e).__name__}: {e}")
