"""页面探测与并发下载。

设计要点：
1. 页数用「倍增 + 二分」定位，约 12-16 次请求即可，不是逐页试探。
2. 平台对不存在的页返回 403（不是 404），而限流同样返回 403 ——
   两者必须区分，否则会把限流误判成"书到头了"，静默少下几十页。
   做法：判定末页前二次确认，中间等待。
3. 三个镜像 r1/r2/r3 内容一致，失败时轮换而不是死等同一个。
4. 每页下完校验 JPEG 首尾标记，截断的文件重下——半张图合进 PDF
   是最难发现的错误。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from catalog import Book, HEADERS

HOSTS = ("r1", "r2", "r3")
MAX_PAGES = 1024          # 安全上界，正常教材远小于此
MIN_VALID_BYTES = 2048    # 小于此视为无效/错误页面


class FetchError(RuntimeError):
    pass


def _probe(session: requests.Session, book: Book, page: int,
           host_idx: int = 0, retries: int = 2) -> bool:
    """探测某页是否存在。只取前 1KB，不下整页。"""
    # 只取头部即可判断存在性，不下整页
    probe_headers = {**HEADERS, "Range": "bytes=0-1023"}
    for attempt in range(retries + 1):
        # 换镜像本身就绕开了单节点的缓存/限流，不需要再加随机 query
        host = HOSTS[(host_idx + attempt) % len(HOSTS)]
        try:
            r = session.get(book.image_url(page, host), headers=probe_headers,
                            timeout=20, stream=True)
            code = r.status_code
            r.close()
            if code in (200, 206):
                return True
            if code == 403:
                return False
            # 5xx / 429 之类：换镜像重试
        except requests.RequestException:
            pass
        time.sleep(1.0 * (attempt + 1))
    return False


def probe_page_count(book: Book, quiet: bool = False) -> int:
    """返回教材总页数。倍增找上界，再二分定位最后一页。"""
    with requests.Session() as s:
        if not _probe(s, book, 1):
            raise FetchError(
                "第 1 页就无法访问。可能是坐标已失效，或平台调整了资源路径。"
            )

        lo, hi = 1, 2
        while hi <= MAX_PAGES and _probe(s, book, hi):
            lo, hi = hi, hi * 2
        if hi > MAX_PAGES:
            raise FetchError(f"页数超过安全上界 {MAX_PAGES}，疑似探测异常")

        while hi - lo > 1:
            mid = (lo + hi) // 2
            if _probe(s, book, mid):
                lo = mid
            else:
                hi = mid

        # 关键：确认 lo+1 真的不存在，而不是刚好撞上限流
        time.sleep(1.5)
        if _probe(s, book, lo + 1, host_idx=1):
            if not quiet:
                print("  [!] 末页判定存在歧义（疑似限流），重新探测…")
            time.sleep(3)
            hi2 = lo + 1
            while hi2 <= MAX_PAGES and _probe(s, book, hi2, host_idx=1):
                hi2 += 1
                time.sleep(0.3)
            lo = hi2 - 1

    return lo


def _valid_jpeg(path: Path) -> bool:
    """校验 JPEG 首尾标记，挡住截断的文件。"""
    try:
        if path.stat().st_size < MIN_VALID_BYTES:
            return False
        with path.open("rb") as f:
            if f.read(2) != b"\xff\xd8":
                return False
            f.seek(-2, 2)
            return f.read(2) == b"\xff\xd9"
    except OSError:
        return False


def _download_one(book: Book, page: int, dest: Path, delay: float) -> tuple[int, bool]:
    """下载单页，最多 3 次尝试，每次换一个镜像。"""
    if dest.exists() and _valid_jpeg(dest):
        return page, True  # 断点续传：已完成的跳过

    for attempt in range(3):
        host = HOSTS[attempt % len(HOSTS)]
        try:
            if delay:
                time.sleep(delay)
            r = requests.get(book.image_url(page, host), headers=HEADERS, timeout=60)
            if r.status_code == 200 and len(r.content) >= MIN_VALID_BYTES:
                tmp = dest.with_suffix(".part")
                tmp.write_bytes(r.content)
                if _valid_jpeg(tmp):
                    tmp.replace(dest)
                    return page, True
                tmp.unlink(missing_ok=True)
        except (requests.RequestException, OSError):
            pass
        time.sleep(2 ** attempt)

    return page, False


def download_pages(book: Book, total: int, out_dir: Path,
                   jobs: int = 8, slow: bool = False,
                   quiet: bool = False) -> tuple[list[Path], list[int]]:
    """并发下载全部页面。返回 (有效文件列表, 失败页码列表)。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = max(1, min(int(jobs), 16))       # 硬上限 16，不提供解除开关
    delay = 0.35 if slow else 0.0

    files: dict[int, Path] = {}
    failed: list[int] = []
    done = 0

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {}
        for p in range(1, total + 1):
            dest = out_dir / f"{p:04d}.jpg"
            files[p] = dest
            futures[pool.submit(_download_one, book, p, dest, delay)] = p

        for fut in as_completed(futures):
            page, ok = fut.result()
            done += 1
            if not ok:
                failed.append(page)
            if not quiet and (done % 10 == 0 or done == total):
                bar_len = 28
                filled = int(bar_len * done / total)
                print(f"\r  下载中 [{'█' * filled}{'░' * (bar_len - filled)}] "
                      f"{done}/{total}", end="", flush=True)

    if not quiet:
        print()

    ordered = [files[p] for p in sorted(files) if p not in failed and files[p].exists()]
    return ordered, sorted(failed)
