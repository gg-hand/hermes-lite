"""尝试通过环境变量禁用 PIR 执行器解决 oneDNN bug。"""
import os

# 在 import paddle 前设置环境变量
os.environ['FLAGS_enable_pir_in_executor'] = '0'
os.environ['FLAGS_enable_pir_api'] = '0'
os.environ['FLAGS_use_mkldnn'] = 'false'

import time
import numpy as np
from PIL import Image
import paddle
print(f"paddle 版本: {paddle.__version__}")

from paddleocr import PaddleOCR
print(f"paddleocr 版本: {__import__('paddleocr').__version__}")

image_path = "data/uploads/0cc36a28-0563-43c9-844d-b94fda630926.png"
pil_image = Image.open(image_path)
image = np.array(pil_image.convert("RGB"))
print(f"图片尺寸: {pil_image.size}")

print("\n初始化 PaddleOCR...")
ocr = PaddleOCR(use_textline_orientation=True, lang="ch")
print("初始化成功!")

print("\n推理中...")
start = time.time()
try:
    result = ocr.predict(image)
    elapsed = time.time() - start
    print(f"predict() 成功! 耗时 {elapsed:.2f}s")

    if isinstance(result, list):
        for i, item in enumerate(result):
            if hasattr(item, 'rec_texts'):
                texts = item.rec_texts
                scores = item.rec_scores if hasattr(item, 'rec_scores') else None
                print(f"  识别到 {len(texts)} 行文字")
                if scores:
                    avg_conf = sum(float(s) for s in scores) / len(scores)
                    print(f"  平均置信度: {avg_conf:.3f}")
                for j, t in enumerate(texts):
                    s = float(scores[j]) if scores and j < len(scores) else 0
                    print(f"  [{j}] conf={s:.3f} {repr(t)}")
                print(f"\n  合并文本:\n{chr(10).join(texts)}")
            else:
                print(f"  item type: {type(item).__name__}")
                print(f"  attrs: {[a for a in dir(item) if not a.startswith('_')]}")
                print(f"  str: {str(item)[:500]}")
except Exception as e:
    elapsed = time.time() - start
    print(f"predict() 失败 ({elapsed:.2f}s): {type(e).__name__}: {e}")
