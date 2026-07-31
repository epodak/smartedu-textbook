/* 在国家中小学智慧教育平台上一键提取教材坐标
 * ────────────────────────────────────────────────
 * 仅在目录检索查不到某本教材时才需要用到（约 15% 的教材）。
 *
 * 用法：
 *   1. 打开 https://basic.smartedu.cn/tchMaterial
 *   2. 筛选到目标的 学段 / 学科 / 版本 / 年级
 *   3. F12 打开开发者工具 → Console
 *   4. 粘贴本文件全部内容，回车
 *   5. 复制输出的地址，交给下载脚本：
 *        python scripts/download.py --url "<地址>"
 *      多本时可保存成文件后用 --batch
 */

(() => {
  const RE = /\/assets\/[0-9a-f-]{36}\.t\/zh-CN\/\d+\/transcode\/image\/\d+\.jpg/i;

  // 教材卡片的封面图就带着 contentId 和 timestamp，直接从 DOM 取，
  // 不依赖平台的内部接口——接口会变，页面上看得见的东西相对稳定。
  const found = new Map();

  document.querySelectorAll('img').forEach((img) => {
    const src = img.currentSrc || img.src || '';
    if (!RE.test(src)) return;
    const clean = src.split('?')[0];
    const card = img.closest('li') || img.closest('[class*="item"]') || img.parentElement;
    const title = (card?.innerText || '').trim().split('\n')[0].slice(0, 60) || '(未知标题)';
    if (!found.has(clean)) found.set(clean, title);
  });

  if (!found.size) {
    console.warn(
      '没有找到教材封面图。请确认：\n' +
      '  1. 当前在「教材」页面且已筛选出教材列表\n' +
      '  2. 列表已经加载出来（图片可见）\n' +
      '  3. 如果页面很长，先向下滚动让图片加载完'
    );
    return;
  }

  console.log(`找到 ${found.size} 本教材：\n`);
  const lines = [];
  found.forEach((title, url) => {
    console.log(`${title}\n  ${url}\n`);
    lines.push(url);
  });

  const text = lines.join('\n');
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text)
      .then(() => console.log('✔ 全部地址已复制到剪贴板'))
      .catch(() => console.log('（自动复制失败，请手动从上方复制）'));
  }
  return text;
})();
