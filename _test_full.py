"""完整测试 PaddleOCR enable_mkldnn=False 的识别效果。"""
import os
os.environ['FLAGS_use_mkldnn'] = 'false'

import time
import numpy as np
from PIL import Image
from paddleocr import PaddleOCR

image_path = "data/uploads/0cc36a28-0563-43c9-844d-b94fda630926.png"
pil_image = Image.open(image_path)
image = np.array(pil_image.convert("RGB"))
print(f"图片尺寸: {pil_image.size}")

print("\n初始化 PaddleOCR(enable_mkldnn=False)...")
ocr = PaddleOCR(use_textline_orientation=True, lang="ch", enable_mkldnn=False)
print("初始化成功!")

print("\n推理中...")
start = time.time()
result = ocr.predict(image)
elapsed = time.time() - start
print(f"predict() 成功! 耗时 {elapsed:.2f}s")
print(f"返回类型: {type(result).__name__}")
print(f"是否为 list: {isinstance(result, list)}")

if isinstance(result, list):
    print(f"返回长度: {len(result)}")
    for i, item in enumerate(result):
        print(f"\n--- 项 {i} ---")
        print(f"  type: {type(item).__name__}")
        print(f"  attrs: {[a for a in dir(item) if not a.startswith('_')]}")
        if hasattr(item, 'rec_texts'):
            texts = item.rec_texts
            scores = item.rec_scores if hasattr(item, 'rec_scores') else None
            print(f"  rec_texts 数量: {len(texts)}")
            if scores:
                avg_conf = sum(float(s) for s in scores) / len(scores)
                print(f"  平均置信度: {avg_conf:.3f}")
            for j, t in enumerate(texts):
                s = float(scores[j]) if scores and j < len(scores) else 0
                print(f"  [{j}] conf={s:.3f} {repr(t)}")
            print(f"\n  === 合并文本 ===")
            print("\n".join(texts))
        elif hasattr(item, 'json'):
            import json
            j = item.json if isinstance(item.json, str) else json.dumps(item.json, ensure_ascii=False, indent=2)
            print(f"  json (前1000字): {j[:1000]}")
        else:
            print(f"  str (前1000字): {str(item)[:1000]}")
else:
    print(f"返回内容: {str(result)[:2000]}")
