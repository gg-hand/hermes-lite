"""提取 PaddleOCR 识别结果。"""
import os
os.environ['FLAGS_use_mkldnn'] = 'false'

import time
import json
import numpy as np
from PIL import Image
from paddleocr import PaddleOCR

image_path = "data/uploads/0cc36a28-0563-43c9-844d-b94fda630926.png"
pil_image = Image.open(image_path)
image = np.array(pil_image.convert("RGB"))
print(f"图片尺寸: {pil_image.size}")

ocr = PaddleOCR(use_textline_orientation=True, lang="ch", enable_mkldnn=False)

start = time.time()
result = ocr.predict(image)
elapsed = time.time() - start
print(f"推理耗时: {elapsed:.2f}s")

if isinstance(result, list) and len(result) > 0:
    item = result[0]
    # OCRResult 是 dict 子类，直接用 dict 方式访问
    print(f"\n所有键: {list(item.keys())}")

    # 尝试直接获取 rec_texts
    if 'rec_texts' in item:
        texts = item['rec_texts']
        scores = item.get('rec_scores', None)
        print(f"\n识别到 {len(texts)} 行文字")
        if scores:
            avg_conf = sum(float(s) for s in scores) / len(scores)
            print(f"平均置信度: {avg_conf:.3f}")
        for j, t in enumerate(texts):
            s = float(scores[j]) if scores and j < len(scores) else 0
            print(f"  [{j}] conf={s:.3f} {repr(t)}")
        print(f"\n=== 合并文本 ===")
        print("\n".join(texts))
    else:
        # 检查 res 子 dict
        res = item.get('res', item)
        print(f"\nres 键: {list(res.keys()) if isinstance(res, dict) else type(res)}")
        if isinstance(res, dict) and 'rec_texts' in res:
            texts = res['rec_texts']
            scores = res.get('rec_scores', None)
            print(f"\n识别到 {len(texts)} 行文字")
            if scores:
                avg_conf = sum(float(s) for s in scores) / len(scores)
                print(f"平均置信度: {avg_conf:.3f}")
            for j, t in enumerate(texts):
                s = float(scores[j]) if scores and j < len(scores) else 0
                print(f"  [{j}] conf={s:.3f} {repr(t)}")
            print(f"\n=== 合并文本 ===")
            print("\n".join(texts))
        else:
            # 打印完整 json
            j = item.json if isinstance(item.json, str) else json.dumps(item.json, ensure_ascii=False, indent=2)
            print(f"\n完整 JSON (前3000字):\n{j[:3000]}")
