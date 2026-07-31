"""教材目录：拉取、缓存、检索、坐标解析。

数据源选择（关键，选错会下到旧版教材）：

  只用「目录分片」 .../resources/tch_material/version/data_version.json
                    -> part_*.json

  不要用单本详情接口 .../resources/tch_material/details/{id}.json ——
  实测它返回的是 2023 年的过期资源坐标，用它下载会得到页数更少的旧版教材，
  而且不会报错（能下、能合成、页数看着正常），只有翻到版权页才发现不对。
  2024-2025 正是义务教育教材按 2022 版新课标换版的窗口，这个错误代价很大。

  另有一个常见错误路径 .../zxx/ndrs/tch_material/version/data_version.json
  （少了 resources/），返回 403。网上多数教程卡在这里，误以为接口已下线。
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from pathlib import Path

import requests

VERSION_URL = (
    "https://s-file-1.ykt.cbern.com.cn"
    "/zxx/ndrs/resources/tch_material/version/data_version.json"
)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://basic.smartedu.cn/"}

CACHE_DIR = Path.home() / ".cache" / "smartedu"

# preview 里的图片地址形如：
#   https://r3-ndr.ykt.cbern.com.cn/edu_product/esp/assets/{uuid}.t/zh-CN/{ts}/transcode/image/1.jpg
COORD_RE = re.compile(
    r"https://(?P<host>r\d)-ndr\.ykt\.cbern\.com\.cn/edu_product/esp/assets/"
    r"(?P<cid>[0-9a-fA-F-]{36})\.t/zh-CN/(?P<ts>\d+)/transcode/image/\d+\.jpg"
)

STAGES = ("小学", "初中", "高中")
VOLUMES = ("上册", "下册", "全一册")
# 这些 tag 是分类噪声，不参与科目识别
NOISE_TAGS = {"电子教材", "教材", "教科书"}


class CatalogError(RuntimeError):
    pass


@dataclass
class Book:
    id: str
    title: str
    stage: str
    grade: str
    volume: str
    subject: str
    version: str
    publisher: str
    content_id: str
    timestamp: str

    def image_url(self, page: int, host: str = "r1") -> str:
        return (f"https://{host}-ndr.ykt.cbern.com.cn/edu_product/esp/assets/"
                f"{self.content_id}.t/zh-CN/{self.timestamp}/transcode/image/{page}.jpg")

    def label(self) -> str:
        bits = [b for b in (self.stage, self.grade + self.volume,
                            self.subject, self.version) if b]
        return f"{self.title}  [{' · '.join(bits)}]"

    def to_dict(self) -> dict:
        return asdict(self)


# ────────────────────────── 目录获取 ──────────────────────────

def _get_json(url: str, timeout: int = 60) -> object:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_catalog(refresh: bool = False, quiet: bool = False) -> list[Book]:
    """取得全量教材目录。优先用本地缓存，按 module_version 判断是否过期。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / "catalog.json"
    stamp_file = CACHE_DIR / "version.txt"

    try:
        index = _get_json(VERSION_URL, timeout=30)
        remote_ver = str(index.get("module_version", ""))
        part_urls = [u.strip() for u in str(index.get("urls", "")).split(",") if u.strip()]
    except Exception as exc:  # 离线时允许降级使用旧缓存
        if cache_file.exists() and not refresh:
            if not quiet:
                print(f"  [!] 无法连接平台（{exc}），使用本地缓存目录")
            return _load_cache(cache_file)
        raise CatalogError(f"无法获取教材目录索引：{exc}") from exc

    if not part_urls:
        raise CatalogError("目录索引返回为空，平台接口可能已变更")

    local_ver = stamp_file.read_text(encoding="utf-8").strip() if stamp_file.exists() else ""
    if not refresh and cache_file.exists() and local_ver == remote_ver:
        return _load_cache(cache_file)

    if not quiet:
        print(f"  正在更新教材目录（{len(part_urls)} 个分片，约 33 MB，仅首次或版本变更时需要）…")

    raw: list[dict] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for part in pool.map(lambda u: _fetch_part(u, quiet), part_urls):
            raw.extend(part)

    books = [b for b in (_parse(item) for item in raw) if b is not None]
    if not books:
        raise CatalogError("目录解析后为空，数据结构可能已变更")

    cache_file.write_text(
        json.dumps([b.to_dict() for b in books], ensure_ascii=False),
        encoding="utf-8",
    )
    stamp_file.write_text(remote_ver, encoding="utf-8")
    if not quiet:
        print(f"  目录就绪：{len(books)} 本可下载教材")
    return books


