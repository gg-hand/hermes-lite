"""瀑布式文件解析器。

三层解析策略：
1. L1 库解析：PyMuPDF（PDF）/ python-docx（DOCX）/ txt+md 直接解码 / pytesseract（图片 OCR）
2. L2 编码修复：UTF-8 → GBK 回退 + 乱码检测（中文字符占比）
3. L3 LLM 降级：截取前 10KB 送 LLM 提取文本（可选，默认关）

所有第三方库导入用 ``try/except`` 保护，缺失时 L1 对应格式返回失败。
图片 OCR 仅提取文字，不生成视觉描述。
"""

from __future__ import annotations

import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 可选依赖检测
# ---------------------------------------------------------------------------

_PYMUPDF_AVAILABLE = False
try:
    import fitz  # PyMuPDF
    _PYMUPDF_AVAILABLE = True
except ImportError:
    pass

_DOCX_AVAILABLE = False
try:
    import docx
    _DOCX_AVAILABLE = True
except ImportError:
    pass

_TESSERACT_AVAILABLE = False
try:
    import pytesseract
    # 按优先级查找 tesseract 可执行文件路径
    import shutil as _shutil
    import os as _os
    _tesseract_candidates = []
    # 1. 环境变量 TESSERACT_CMD
    _env_path = _os.environ.get("TESSERACT_CMD")
    if _env_path:
        _tesseract_candidates.append(_env_path)
    # 2. PATH 中的 tesseract
    _which = _shutil.which("tesseract")
    if _which:
        _tesseract_candidates.append(_which)
    # 3. 常见安装路径
    for _base in [
        _os.environ.get("ProgramFiles", "C:\\Program Files"),
        _os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"),
        _os.environ.get("LOCALAPPDATA", ""),
    ]:
        _p = _os.path.join(_base, "Tesseract-OCR", "tesseract.exe")
        if _p not in _tesseract_candidates:
            _tesseract_candidates.append(_p)
    for _p in _tesseract_candidates:
        if _os.path.exists(_p):
            pytesseract.pytesseract.tesseract_cmd = _p
            break
    _TESSERACT_AVAILABLE = True
except ImportError:
    pass

_PIL_AVAILABLE = False
try:
    from PIL import Image
    import io as _pil_io
    _PIL_AVAILABLE = True
except ImportError:
    pass

_PADDLE_AVAILABLE = False
try:
    from paddleocr import PaddleOCR  # noqa: F401
    _PADDLE_AVAILABLE = True
except ImportError:
    pass

_CV2_AVAILABLE = False
try:
    import cv2  # noqa: F401
    _CV2_AVAILABLE = True
except ImportError:
    pass

# PaddleOCR 模块级单例（首次加载数秒，必须复用）
# paddlepaddle 推理非线程安全，单例 + Lock 串行调用避免数据竞争
_paddle_singleton = None
_paddle_lock = threading.Lock()
_paddle_poisoned = False  # 上次推理异常崩溃标志，下次调用前重建实例


def _get_paddle_singleton(lang: str, use_gpu: bool):
    """懒加载 PaddleOCR 单例。poisoned 时重建实例。"""
    global _paddle_singleton, _paddle_poisoned
    with _paddle_lock:
        if _paddle_singleton is None or _paddle_poisoned:
            _paddle_singleton = PaddleOCR(use_angle_cls=True, lang=lang, use_gpu=use_gpu)
            _paddle_poisoned = False
        return _paddle_singleton


class ParseError(Exception):
    """解析失败异常，包含失败层级信息。"""

    def __init__(self, message: str, level: int = 1) -> None:
        super().__init__(message)
        self.level = level


