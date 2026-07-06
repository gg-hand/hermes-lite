"""检查图片的 img_text 和尝试禁用 oneDNN。"""
import sqlite3
import os

# 1. 检查数据库中的 img_text
conn = sqlite3.connect('data/sessions.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("SELECT file_id, original_name, etl_status, img_text, error_reason FROM uploaded_files WHERE file_id LIKE '0cc36a28%'")
row = cur.fetchone()
if row:
    print(f"file_id: {row['file_id']}")
    print(f"name: {row['original_name']}")
    print(f"status: {row['etl_status']}")
    print(f"img_text长度: {len(row['img_text'] or '')}")
    print(f"img_text内容:\n{row['img_text'] or '(空)'}")
    print(f"error: {row['error_reason'] or '(无)'}")
conn.close()

# 2. 尝试禁用 oneDNN 后重新推理
print("\n=== 禁用 oneDNN 后测试 PaddleOCR ===")
import paddle
paddle.set_flags({'FLAGS_use_mkldnn': False})

import time
import numpy as np
from PIL import Image
from paddleocr import PaddleOCR

image_path = "data/uploads/0cc36a28-0563-43c9-844d-b94fda630926.png"
pil_image = Image.open(image_path)
image = np.array(pil_image.convert("RGB"))
print(f"图片尺寸: {pil_image.size}")

print("初始化 PaddleOCR(use_textline_orientation=True, lang='ch')...")
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
                    s = float(scores[j]) if scores else 0
                    print(f"  [{j}] conf={s:.3f} {repr(t)}")
                print(f"\n  合并文本:\n{chr(10).join(texts)}")
            else:
                print(f"  item type: {type(item).__name__}")
                print(f"  attrs: {[a for a in dir(item) if not a.startswith('_')]}")
                print(f"  str: {str(item)[:500]}")
except Exception as e:
    elapsed = time.time() - start
    print(f"predict() 失败 ({elapsed:.2f}s): {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