def _fetch_part(url: str, quiet: bool) -> list[dict]:
    for attempt in range(3):
        try:
            data = _get_json(url, timeout=90)
            return data if isinstance(data, list) else []
        except Exception as exc:
            if attempt == 2:
                if not quiet:
                    print(f"  [!] 分片下载失败，已跳过：{url}（{exc}）")
                return []
            time.sleep(2 ** attempt)
    return []


def _load_cache(path: Path) -> list[Book]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Book(**d) for d in data]


# ────────────────────────── 条目解析 ──────────────────────────

def _parse(item: dict) -> Book | None:
    """把一条原始目录记录转成 Book；无法定位图片坐标的直接丢弃。"""
    if not isinstance(item, dict):
        return None

    preview = ((item.get("custom_properties") or {}).get("preview")) or {}
    coord = None
    # preview 的键形如 Slide1/Slide2…，取页码最小的一条即可解析出坐标
    for key in sorted(preview, key=lambda s: int(re.sub(r"\D", "", s) or 0)):
        value = preview.get(key)
        if isinstance(value, str):
            m = COORD_RE.match(value)
            if m:
                coord = m
                break
    if coord is None:
        return None  # 约 15% 的条目没有 preview，交给 --url 回退方式处理

    tags = [t.get("tag_name", "") for t in (item.get("tag_list") or [])
            if isinstance(t, dict)]
    stage = next((t for t in tags if t in STAGES), "")
    volume = next((t for t in tags if t in VOLUMES), "")
    grade = next((t for t in tags if t.endswith("年级") and t not in STAGES), "")
    version = next((t for t in tags if t.endswith("版")), "")

    subject = ""
    for t in tags:
        if t in NOISE_TAGS or t in STAGES or t in VOLUMES:
            continue
        if t == grade or t == version:
            continue
        subject = t
        break

    providers = item.get("provider_list") or []
    publisher = ""
    if providers and isinstance(providers[0], dict):
        publisher = providers[0].get("name", "") or ""

    return Book(
        id=item.get("id", ""),
        title=(item.get("title") or "").strip(),
        stage=stage, grade=grade, volume=volume,
        subject=subject, version=version, publisher=publisher,
        content_id=coord.group("cid"), timestamp=coord.group("ts"),
    )


# ────────────────────────── 检索 ──────────────────────────

def search(books: list[Book], query: str = "", **filters) -> list[Book]:
    """按关键词 + 维度过滤检索。

    query 按空格分词，每个词都必须命中（标题或任一维度），即 AND 语义。
    这样 "苏教版 三年级 数学 上册" 能精确收敛，而不是返回一堆近似结果。
    """
    result = books

    for field, want in filters.items():
        if not want:
            continue
        result = [b for b in result if want in (getattr(b, field, "") or "")]

    for word in query.split():
        w = word.strip()
        if not w:
            continue
        result = [
            b for b in result
            if w in b.title or w in b.subject or w in b.version
            or w in b.grade or w in b.volume or w in b.stage or w in b.publisher
        ]

    return result


def distinct(books: list[Book], field: str) -> list[str]:
    """列出某个维度的所有取值，用于"没搜到时告诉用户有哪些可选"。"""
    seen = {getattr(b, field, "") for b in books}
    return sorted(v for v in seen if v)


def parse_coord_url(url: str) -> tuple[str, str]:
    """从任意一张封面/内页图片地址解析出 (contentId, timestamp)。

    回退方案：目录里没有 preview 的教材，用户可在平台上右键封面图
    复制地址，直接喂给本函数。
    """
    m = COORD_RE.search(url.strip())
    if not m:
        raise CatalogError(
            "无法从该地址解析教材坐标。\n"
            "  需要的是平台上教材封面图的地址，形如：\n"
            "  https://r3-ndr.ykt.cbern.com.cn/edu_product/esp/assets/"
            "<36位ID>.t/zh-CN/<数字时间戳>/transcode/image/1.jpg"
        )
    return m.group("cid"), m.group("ts")