class WaterfallParser:
    """瀑布式文件解析器。

    按 L1 → L2 → L3 层级尝试解析文件内容为纯文本。
    每层失败时自动降级到下一层，所有层均失败时抛出 :class:`ParseError`。
    """

    # 中文字符 Unicode 范围（用于乱码检测）
    _CHINESE_CHAR_RE = re.compile(r'[一-鿿㐀-䶿]')

    # 常见编码回退顺序
    _FALLBACK_ENCODINGS = ["utf-8", "gbk", "gb2312", "gb18030", "latin-1"]

    def __init__(
        self,
        llm_fallback_enabled: bool = False,
        llm_client: Optional[object] = None,
        parse_timeouts: Optional[dict] = None,
        ocr_config: Optional[dict] = None,
        metrics_collector: Optional[object] = None,
    ) -> None:
        """初始化解析器。

        Args:
            llm_fallback_enabled: 是否启用 L3 LLM 降级（默认关）。
            llm_client: LLM 客户端实例，需提供 ``chat(messages)`` 接口。
                L3 关闭时可为 None。
            parse_timeouts: 按扩展名的超时配置，如 ``{'.pdf': 120, '.docx': 15}``。
                缺省使用 ``_default`` 键的值（默认 30 秒）。
            ocr_config: 图片 OCR 分层配置（files.ocr 子段）。拍平为实例属性
                以支持热更新直接写入。缺省时按 PaddleOCR→Tesseract 默认值。
            metrics_collector: 可选的 MetricsCollector 实例，用于 OCR 埋点。
                缺省时跳过埋点。
        """
        self.llm_fallback_enabled = llm_fallback_enabled
        self.llm_client = llm_client
        self.parse_timeouts = parse_timeouts or {"_default": 30}
        self._metrics = metrics_collector
        # OCR 分层配置：拍平为实例属性，支持热更新直接写入
        ocr_config = ocr_config or {}
        paddle_cfg = ocr_config.get("paddle", {}) or {}
        tesseract_cfg = ocr_config.get("tesseract", {}) or {}
        vision_cfg = ocr_config.get("vision_llm", {}) or {}
        self.ocr_primary_engine = ocr_config.get("primary_engine", "paddle")
        self.ocr_paddle_lang = paddle_cfg.get("lang", "ch")
        self.ocr_paddle_use_gpu = bool(paddle_cfg.get("use_gpu", False))
        self.ocr_paddle_min_confidence = float(paddle_cfg.get("min_confidence", 0.6))
        self.ocr_paddle_infer_timeout = int(paddle_cfg.get("infer_timeout", 30))
        self.ocr_tesseract_lang = tesseract_cfg.get("lang", "chi_sim+eng")
        self.ocr_tesseract_preprocess = bool(tesseract_cfg.get("preprocess", True))
        self.ocr_vision_llm_enabled = bool(vision_cfg.get("enabled", False))

    def _get_timeout(self, filename: str) -> int:
        """根据文件名获取解析超时秒数。"""
        ext = os.path.splitext(filename)[1].lower()
        return self.parse_timeouts.get(ext, self.parse_timeouts.get("_default", 30))

    # ------------------------------------------------------------------
    # L1: 库解析
    # ------------------------------------------------------------------

    def _parse_l1(self, content: bytes, filename: str) -> str:
        """L1 层：使用专用库解析文件。

        Args:
            content: 文件字节内容。
            filename: 原始文件名（用于判断类型）。

        Returns:
            解析后的纯文本。

        Raises:
            ParseError: 解析失败（库缺失或提取异常）。
        """
        ext = os.path.splitext(filename)[1].lower()

        if ext in (".txt", ".md"):
            return self._parse_text_l1(content)

        if ext == ".pdf":
            return self._parse_pdf_l1(content)

        if ext == ".docx":
            return self._parse_docx_l1(content)

        if ext in (".png", ".jpg", ".jpeg", ".gif"):
            # 图片在 parse() 入口走 _parse_image_waterfall 分层通道
            raise ParseError(
                "L1: 图片应在 parse() 入口走 _parse_image_waterfall 分层",
                level=1,
            )

        raise ParseError(f"L1: 不支持的格式 {ext}", level=1)

    def _parse_text_l1(self, content: bytes) -> str:
        """L1 纯文本：直接解码。"""
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            raise ParseError("L1: UTF-8 解码失败", level=1)

    def _parse_pdf_l1(self, content: bytes) -> str:
        """L1 PDF：PyMuPDF 提取文本。"""
        if not _PYMUPDF_AVAILABLE:
            raise ParseError("L1: PyMuPDF 未安装", level=1)
        try:
            doc = fitz.open(stream=content, filetype="pdf")
            texts = []
            for page in doc:
                texts.append(page.get_text())
            doc.close()
            result = "\n".join(texts)
            if not result.strip():
                raise ParseError("L1: PDF 无可提取文本（可能是扫描件）", level=1)
            return result
        except ParseError:
            raise
        except Exception as e:
            raise ParseError(f"L1: PDF 解析失败: {e}", level=1)

    def _parse_docx_l1(self, content: bytes) -> str:
        """L1 DOCX：python-docx 提取段落文本。"""
        if not _DOCX_AVAILABLE:
            raise ParseError("L1: python-docx 未安装", level=1)
        try:
            import io
            document = docx.Document(io.BytesIO(content))
            paragraphs = [p.text for p in document.paragraphs if p.text.strip()]
            result = "\n".join(paragraphs)
            if not result.strip():
                raise ParseError("L1: DOCX 无文本内容", level=1)
            return result
        except ParseError:
            raise
        except Exception as e:
            raise ParseError(f"L1: DOCX 解析失败: {e}", level=1)

    def _parse_image_waterfall(self, content: bytes, filename: str) -> str:
        """图片专用分层 OCR：PaddleOCR → Tesseract+预处理 → vision_llm。

        每层只在前层「不可用/空/低置信度」时触发。最终失败抛 ParseError(level=3)。
        """
        errors = []

        # L1: PaddleOCR 主引擎
        if self.ocr_primary_engine == "paddle" and _PADDLE_AVAILABLE:
            try:
                text, confidence = self._ocr_paddle_l1(content)
                if text and text.strip() and confidence >= self.ocr_paddle_min_confidence:
                    logger.info("OCR L1 PaddleOCR 命中: conf=%.3f", confidence)
                    return text
                if text and text.strip():
                    logger.info(
                        "OCR L1 PaddleOCR 低置信度降级: conf=%.3f < %.3f",
                        confidence, self.ocr_paddle_min_confidence,
                    )
                else:
                    errors.append("L1 PaddleOCR: 返回空内容")
            except ParseError as e:
                errors.append(str(e))
            except Exception as e:
                errors.append(f"L1 PaddleOCR: 未知错误: {e}")
        elif self.ocr_primary_engine == "paddle":
            errors.append("L1 PaddleOCR: 依赖未安装，跳过")

        # L2: Tesseract + 预处理
        if self.ocr_primary_engine != "none" and _TESSERACT_AVAILABLE and _PIL_AVAILABLE:
            try:
                text = self._ocr_tesseract_l2(content)
                if text and text.strip():
                    logger.info("OCR L2 Tesseract 命中")
                    return text
                errors.append("L2 Tesseract: 返回空内容")
            except ParseError as e:
                errors.append(str(e))
            except Exception as e:
                errors.append(f"L2 Tesseract: 未知错误: {e}")
        elif not (_TESSERACT_AVAILABLE and _PIL_AVAILABLE):
            errors.append("L2 Tesseract: 依赖未安装")

        # L3: 视觉 LLM 终极兜底（本次仅预留钩子）
        if self.ocr_vision_llm_enabled:
            try:
                text = self._ocr_vision_llm_l3(content, filename)
                if text and text.strip():
                    logger.info("OCR L3 vision_llm 命中")
                    return text
                errors.append("L3 vision_llm: 返回空内容")
            except ParseError as e:
                errors.append(str(e))
            except Exception as e:
                errors.append(f"L3 vision_llm: 未知错误: {e}")
        else:
            errors.append("L3 vision_llm: 未启用")

        raise ParseError(
            f"图片 OCR 三层均失败: {'; '.join(errors)}", level=3
        )

    def _ocr_paddle_l1(self, content: bytes) -> tuple:
        """L1 PaddleOCR：返回 (文本, 平均置信度)。

        使用模块级单例 + threading.Lock 串行调用。软超时记 warning 不强制终止
        （避免 ThreadPoolExecutor 超时后 Lock 不释放导致下次调用永久卡死）。
        异常时设 _paddle_poisoned=True，下次调用前重建实例。
        """
        global _paddle_poisoned
        if not _PADDLE_AVAILABLE:
            raise ParseError("L1: paddleocr 未安装", level=1)
        if not _PIL_AVAILABLE:
            raise ParseError("L1: Pillow 未安装", level=1)
        try:
            import io
            import time
            import numpy as np
            pil_image = Image.open(io.BytesIO(content))
            # PaddleOCR 2.x 不支持 PIL.Image，需转 numpy.ndarray
            image = np.array(pil_image.convert("RGB"))
            ocr = _get_paddle_singleton(self.ocr_paddle_lang, self.ocr_paddle_use_gpu)
            start = time.time()
            result = ocr.ocr(image, cls=True)
            latency = time.time() - start
            if latency > self.ocr_paddle_infer_timeout:
                logger.warning(
                    "PaddleOCR 推理耗时 %.1fs 超过软超时 %ds（不强制终止）",
                    latency, self.ocr_paddle_infer_timeout,
                )
            # PaddleOCR 返回 [[bbox, (text, conf)], ...]，可能为 None
            if not result:
                self._record_ocr_metric("paddle", True, latency * 1000)
                return "", 0.0
            texts = []
            confs = []
            for line in result:
                if line is None:
                    continue
                for item in line:
                    if item is None or len(item) < 2:
                        continue
                    text_part = item[1][0]
                    conf_part = float(item[1][1])
                    texts.append(text_part)
                    confs.append(conf_part)
            text = "\n".join(texts)
            avg_conf = sum(confs) / len(confs) if confs else 0.0
            self._record_ocr_metric("paddle", True, latency * 1000)
            return text, avg_conf
        except ParseError:
            raise
        except Exception as e:
            _paddle_poisoned = True
            self._record_ocr_metric("paddle", False, 0)
            raise ParseError(f"L1 PaddleOCR 推理失败: {e}", level=1)

    def _ocr_tesseract_l2(self, content: bytes) -> str:
        """L2 Tesseract + 轻量预处理。

        preprocess=true 且 _CV2_AVAILABLE 时：灰度 → Otsu 二值化 → 中值滤波。
        修复 lang 读取：从 self.ocr_tesseract_lang 取，不再硬编码。
        """
        if not _TESSERACT_AVAILABLE:
            raise ParseError("L2: pytesseract 未安装", level=1)
        if not _PIL_AVAILABLE:
            raise ParseError("L2: Pillow 未安装", level=1)
        try:
            import io
            import time
            if self.ocr_tesseract_preprocess and _CV2_AVAILABLE:
                content = self._preprocess_image(content)
            image = Image.open(io.BytesIO(content))
            start = time.time()
            text = pytesseract.image_to_string(image, lang=self.ocr_tesseract_lang)
            latency = time.time() - start
            self._record_ocr_metric("tesseract", True, latency * 1000)
            return text.strip()
        except ParseError:
            raise
        except Exception as e:
            self._record_ocr_metric("tesseract", False, 0)
            raise ParseError(f"L2 Tesseract 解析失败: {e}", level=1)

    def _ocr_vision_llm_l3(self, content: bytes, filename: str) -> str:
        """L3 视觉 LLM 终极兜底（本次仅预留钩子，enabled=false 时直接 raise）。

        未来接入 Qwen-VL-Max 等多模态模型时在此填充实现。
        """
        if not self.ocr_vision_llm_enabled:
            raise ParseError("L3: vision_llm 未启用", level=3)
        # TODO: 未来实现视觉 LLM 调用（Qwen-VL-Max via DashScope OpenAI 兼容端点）
        raise ParseError("L3: vision_llm 尚未实现", level=3)

    def _preprocess_image(self, content: bytes) -> bytes:
        """图片预处理：灰度 → Otsu 二值化 → 中值滤波去噪。

        仅用于 L2 Tesseract 路径，PaddleOCR 内部自带预处理无需外挂。
        """
        import io
        import numpy as np
        image = Image.open(io.BytesIO(content)).convert("RGB")
        arr = np.array(image)
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        denoised = cv2.medianBlur(binary, 3)
        out = Image.fromarray(denoised)
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        return buf.getvalue()

    def _record_ocr_metric(self, engine: str, success: bool, latency_ms: float) -> None:
        """best-effort 埋点 OCR 调用，metrics 缺失时静默跳过。"""
        if self._metrics is None:
            return
        try:
            self._metrics.observe_ocr_call(engine, success, latency_ms)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # L2: 编码修复 + 乱码检测
    # ------------------------------------------------------------------

    def _parse_l2(self, content: bytes) -> str:
        """L2 层：尝试多种编码回退，检测乱码。

        对二进制格式（PDF/DOCX）的字节，L2 通常无效（编码修复不适用于
        二进制数据），直接抛出 ParseError 让调用方决定是否继续 L3。

        Args:
            content: 原始字节内容。

        Returns:
            解码后的文本。

        Raises:
            ParseError: 所有编码尝试失败或检测到乱码。
        """
        last_error = None
        for encoding in self._FALLBACK_ENCODINGS:
            try:
                text = content.decode(encoding)
                # 乱码检测
                if self._is_garbled(text):
                    last_error = ParseError(
                        f"L2: {encoding} 解码后检测到乱码", level=2
                    )
                    continue
                return text
            except (UnicodeDecodeError, UnicodeError) as e:
                last_error = ParseError(
                    f"L2: {encoding} 解码失败: {e}", level=2
                )
        raise last_error or ParseError("L2: 所有编码回退失败", level=2)

    def _is_garbled(self, text: str) -> bool:
        """检测文本是否包含乱码。

        启发式规则：
        - 空文本或纯空白 → 不算乱码（可能是空文件）
        - 含可打印 ASCII 为主（>80%）→ 不算乱码（英文文本）
        - 含正常中文 + 少量未知字符 → 不算乱码
        - 含大量替换字符（U+FFFD）→ 乱码
        - 含大量控制字符（非空白控制字符占比 > 5%）→ 乱码

        Args:
            text: 待检测文本。

        Returns:
            True 表示疑似乱码。
        """
        if not text or not text.strip():
            return False

        total = len(text)
        # U+FFFD 替换字符
        replacement_count = text.count('�')
        if replacement_count / total > 0.1:
            return True

        # 控制字符（排除常见空白：\t \n \r）
        control_count = sum(
            1 for c in text
            if ord(c) < 32 and c not in '\t\n\r'
        )
        if control_count / total > 0.05:
            return True

        return False

    # ------------------------------------------------------------------
    # L3: LLM 降级
    # ------------------------------------------------------------------

    def _parse_l3(self, content: bytes, filename: str) -> str:
        """L3 层：截取前 10KB 送 LLM 提取文本。

        Args:
            content: 原始字节内容。
            filename: 原始文件名。

        Returns:
            LLM 提取的文本。

        Raises:
            ParseError: L3 未启用或 LLM 调用失败。
        """
        if not self.llm_fallback_enabled:
            raise ParseError("L3: LLM 降级未启用", level=3)

        if self.llm_client is None:
            raise ParseError("L3: LLM 客户端未配置", level=3)

        # 截取前 10KB
        sample = content[:10240]
        # 尝试 UTF-8 解码
        try:
            sample_text = sample.decode("utf-8", errors="replace")
        except Exception:
            sample_text = str(sample)

        prompt = (
            f"以下是一个文件的前 10KB 内容。请从中提取所有可读的文字内容，"
            f"忽略二进制数据。文件名: {filename}\n\n"
            f"```\n{sample_text}\n```\n\n"
            f"请输出提取到的文字内容："
        )

        try:
            response = self.llm_client.chat([{"role": "user", "content": prompt}])
            # 从响应中提取文本
            if hasattr(response, "content"):
                # Anthropic SDK 响应
                result = response.content
                if isinstance(result, list):
                    result = "".join(
                        b.get("text", "") for b in result if isinstance(b, dict)
                    )
            elif isinstance(response, dict):
                result = response.get("content", "")
            else:
                result = str(response)

            if not result or not result.strip():
                raise ParseError("L3: LLM 返回空内容", level=3)
            return result
        except ParseError:
            raise
        except Exception as e:
            raise ParseError(f"L3: LLM 调用失败: {e}", level=3)

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def parse(self, content: bytes, filename: str) -> str:
        """解析文件内容为纯文本（瀑布式降级）。

        尝试 L1 → L2 → L3，任一层返回非空结果即停止。
        所有层均失败时抛出 :class:`ParseError`。

        对图片类文件，L1 即 OCR，失败后不尝试 L2/L3（二进制无意义）。

        Args:
            content: 文件字节内容。
            filename: 原始文件名。

        Returns:
            解析后的纯文本。

        Raises:
            ParseError: 所有层均解析失败。
        """
        ext = os.path.splitext(filename)[1].lower()
        timeout = self._get_timeout(filename)
        is_image = ext in (".png", ".jpg", ".jpeg", ".gif")

        # 图片走专用分层通道（PaddleOCR → Tesseract → vision_llm），
        # 跳过通用 L1/L2/L3（文本类专用，对二进制图片无意义）
        if is_image:
            return self._parse_image_waterfall(content, filename)

        errors = []

        # L1：库解析（线程池超时保护）
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self._parse_l1, content, filename)
                result = future.result(timeout=timeout)
                if result and result.strip():
                    return result
                errors.append("L1: 返回空内容")
        except FuturesTimeoutError:
            errors.append(f"L1: 超时 ({timeout}s)")
        except ParseError as e:
            errors.append(str(e))
        except Exception as e:
            errors.append(f"L1: 未知错误: {e}")

        # L2：编码修复
        try:
            result = self._parse_l2(content)
            if result and result.strip():
                return result
            errors.append("L2: 返回空内容")
        except ParseError as e:
            errors.append(str(e))

        # L3：LLM 降级
        try:
            result = self._parse_l3(content, filename)
            if result and result.strip():
                return result
            errors.append("L3: 返回空内容")
        except ParseError as e:
            errors.append(str(e))

        raise ParseError(
            f"文件解析失败（{filename}）: {'; '.join(errors)}",
            level=3,
        )
