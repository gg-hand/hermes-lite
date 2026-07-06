"""测试 enable_mkldnn=False 参数。"""
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

# 尝试 enable_mkldnn=False
print("\n=== PaddleOCR(enable_mkldnn=False) ===")
try:
    ocr = PaddleOCR(use_textline_orientation=True, lang="ch", enable_mkldnn=False)
    print("初始化成功!")
    start = time.time()
    result = ocr.predict(image)
    elapsed = time.time() - start
    print(f"predict() 成功! 耗时 {elapsed:.2f}s")
    if isinstance(result, list):
        for item in result:
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
except Exception as e:
    print(f"失败: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
