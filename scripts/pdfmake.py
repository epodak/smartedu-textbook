"""把下载好的整册页面图合成单个 PDF。

两条路径，取舍是「无损 vs 体积」——实测同一册 118 页：

  img2pdf  62.6 MB   把 JPEG 原样嵌入，不重新编码。无损，画质等同原件，
                     但体积约等于原图总和。默认走这条。
  Pillow   19.9 MB   重新编码 JPEG（有损）。体积约为前者 1/3，
                     教材阅读足够，适合需要传输/存档多册的场景。走 --compress。

Pillow 路径的内存陷阱：Image.open() 是惰性的，不会立刻读像素，
但只要调用 convert()/load()/save() 就会解码。一册 130 页 1353×1885
的图全部解码约 1 GB，低配机器会 OOM。因此只对少数非 RGB 图单独转换，
其余交给 save() 逐张流式处理。
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["make_pdf", "backend_name"]

JPEG_QUALITY = 82   # 压缩模式下的质量，教材小字在 82 仍清晰


def backend_name(compress: bool = False) -> str:
    if compress:
        return "Pillow/压缩"
    try:
        import img2pdf  # noqa: F401
        return "img2pdf/无损"
    except ImportError:
        return "Pillow/压缩"


def make_pdf(images: list[Path], out_file: Path, dpi: int = 150,
             compress: bool = False) -> Path:
    if not images:
        raise ValueError("没有可合成的页面")

    out_file.parent.mkdir(parents=True, exist_ok=True)

    if compress:
        return _via_pillow(images, out_file, dpi)

    try:
        return _via_img2pdf(images, out_file, dpi)
    except ImportError:
        return _via_pillow(images, out_file, dpi)


def _via_img2pdf(images: list[Path], out_file: Path, dpi: int) -> Path:
    """无损路径：JPEG 原样嵌入，不解码不重编码。

    dpi 这里不强制指定——img2pdf 会读取 JPEG 自带的分辨率信息来定页面尺寸，
    强行覆盖反而可能让页面比例失真。教材扫描件页面尺寸不影响阅读。
    """
    import img2pdf

    with out_file.open("wb") as f:
        f.write(img2pdf.convert([str(p) for p in images]))
    return out_file


def _via_pillow(images: list[Path], out_file: Path, dpi: int) -> Path:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "缺少 PDF 合成依赖。请安装：pip install img2pdf Pillow"
        ) from exc

    # 惰性打开：此时不解码像素
    handles = [Image.open(p) for p in images]
    try:
        # 只有非 RGB 的才转换，避免整册解码进内存
        first = handles[0]
        first_rgb = first if first.mode == "RGB" else first.convert("RGB")
        rest = [h if h.mode == "RGB" else h.convert("RGB") for h in handles[1:]]

        first_rgb.save(
            out_file, "PDF", save_all=True,
            append_images=rest, resolution=float(dpi),
            quality=JPEG_QUALITY,
        )
    finally:
        for h in handles:
            try:
                h.close()
            except Exception:
                pass

    return out_file


def verify_pdf(path: Path, expect_pages: int | None = None) -> tuple[bool, str]:
    """轻量校验：文件头、EOF 标记、页对象数量。

    不引入额外依赖——发布级工具应当能在只装了 requests+Pillow 的环境里
    自检，而不是要求用户再装一个 PDF 解析库。
    """
    import re

    try:
        data = path.read_bytes()
    except OSError as exc:
        return False, f"无法读取：{exc}"

    if not data.startswith(b"%PDF-"):
        return False, "文件头不是 %PDF-"
    if not data.rstrip().endswith(b"%%EOF"):
        return False, "缺少 %%EOF 结尾，文件可能被截断"

    pages = len(re.findall(rb"/Type\s*/Page[^s]", data))
    if expect_pages is not None and pages != expect_pages:
        return False, f"页对象数 {pages} 与预期 {expect_pages} 不符"

    return True, f"{pages} 页"
