#!/usr/bin/env python3
"""国家中小学智慧教育平台 · 电子教材下载器

用法示例：
    python download.py --search "苏教版 三年级 数学 上册"
    python download.py --grade 八年级 --subject 物理 --version 苏科版
    python download.py --list --grade 三年级 --subject 数学
    python download.py --url "https://r3-ndr.ykt.cbern.com.cn/.../image/1.jpg"
    python download.py --batch books.txt

仅下载平台公开提供的电子课本，供个人教学/学习使用。
不绕过任何鉴权，不下载需要登录的资源。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

MIN_PYTHON = (3, 8)
DEFAULT_ROOT_NAME = "教材下载"


def _setup_console() -> None:
    """Windows 控制台默认是 GBK，中文/符号输出会抛 UnicodeEncodeError。"""
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


def _preflight() -> None:
    """在导入任何自有模块之前检查运行环境。

    必须放在 import catalog 之前：catalog 顶部就 import requests，
    缺依赖时用户会先看到 ModuleNotFoundError 堆栈，友好提示根本来不及执行。
    """
    if sys.version_info < MIN_PYTHON:
        print(f"需要 Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 或更高版本，"
              f"当前为 {sys.version.split()[0]}")
        sys.exit(2)

    missing = []
    try:
        import requests  # noqa: F401
    except ImportError:
        missing.append("requests")

    has_img2pdf = has_pillow = True
    try:
        import img2pdf  # noqa: F401
    except ImportError:
        has_img2pdf = False
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        has_pillow = False
    if not has_img2pdf and not has_pillow:
        missing.append("Pillow")   # 两者至少要有一个才能合成 PDF

    if missing:
        print("缺少运行依赖：" + "、".join(missing))
        print()
        print("请先安装：")
        print("    pip install -r requirements.txt")
        print("或：")
        print("    pip install requests Pillow img2pdf")
        sys.exit(2)


_setup_console()
_preflight()

import catalog  # noqa: E402
import fetcher  # noqa: E402
import paths  # noqa: E402
import pdfmake  # noqa: E402


# ────────────────────────── 选书 ──────────────────────────

def resolve_book(args, books: list[catalog.Book]) -> catalog.Book | None:
    """根据参数确定唯一一本教材。返回 None 表示无法确定（已打印原因）。"""
    if args.url or (args.id and args.ts):
        if args.url:
            cid, ts = catalog.parse_coord_url(args.url)
        else:
            cid, ts = args.id, args.ts
        # 先在目录里反查，命中就能拿到完整元数据用于建目录
        for b in books:
            if b.content_id == cid:
                if b.timestamp != ts:
                    print(f"  [!] 你提供的时间戳 {ts} 与目录记录的 {b.timestamp} 不同。")
                    print(f"      将使用你提供的值。若下载结果页数异常，请改用 --search。")
                    b = catalog.Book(**{**b.to_dict(), "timestamp": ts})
                return b
        print("  [!] 该教材不在目录索引中，将以最简信息保存到「未分类」目录。")
        return catalog.Book(
            id=cid, title=args.name or f"教材_{cid[:8]}", stage="", grade="",
            volume="", subject="", version="", publisher="",
            content_id=cid, timestamp=ts,
        )

    hits = catalog.search(
        books, args.search or "",
        stage=args.stage, grade=args.grade, volume=args.volume,
        subject=args.subject, version=args.version,
    )

    if not hits:
        print("没有匹配的教材。")
        narrowed = catalog.search(books, "", grade=args.grade, subject=args.subject)
        if narrowed:
            print(f"\n在 {args.grade or ''}{args.subject or ''} 下，可选的版本有：")
            for v in catalog.distinct(narrowed, "version"):
                print(f"    {v}")
        else:
            print("\n提示：先用 --list 配合 --grade / --subject 查看可选项，例如")
            print('    python download.py --list --grade 三年级 --subject 数学')
        return None

    if len(hits) == 1:
        return hits[0]

    print(f"匹配到 {len(hits)} 本，请选择：\n")
    for i, b in enumerate(hits[:40], 1):
        print(f"  {i:2d}. {b.label()}")
    if len(hits) > 40:
        print(f"  …… 另有 {len(hits) - 40} 本未显示，请增加筛选条件")

    if args.yes or not sys.stdin.isatty():
        print("\n条件不唯一，请补充 --version / --volume 等参数后重试。")
        return None

    try:
        raw = input("\n输入编号（直接回车取消）：").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not raw.isdigit() or not (1 <= int(raw) <= min(len(hits), 40)):
        print("已取消。")
        return None
    return hits[int(raw) - 1]


# ────────────────────────── 下载一本 ──────────────────────────

def download_book(book: catalog.Book, args) -> bool:
    root = Path(args.out).expanduser() if args.out else paths.desktop_dir() / DEFAULT_ROOT_NAME
    out_dir = paths.build_out_dir(
        root, book.grade, book.volume, book.subject, book.version, book.publisher
    )
    pdf_path = out_dir / f"{paths.safe_name(book.title)}.pdf"

    print(f"\n>> {book.title}")
    if book.stage or book.version:
        print(f"   {book.stage} {book.grade}{book.volume} · {book.subject} · "
              f"{book.version} {book.publisher}".replace("  ", " ").strip())

    if pdf_path.exists() and not args.force:
        print(f"   已存在，跳过：{pdf_path}")
        print("   （如需重新下载，加 --force）")
        return True

    print("   探测页数…", end=" ", flush=True)
    try:
        total = fetcher.probe_page_count(book, quiet=args.quiet)
    except fetcher.FetchError as exc:
        print(f"\n   [x] {exc}")
        return False
    print(f"共 {total} 页")

    tmp_root = Path(args.keep_pages_dir) if args.keep_pages_dir else Path(tempfile.gettempdir())
    pages_dir = tmp_root / f"smartedu_{book.content_id[:8]}"

    imgs, failed = fetcher.download_pages(
        book, total, pages_dir, jobs=args.jobs, slow=args.slow, quiet=args.quiet
    )

    if failed:
        print(f"   [!] 有 {len(failed)} 页下载失败：{failed[:12]}"
              f"{' …' if len(failed) > 12 else ''}")
        if len(failed) > total * 0.02:
            print("   [x] 失败比例过高，已中止合成。请稍后重试（断点续传会跳过已完成的页）。")
            return False
        print("   将跳过失败页继续合成，请注意 PDF 会缺页。")

    print(f"   合成 PDF（{pdfmake.backend_name(args.compress)}）…", end=" ", flush=True)
    try:
        pdfmake.make_pdf(imgs, pdf_path, compress=args.compress)
    except Exception as exc:
        print(f"\n   [x] 合成失败：{exc}")
        return False

    ok, detail = pdfmake.verify_pdf(pdf_path, expect_pages=len(imgs))
    size = paths.human_size(pdf_path.stat().st_size)
    print(f"{'完成' if ok else '异常'}（{detail}，{size}）")
    if not ok:
        print(f"   [x] PDF 自检未通过：{detail}")
        return False

    info = {
        "title": book.title,
        "stage": book.stage, "grade": book.grade, "volume": book.volume,
        "subject": book.subject, "version": book.version, "publisher": book.publisher,
        "content_id": book.content_id, "timestamp": book.timestamp,
        "pages": len(imgs), "failed_pages": failed,
        "downloaded_at": datetime.now().isoformat(timespec="seconds"),
        "source": "国家中小学智慧教育平台 basic.smartedu.cn",
        "image_url_pattern": book.image_url(0).replace("/0.jpg", "/{page}.jpg"),
    }
    (out_dir / "info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if not args.keep_pages:
        shutil.rmtree(pages_dir, ignore_errors=True)

    print(f"   保存至：{pdf_path}")
    return True


# ────────────────────────── 入口 ──────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="download.py",
        description="下载国家中小学智慧教育平台的电子教材并合成 PDF",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    g = p.add_argument_group("选择教材")
    g.add_argument("--search", help='关键词，如 "苏教版 三年级 数学 上册"')
    g.add_argument("--stage", help="学段：小学/初中/高中")
    g.add_argument("--grade", help="年级，如 三年级")
    g.add_argument("--volume", help="册次：上册/下册/全一册")
    g.add_argument("--subject", help="科目，如 数学")
    g.add_argument("--version", help="版本，如 苏教版")
    g.add_argument("--url", help="回退方式：平台上教材封面图的图片地址")
    g.add_argument("--id", help="回退方式：contentId")
    g.add_argument("--ts", help="回退方式：timestamp")
    g.add_argument("--name", help="配合 --url 使用，自定义文件名")
    g.add_argument("--batch", help="批量：文件路径，一行一个搜索词或图片地址")

    o = p.add_argument_group("输出与行为")
    o.add_argument("--out", help="输出根目录（默认：桌面/教材下载）")
    o.add_argument("--list", action="store_true", help="只列出匹配结果，不下载")
    o.add_argument("--jobs", type=int, default=8, help="并发数，默认 8，上限 16")
    o.add_argument("--slow", action="store_true", help="弱网模式，降低请求频率")
    o.add_argument("--compress", action="store_true",
                   help="压缩 PDF（重编码，体积约为无损版 1/3，画质略降）")
    o.add_argument("--keep-pages", action="store_true", help="保留单页图片")
    o.add_argument("--keep-pages-dir", help="单页图片的存放目录")
    o.add_argument("--force", action="store_true", help="已存在时也重新下载")
    o.add_argument("--refresh", action="store_true", help="强制刷新教材目录缓存")
    o.add_argument("--yes", action="store_true", help="非交互模式")
    o.add_argument("--quiet", action="store_true", help="减少输出")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        books = catalog.fetch_catalog(refresh=args.refresh, quiet=args.quiet)
    except catalog.CatalogError as exc:
        print(f"[x] {exc}")
        return 1

    if args.list:
        if args.url or (args.id and args.ts):
            try:
                b = resolve_book(args, books)
            except catalog.CatalogError as exc:
                print(f"[x] {exc}")
                return 1
            if b:
                print(f"\n{b.label()}")
                print(f"  contentId : {b.content_id}")
                print(f"  timestamp : {b.timestamp}")
            return 0 if b else 1

        hits = catalog.search(
            books, args.search or "",
            stage=args.stage, grade=args.grade, volume=args.volume,
            subject=args.subject, version=args.version,
        )
        print(f"\n匹配 {len(hits)} 本：\n")
        for b in hits[:80]:
            print(f"  {b.label()}")
        if len(hits) > 80:
            print(f"\n  …… 另有 {len(hits) - 80} 本，请增加筛选条件")
        return 0

    targets: list[catalog.Book] = []

    if args.batch:
        lines = [l.strip() for l in Path(args.batch).read_text(encoding="utf-8").splitlines()]
        for line in [l for l in lines if l and not l.startswith("#")]:
            sub = argparse.Namespace(**vars(args))
            if line.startswith("http"):
                sub.url, sub.search = line, None
            else:
                sub.url, sub.search = None, line
            sub.yes = True
            try:
                b = resolve_book(sub, books)
            except catalog.CatalogError as exc:
                print(f"  [!] 跳过（{exc.args[0].splitlines()[0]}）：{line}")
                continue
            if b:
                targets.append(b)
            else:
                print(f"  [!] 跳过无法确定的条目：{line}")
    else:
        if not any([args.search, args.url, args.id, args.stage, args.grade,
                    args.subject, args.version, args.volume]):
            build_parser().print_help()
            return 0
        try:
            b = resolve_book(args, books)
        except catalog.CatalogError as exc:
            print(f"[x] {exc}")
            return 1
        if not b:
            return 1
        targets.append(b)

    if not targets:
        print("没有可下载的教材。")
        return 1

    ok_count = 0
    for i, book in enumerate(targets, 1):
        if len(targets) > 1:
            print(f"\n[{i}/{len(targets)}]", end="")
        if download_book(book, args):
            ok_count += 1
        if i < len(targets):
            time.sleep(1.0)

    print(f"\n完成：{ok_count}/{len(targets)} 本")
    if ok_count:
        print("提示：请翻到 PDF 的版权页核对「版次」，确认与孩子在用的课本一致。")
    return 0 if ok_count == len(targets) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。已下载的页面会保留，重新运行可断点续传。")
        sys.exit(130)
