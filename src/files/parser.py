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
    ) -> None:
        """初始化解析器。

        Args:
            llm_fallback_enabled: 是否启用 L3 LLM 降级（默认关）。
            llm_client: LLM 客户端实例，需提供 ``chat(messages)`` 接口。
                L3 关闭时可为 None。
            parse_timeouts: 按扩展名的超时配置，如 ``{'.pdf': 120, '.docx': 15}``。
                缺省使用 ``_default`` 键的值（默认 30 秒）。
        """
        self.llm_fallback_enabled = llm_fallback_enabled
        self.llm_client = llm_client
        self.parse_timeouts = parse_timeouts or {"_default": 30}

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
            return self._parse_image_l1(content)

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

    def _parse_image_l1(self, content: bytes) -> str:
        """L1 图片：OCR 提取文字（仅文字，不描述不生成）。"""
        if not _TESSERACT_AVAILABLE:
            raise ParseError("L1: pytesseract 未安装", level=1)
        if not _PIL_AVAILABLE:
            raise ParseError("L1: Pillow 未安装", level=1)
        try:
            import io
            image = Image.open(io.BytesIO(content))
            text = pytesseract.image_to_string(image, lang="chi_sim+eng")
            result = text.strip()
            if not result:
                raise ParseError("L1: OCR 未识别到文字", level=1)
            return result
        except ParseError:
            raise
        except Exception as e:
            raise ParseError(f"L1: OCR 失败: {e}", level=1)

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

        # 图片类不尝试 L2/L3
        if is_image:
            raise ParseError(
                f"图片解析失败: {'; '.join(errors)}", level=1
            )

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
