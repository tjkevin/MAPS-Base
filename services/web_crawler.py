"""
互联网批量采集服务（反馈#8）
================================
多模态互联网数据采集，统一被 Web 端 /api/crawl/* 调用：

视频采集
- 关键字搜索：哔哩哔哩（内置搜索 API，含播放量/弹幕/收藏等元数据）、
  YouTube（yt-dlp ytsearch）；
- 链接批量采集：任意 yt-dlp 支持站点的视频页 / 合集 / 频道 / UP 主主页 /
  播放列表（抖音、优酷、爱奇艺、腾讯视频、微博、小红书等 1000+ 站点），
  提取条目后多选下载。
- 采集范围（可多选）：字幕(subtitles)、弹幕(danmaku，B站)、封面(cover)、
  播放量等统计(stats)、简介(description)——元数据统一写入 .meta.json 侧车文件。

图片采集（BAGEL 图像理解的主要数据来源）
- 关键字搜索：必应图片搜索（无需 API Key）；
- 网页图片提取：给定任意网页 URL，提取页面内图片；
- 下载时校验 magic bytes（防伪造扩展名），单图 ≤ 20MB。

设计约束（云端无 GPU/ffmpeg）：
- yt-dlp 为纯 Python 依赖，懒加载：未安装时视频功能给出明确错误，图片功能不受影响；
- 无 ffmpeg 时格式选择器优先取预合并的单文件（best[ext=mp4]/best），不做合并；
- 所有可选特性（字幕/弹幕/封面）失败自动降级，不阻塞主流程；
- 所有媒体下载到 UPLOAD_FOLDER（与人工上传一致），可直接经 /uploads/<name> 预览。
"""
import glob
import html
import json
import os
import random
import re
import time
import uuid
from datetime import datetime
from urllib.parse import quote_plus, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
_DEFAULT_HEADERS = {'User-Agent': _UA, 'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'}

# 反馈#15：robots.txt 遵守/忽略——默认遵守，用户勾选「忽略 robots 协议」后放行
_ROBOTS_USER_AGENT = 'MAPS-DataCollector'
_robots_cache = {}  # netloc -> RobotFileParser 或 None（None=无 robots 文件/不可达，全允许）


def _robots_allowed(url, ignore_robots=False):
    """检查 url 是否允许被本系统抓取。robots.txt 缺失/不可达时 fail-open 放行；
    解析异常同样放行（不因 robots 检查本身阻塞采集），仅显式 Disallow 时返回 False。"""
    if ignore_robots or not url:
        return True
    try:
        parts = urlparse(url)
        if parts.scheme not in ('http', 'https') or not parts.netloc:
            return True
        netloc = parts.netloc.lower()
        if netloc not in _robots_cache:
            rp = RobotFileParser()
            robots_url = f'{parts.scheme}://{parts.netloc}/robots.txt'
            try:
                resp = requests.get(robots_url, headers=_DEFAULT_HEADERS, timeout=8)
                if resp.status_code >= 400:
                    _robots_cache[netloc] = None  # 无 robots 文件 → 全部允许
                else:
                    rp.parse(resp.text.splitlines())
                    _robots_cache[netloc] = rp
            except requests.RequestException:
                _robots_cache[netloc] = None
        rp = _robots_cache[netloc]
        if rp is None:
            return True
        return rp.can_fetch(_ROBOTS_USER_AGENT, url)
    except Exception:
        return True


def assert_robots_allowed(url, ignore_robots=False):
    """反馈#15：抓取前置校验；被 robots.txt 禁止时给出可操作的中文提示。"""
    if not _robots_allowed(url, ignore_robots=ignore_robots):
        raise CrawlUnavailable(
            '目标站点 robots.txt 禁止抓取该路径；如确认本次采集符合法律法规与站点条款，'
            '请勾选「采集范围」中的「忽略 robots 协议」后重试。')


# 单图大小上限 20MB
_IMAGE_MAX_BYTES = 20 * 1024 * 1024

# 支持的站点注册表（keyword_search=是否支持站内关键字搜索；False 时用链接批量采集）
VIDEO_SITES = [
    {'id': 'bilibili', 'name': '哔哩哔哩', 'keyword_search': True},
    {'id': 'youtube', 'name': 'YouTube', 'keyword_search': True},
    {'id': 'douyin', 'name': '抖音', 'keyword_search': False},
    {'id': 'youku', 'name': '优酷', 'keyword_search': False},
    {'id': 'iqiyi', 'name': '爱奇艺', 'keyword_search': False},
    {'id': 'qq', 'name': '腾讯视频', 'keyword_search': False},
    {'id': 'weibo', 'name': '微博', 'keyword_search': False},
    {'id': 'xiaohongshu', 'name': '小红书', 'keyword_search': False},
    {'id': 'generic', 'name': '通用网址（yt-dlp 支持的其他站点）', 'keyword_search': False},
]

_IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.gif')
# yt-dlp 下载后非媒体的附属文件扩展名（字幕/封面/弹幕/元数据）
_SIDECAR_EXTS = ('.json', '.srt', '.vtt', '.ass', '.ssa', '.xml', '.jpg', '.jpeg', '.png', '.webp')


class CrawlUnavailable(RuntimeError):
    """采集能力不可用（如视频采集依赖 yt-dlp 未安装）。"""


# ---------------- 通用工具 ----------------

def _safe_name(title, fallback='media', maxlen=48):
    """把标题清洗为安全文件名片段（去路径分隔符/控制字符，截断长度）。"""
    name = re.sub(r'[\\/*?:"<>|\r\n\t]+', '_', str(title or ''))
    name = re.sub(r'\s+', ' ', name).strip(' ._')[:maxlen].strip()
    return name or fallback


def _now_str():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')


def _lazy_ytdlp():
    """懒加载 yt-dlp：视频功能专用，缺失时抛 CrawlUnavailable（图片功能不受影响）。"""
    try:
        from yt_dlp import YoutubeDL
        return YoutubeDL
    except ImportError as e:
        raise CrawlUnavailable(
            '视频采集依赖 yt-dlp（云端镜像未安装或版本过旧），请联系管理员升级镜像；'
            '图片采集不受影响。'
        ) from e


def _base_opts():
    """yt-dlp 白名单参数（仅使用官方稳定选项；无 ffmpeg 环境的降级配置）。"""
    return {
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'ignoreerrors': False,
        'retries': 2,
        'fragment_retries': 2,
        'sleep_interval': 1,
        'max_sleep_interval': 3,
        'http_headers': {'User-Agent': _UA, 'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'},
    }


# ---------------- 视频：关键字搜索 ----------------

def _bili_session():
    """
    B站反爬（412 Precondition Failed）规避：先访问首页取得 buvid3/b_nut 等风控
    Cookie，再携带 Cookie + Referer 调用 API。云服务器 IP 直连 API 不带 Cookie
    时高频触发 412（表现为搜索/下载全部失败）。
    """
    sess = requests.Session()
    sess.headers.update(_DEFAULT_HEADERS)
    try:
        sess.get('https://www.bilibili.com/', timeout=10)
    except requests.RequestException:
        pass
    return sess


def _bili_search_duration_param(duration_limit):
    """B站搜索 API duration 参数：1=10分钟内, 2=10-30分钟, 3=30-60分钟, 0=全部。
    按页面时长上限选择最接近的区间，减少翻页与风控触发。"""
    if duration_limit <= 10:
        return 1
    if duration_limit <= 30:
        return 2
    if duration_limit <= 60:
        return 3
    return 0


def _bili_parse_duration(s):
    """B站搜索结果 duration 形如 '2:25'（分:秒）或 '1:02:33'（时:分:秒），返回秒数。"""
    try:
        parts = [int(x) for x in str(s).split(':')]
    except (TypeError, ValueError):
        return 0
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0


def _bili_strip_highlight(s):
    """去除搜索结果标题中的 <em class="keyword">…</em> 高亮标签。"""
    return re.sub(r'</?em[^>]*>', '', str(s or ''))


def _bilibili_search(keywords, count, duration_limit):
    """B站视频关键字搜索（反馈#12：原生 search/type API 直连，替代旧脚本）。

    旧 scripts/bilibili_crawler.py 按 data.result.video 字典结构解析，而
    search/type 端点返回的 data.result 是视频列表，结构不匹配导致恒为 0 条；
    且云服务器 IP 高频触发 412。此处先访问首页取 buvid 风控 Cookie，带
    Referer=https://search.bilibili.com/ 直连官方搜索接口（实测单次返回
    20 条），服务端按 duration 区间过滤、本地再按秒数复核。
    """
    items = []
    seen_bv = set()
    max_pages = 3
    sess = _bili_session()
    headers = dict(_DEFAULT_HEADERS)
    headers['Referer'] = 'https://search.bilibili.com/'
    duration_param = _bili_search_duration_param(duration_limit)
    max_seconds = duration_limit * 60
    errors = []
    for kw in keywords:
        if len(items) >= count:
            break
        for page in range(1, max_pages + 1):
            if len(items) >= count:
                break
            params = {
                'keyword': kw,
                'search_type': 'video',
                'page': page,
                'order': 'click',
                'duration': duration_param,
                'tids': 0,
            }
            try:
                time.sleep(random.uniform(0.8, 1.5))
                resp = sess.get(
                    'https://api.bilibili.com/x/web-interface/search/type',
                    params=params, headers=headers, timeout=15)
                if resp.status_code == 412:
                    errors.append(f'关键词「{kw}」触发 412 风控')
                    sess = _bili_session()  # 重建会话刷新 buvid Cookie 后重试
                    continue
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                errors.append(f'关键词「{kw}」{type(e).__name__}: {str(e)[:80]}')
                continue
            if data.get('code') != 0:
                errors.append(f'关键词「{kw}」API code={data.get("code")} {str(data.get("message", ""))[:60]}')
                break  # code!=0（风控/参数错误）时继续翻页无意义
            rows = (data.get('data') or {}).get('result')
            if not isinstance(rows, list) or not rows:
                break  # 该关键词已无更多结果
            for v in rows:
                bvid = v.get('bvid') or ''
                if not bvid or bvid in seen_bv:
                    continue
                dur = _bili_parse_duration(v.get('duration'))
                if dur and dur > max_seconds:
                    continue
                seen_bv.add(bvid)
                items.append({
                    'media_type': 'video',
                    'site': 'bilibili',
                    'title': _bili_strip_highlight(v.get('title')) or bvid,
                    'author': v.get('author') or '',
                    'page_url': f'https://www.bilibili.com/video/{bvid}',
                    'duration': dur,
                    'published': datetime.utcfromtimestamp(v.get('pubdate') or 0).strftime('%Y-%m-%d') if v.get('pubdate') else '',
                    'play_count': int(v.get('play') or 0),
                    'danmaku_count': int(v.get('danmaku') or v.get('video_review') or 0),
                    'favorite_count': int(v.get('favorites') or 0),
                    'cover': v.get('pic') or v.get('cover') or '',
                    'description': str(v.get('description') or v.get('desc') or '')[:500],
                    'keywords': kw,
                })
                if len(items) >= count:
                    break
    if not items and errors:
        raise CrawlUnavailable(
            'B站搜索暂时被风控限制（' + '；'.join(errors[:3]) + '）。请稍后重试，'
            '或改用「链接批量采集」直接粘贴哔哩哔哩视频页地址（下载不受影响）。')
    return items


def _youtube_search(keywords, count):
    """YouTube 关键字搜索（yt-dlp ytsearch，flat 提取速度快）。"""
    YoutubeDL = _lazy_ytdlp()
    opts = _base_opts()
    opts.update({'extract_flat': True, 'skip_download': True})
    items = []
    with YoutubeDL(opts) as ydl:
        for kw in keywords:
            if len(items) >= count:
                break
            try:
                info = ydl.extract_info(f'ytsearch{count}:{kw}', download=False)
            except Exception:
                continue
            for e in (info or {}).get('entries') or []:
                if not e:
                    continue
                vid = e.get('id') or ''
                url = e.get('url') or e.get('webpage_url') or ''
                if not url.startswith('http') and vid:
                    url = f'https://www.youtube.com/watch?v={vid}'
                items.append({
                    'media_type': 'video',
                    'site': 'youtube',
                    'title': e.get('title') or vid or 'untitled',
                    'author': e.get('uploader') or e.get('channel') or '',
                    'page_url': url,
                    'duration': int(e.get('duration') or 0),
                    'published': str(e.get('upload_date') or ''),
                    'play_count': int(e.get('view_count') or 0),
                    'danmaku_count': 0,
                    'favorite_count': 0,
                    'cover': e.get('thumbnails')[-1]['url'] if e.get('thumbnails') else (e.get('thumbnail') or ''),
                    'description': '',
                    'keywords': kw,
                })
                if len(items) >= count:
                    break
            time.sleep(1)
    return items


def _norm_yt_entry(e, site):
    """yt-dlp info/flat entry → 归一化视频条目。"""
    e = e or {}
    url = e.get('webpage_url') or e.get('url') or e.get('original_url') or ''
    vid = e.get('id') or ''
    if not url.startswith('http'):
        if site == 'youtube' and vid:
            url = f'https://www.youtube.com/watch?v={vid}'
        else:
            url = ''
    thumbs = e.get('thumbnails') or []
    return {
        'media_type': 'video',
        'site': site if site != 'generic' else (e.get('extractor_key') or 'generic').lower(),
        'title': e.get('title') or vid or 'untitled',
        'author': e.get('uploader') or e.get('channel') or e.get('uploader_id') or '',
        'page_url': url,
        'duration': int(e.get('duration') or 0),
        'published': str(e.get('upload_date') or ''),
        'play_count': int(e.get('view_count') or 0),
        'danmaku_count': int(e.get('comment_count') or 0) if site == 'bilibili' else 0,
        'favorite_count': int(e.get('like_count') or 0),
        'cover': (thumbs[-1].get('url') if thumbs else '') or e.get('thumbnail') or '',
        'description': str(e.get('description') or '')[:500],
    }


def _ensure_site_reachable(site):
    """反馈#12：云端位于中国大陆，YouTube 等站点网络不可达时快速失败并给出明确提示，
    避免 yt-dlp 长超时挂起下载线程。"""
    probe = {
        'youtube': 'https://www.youtube.com/',
    }.get(site)
    if not probe:
        return
    try:
        requests.get(probe, headers=_DEFAULT_HEADERS, timeout=6)
    except requests.RequestException:
        if site == 'youtube':
            raise CrawlUnavailable(
                'YouTube 与当前云服务器网络不通（云服务器位于中国大陆，无法直接访问 YouTube）。'
                '请改用「哔哩哔哩」关键字搜索，或采集其他可直达站点；'
                '如需 YouTube 素材，请在可访问的网络环境下载后通过「本地上传」入库。')
        raise CrawlUnavailable(f'站点 {site} 当前网络不可达，请稍后重试或改用其他站点')


def search_videos(site, keywords, count=10, duration_limit=30, ignore_robots=False):
    """视频关键字搜索。site: bilibili / youtube；其余站点请用 extract_videos_from_url。
    平台官方 API 通道（B站/YouTube）不经过 robots 拦截；ignore_robots 透传给链接提取通道。"""
    site = (site or 'bilibili').strip().lower()
    if site == 'bilibili':
        return _bilibili_search(keywords, count, duration_limit)
    if site == 'youtube':
        _ensure_site_reachable('youtube')
        return _youtube_search(keywords, count)
    raise CrawlUnavailable(f'站点 {site} 暂不支持关键字搜索，请改用「链接批量采集」（粘贴视频页/合集/频道地址）')


def extract_videos_from_url(site, page_url, count=30, ignore_robots=False):
    """链接批量采集：yt-dlp 解析视频页/合集/频道/播放列表，返回候选视频条目。"""
    YoutubeDL = _lazy_ytdlp()
    if not page_url or not page_url.startswith('http'):
        raise CrawlUnavailable('请输入以 http(s):// 开头的有效页面地址')
    # 反馈#15：通用网页抓取遵守 robots.txt（B站原生下载通道不走此函数，不受影响）
    assert_robots_allowed(page_url, ignore_robots=ignore_robots)
    if (site or '').lower() == 'youtube' or 'youtube.com' in page_url or 'youtu.be' in page_url:
        _ensure_site_reachable('youtube')
    opts = _base_opts()
    opts.update({'extract_flat': True, 'skip_download': True})
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(page_url, download=False)
    if not info:
        return []
    entries = info.get('entries') and list(info['entries']) or [info]
    items = []
    for e in entries:
        if not e:
            continue
        item = _norm_yt_entry(e, (site or 'generic').lower())
        if not item['page_url']:
            continue
        items.append(item)
        if len(items) >= count:
            break
    return items


# ---------------- 图片：关键字搜索 / 网页提取 ----------------

def search_images(keywords, count=20, ignore_robots=False):
    """必应图片关键字搜索（解析搜索页 HTML 中的 iusc 条目，无需 API Key）。"""
    items = []
    seen = set()
    for kw in keywords:
        if len(items) >= count:
            break
        try:
            url = f'https://www.bing.com/images/search?q={quote_plus(kw)}&form=HDRSC2&first=1'
            # 反馈#15：遵守 robots.txt（勾选忽略则放行）
            assert_robots_allowed(url, ignore_robots=ignore_robots)
            resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=15)
            resp.raise_for_status()
            page = resp.text
        except requests.RequestException:
            continue
        for raw in re.findall(r'<a[^>]+class="iusc"[^>]*\sm="([^"]+)"', page):
            try:
                meta = json.loads(html.unescape(raw))
            except (json.JSONDecodeError, TypeError):
                continue
            murl = meta.get('murl') or ''
            if not murl.startswith('http') or murl in seen:
                continue
            seen.add(murl)
            items.append({
                'media_type': 'image',
                'site': 'bing',
                'title': (meta.get('t') or kw)[:120],
                'image_url': murl,
                'source_page': meta.get('purl') or '',
                'width': int(meta.get('imgw') or 0),
                'height': int(meta.get('imgh') or 0),
                'keywords': kw,
            })
            if len(items) >= count:
                break
        time.sleep(1)
    return items


def extract_images_from_page(page_url, count=30, ignore_robots=False):
    """从任意网页提取图片地址（<img src>/data-src/data-original 等懒加载属性）。"""
    if not page_url or not page_url.startswith('http'):
        raise CrawlUnavailable('请输入以 http(s):// 开头的有效页面地址')
    # 反馈#15：遵守 robots.txt（勾选忽略则放行）
    assert_robots_allowed(page_url, ignore_robots=ignore_robots)
    try:
        resp = requests.get(page_url, headers=_DEFAULT_HEADERS, timeout=20)
        resp.raise_for_status()
        page = resp.text
    except requests.RequestException as e:
        raise CrawlUnavailable(f'网页抓取失败：{type(e).__name__}')
    candidates = re.findall(
        r'<img[^>]+(?:src|data-src|data-original|data-url|data-lazy-src)=["\']([^"\']+)["\']',
        page, flags=re.IGNORECASE)
    items, seen = [], set()
    for u in candidates:
        full = urljoin(page_url, html.unescape(u).strip())
        if not full.startswith('http') or full in seen:
            continue
        low = full.split('?')[0].lower()
        # 只收图片扩展名或常见图床；无扩展名的 CDN 地址在下载时再用 magic bytes 判定
        if not (low.endswith(_IMAGE_EXTS) or 'image' in full.lower() or 'img' in urljoin(full, '').netloc):
            continue
        seen.add(full)
        items.append({
            'media_type': 'image',
            'site': 'page',
            'title': os.path.basename(low)[:60] or 'web_image',
            'image_url': full,
            'source_page': page_url,
            'width': 0,
            'height': 0,
        })
        if len(items) >= count:
            break
    return items


# ---------------- 下载 ----------------

def _detect_image_ext(data):
    """按 magic bytes 判定真实图片类型（防伪造扩展名）。"""
    if data[:3] == b'\xff\xd8\xff':
        return 'jpg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    return None


def _write_meta_sidecar(media_path, item, options, remark, extra=None):
    """写 .meta.json 侧车文件（与媒体同目录、同名前缀），返回其路径。"""
    meta = dict(item)
    meta.update({
        'options': options or {},
        'remark': remark or '',
        'downloaded_at': _now_str(),
    })
    if extra:
        meta.update(extra)
    meta_path = os.path.splitext(media_path)[0] + '.meta.json'
    try:
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except OSError:
        return None
    return meta_path


def _download_bilibili_danmaku(page_url, base_path):
    """可选特性：下载 B站弹幕 XML（<base>.danmaku.xml）。失败降级返回 None。"""
    try:
        m = re.search(r'(BV[0-9A-Za-z]+)', page_url or '')
        if not m:
            return None
        headers = dict(_DEFAULT_HEADERS)
        headers['Referer'] = 'https://www.bilibili.com/'
        view = requests.get(
            f'https://api.bilibili.com/x/web-interface/view?bvid={m.group(1)}',
            headers=headers, timeout=10)
        cid = (view.json().get('data') or {}).get('cid')
        if not cid:
            return None
        xml = requests.get(f'https://comment.bilibili.com/{cid}.xml', headers=headers, timeout=15).content
        danmaku_path = base_path + '.danmaku.xml'
        with open(danmaku_path, 'wb') as f:
            f.write(xml)
        return danmaku_path
    except Exception:
        return None


def _download_bilibili_native(item, options, remark, dest_dir, base):
    """
    B站原生下载（反馈#12：修复云服务器 412 风控导致 yt-dlp 下载全部失败）。
    走官方 playurl API（免登录 360P 单文件 MP4，带 buvid Cookie + Referer），
    不依赖 ffmpeg/yt-dlp。长视频多段 durl：FLV 可字节拼接，MP4 多段则分段保存。
    返回 {ok, path, filename, duration, meta_path, meta, error}。
    """
    page_url = item.get('page_url') or ''
    m = re.search(r'(BV[0-9A-Za-z]+)', page_url)
    if not m:
        return {'ok': False, 'error': '无法从链接解析 BV 号'}
    bvid = m.group(1)
    sess = _bili_session()
    headers = dict(_DEFAULT_HEADERS)
    headers['Referer'] = 'https://www.bilibili.com/'
    try:
        view = sess.get(
            'https://api.bilibili.com/x/web-interface/view',
            params={'bvid': bvid}, headers=headers, timeout=15)
        vj = view.json()
        if vj.get('code') != 0 or not vj.get('data'):
            return {'ok': False, 'error': f"B站视频信息获取失败：{vj.get('message') or '未知'}"}
        vdata = vj['data']
        cid = vdata.get('cid')
        if not cid:
            return {'ok': False, 'error': 'B站视频缺少 cid（可能是多 P 合集页，请用单视频链接）'}
        pu = sess.get(
            'https://api.bilibili.com/x/player/playurl',
            params={'bvid': bvid, 'cid': cid, 'qn': 16, 'fnval': 0, 'fourk': 0},
            headers=headers, timeout=15)
        pj = pu.json()
        if pj.get('code') != 0 or not pj.get('data'):
            return {'ok': False, 'error': f"B站下载地址获取失败：{pj.get('message') or '未知'}"}
        durl = (pj['data'].get('durl') or [])
        if not durl:
            return {'ok': False, 'error': 'B站未返回可下载地址（视频可能为会员/地区受限）'}
        # 逐段下载（短视频通常 1 段）
        seg_paths = []
        for idx, seg in enumerate(durl):
            seg_url = seg.get('url') or ''
            if not seg_url:
                continue
            resp = sess.get(seg_url, headers=headers, timeout=120, stream=True)
            resp.raise_for_status()
            data = resp.content
            if not data:
                continue
            # 按 magic bytes 定真实扩展名
            if data[:3] == b'FLV':
                ext = 'flv'
            elif data[4:8] == b'ftyp':
                ext = 'mp4'
            else:
                ext = 'mp4'
            suffix = '' if len(durl) == 1 else f'_part{idx + 1}'
            seg_path = os.path.join(dest_dir, f'{base}{suffix}.{ext}')
            with open(seg_path, 'wb') as f:
                f.write(data)
            seg_paths.append((seg_path, data[:3] == b'FLV'))
        if not seg_paths:
            return {'ok': False, 'error': 'B站视频下载内容为空'}
        # FLV 多段可直接字节拼接为完整文件；MP4 多段保留分段（罕见，360P 短视频基本单段）
        if len(seg_paths) == 1:
            path = seg_paths[0][0]
        elif all(is_flv for _, is_flv in seg_paths):
            path = os.path.join(dest_dir, f'{base}.flv')
            with open(path, 'wb') as out:
                for p, _ in seg_paths:
                    with open(p, 'rb') as inp:
                        out.write(inp.read())
            for p, _ in seg_paths:
                try:
                    os.remove(p)
                except OSError:
                    pass
        else:
            path = seg_paths[0][0]  # 多段 MP4：首段入库，其余段同目录保留（meta 记录分段数）
        enriched = dict(item)
        enriched.update({
            'title': vdata.get('title') or item.get('title'),
            'author': vdata.get('owner', {}).get('name') or item.get('author'),
            'duration': int(vdata.get('duration') or item.get('duration') or 0),
            'published': str(datetime.utcfromtimestamp(vdata.get('pubdate', 0)).date()) if vdata.get('pubdate') else '',
            'play_count': int((vdata.get('stat') or {}).get('view') or item.get('play_count') or 0),
            'danmaku_count': int((vdata.get('stat') or {}).get('danmaku') or item.get('danmaku_count') or 0),
            'favorite_count': int((vdata.get('stat') or {}).get('favorite') or item.get('favorite_count') or 0),
            'cover': vdata.get('pic') or item.get('cover') or '',
            'description': str(vdata.get('desc') or item.get('description') or '')[:1000],
            'extractor': 'bilibili-native',
            'bvid': bvid,
            'segments': len(durl),
        })
        meta_path = _write_meta_sidecar(path, enriched, options, remark)
        if options.get('danmaku'):
            _download_bilibili_danmaku(page_url, os.path.splitext(path)[0])
        return {'ok': True, 'path': path, 'filename': os.path.basename(path),
                'duration': enriched['duration'], 'meta_path': meta_path, 'meta': enriched}
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {str(e)[:200]}'}


def download_video_item(item, options, remark, dest_dir):
    """下载单个视频条目。返回 {ok, path, filename, duration, meta_path, error}。
    反馈#12：B站优先走原生 playurl API（云服务器 412 风控下 yt-dlp 必失败），
    原生失败再回退 yt-dlp；YouTube 在中国大陆云服务器网络不可达时给出明确提示。"""
    page_url = item.get('page_url') or ''
    if not page_url.startswith('http'):
        return {'ok': False, 'error': '缺少有效的视频页面地址'}
    base = f"{_safe_name(item.get('title'), 'video')}_{uuid.uuid4().hex[:8]}"

    # B站：原生 API 优先（免 ffmpeg、抗 412），失败再回退 yt-dlp
    bili_cookie_file = None
    if (item.get('site') or '').lower() == 'bilibili':
        r = _download_bilibili_native(item, options, remark, dest_dir, base)
        if r.get('ok'):
            return r
        native_err = r.get('error') or '原生下载失败'
        try:
            YoutubeDL = _lazy_ytdlp()
        except CrawlUnavailable:
            return {'ok': False, 'error': f'B站下载失败（{native_err}）'}
        # 回退 yt-dlp 时同样注入风控 Cookie（写临时 Netscape cookie 文件）
        try:
            import http.cookiejar
            sess = _bili_session()
            bili_cookie_file = os.path.join(dest_dir, f'.bili_cookies_{uuid.uuid4().hex[:8]}.txt')
            jar = http.cookiejar.MozillaCookieJar(bili_cookie_file)
            for c in sess.cookies:
                jar.set_cookie(c)
            jar.save(ignore_discard=True, ignore_expires=True)
        except Exception:
            bili_cookie_file = None
    else:
        YoutubeDL = _lazy_ytdlp()

    opts = _base_opts()
    if bili_cookie_file:
        opts['cookiefile'] = bili_cookie_file
    # 无 ffmpeg：优先预合并 mp4 单文件，避免触发合并失败
    opts['format'] = 'best[ext=mp4][filesize<800M]/best[filesize<800M]/best'
    opts['outtmpl'] = os.path.join(dest_dir, base + '.%(ext)s')
    opts['noplaylist'] = True
    if options.get('subtitles'):
        # 可选特性：字幕（YouTube 等站点有效；B站多数视频无 CC 字幕，失败静默降级）
        opts.update({
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['zh-Hans', 'zh-CN', 'zh', 'en'],
        })
    if options.get('cover'):
        opts['writethumbnail'] = True
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(page_url, download=True)
            path = ydl.prepare_filename(info)
        if not os.path.exists(path):
            # 实际扩展名可能与预期不同（无合并时），glob 回找媒体文件
            cands = [p for p in glob.glob(os.path.join(dest_dir, base + '.*'))
                     if not p.lower().endswith(_SIDECAR_EXTS)]
            path = cands[0] if cands else None
        if not path or not os.path.exists(path):
            return {'ok': False, 'error': '下载完成但未找到媒体文件'}
        # 用 yt-dlp 完整 info 丰富元数据（统计/简介/封面等）
        enriched = dict(item)
        if isinstance(info, dict):
            enriched.update({
                'title': info.get('title') or item.get('title'),
                'author': info.get('uploader') or info.get('channel') or item.get('author'),
                'duration': int(info.get('duration') or item.get('duration') or 0),
                'published': str(info.get('upload_date') or item.get('published') or ''),
                'play_count': int(info.get('view_count') or item.get('play_count') or 0),
                'favorite_count': int(info.get('like_count') or item.get('favorite_count') or 0),
                'cover': info.get('thumbnail') or item.get('cover') or '',
                'description': str(info.get('description') or item.get('description') or '')[:1000],
                'extractor': info.get('extractor_key') or '',
            })
        duration = int(enriched.get('duration') or 0)
        base_no_ext = os.path.splitext(path)[0]
        if options.get('danmaku') and item.get('site') == 'bilibili':
            _download_bilibili_danmaku(page_url, base_no_ext)
        meta_path = _write_meta_sidecar(path, enriched, options, remark)
        return {'ok': True, 'path': path, 'filename': os.path.basename(path),
                'duration': duration, 'meta_path': meta_path, 'meta': enriched}
    except CrawlUnavailable:
        raise
    except Exception as e:
        msg = f'{type(e).__name__}: {str(e)[:200]}'
        site = (item.get('site') or '').lower()
        if site == 'youtube' and ('Connection' in msg or 'Timeout' in msg or 'Max retries' in msg or 'timed out' in msg):
            msg = ('YouTube 与当前云服务器网络不通（云服务器位于中国大陆，无法直接访问 YouTube）。'
                   '请改用「哔哩哔哩」关键字搜索，或采集其他可直达站点；如需 YouTube 素材，请在能访问的网络环境下载后本地上传。')
        elif site == 'bilibili':
            msg = f'B站下载失败（原生通道：{native_err}；yt-dlp 回退：{msg}）'
        return {'ok': False, 'error': msg}
    finally:
        if bili_cookie_file:
            try:
                os.remove(bili_cookie_file)
            except OSError:
                pass


def download_image_item(item, options, remark, dest_dir):
    """下载单个图片条目（magic bytes 校验，≤20MB）。"""
    img_url = item.get('image_url') or item.get('page_url') or ''
    if not img_url.startswith('http'):
        return {'ok': False, 'error': '缺少有效的图片地址'}
    # 反馈#15：下载图片同样遵守 robots.txt（采集范围勾选「忽略 robots 协议」时放行）
    if not _robots_allowed(img_url, ignore_robots=bool((options or {}).get('ignore_robots'))):
        return {'ok': False, 'error': '目标站点 robots.txt 禁止抓取该图片（可勾选「忽略 robots 协议」后重试）'}
    headers = dict(_DEFAULT_HEADERS)
    if item.get('source_page'):
        headers['Referer'] = item['source_page']
    try:
        resp = requests.get(img_url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.content
    except requests.RequestException as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {str(e)[:150]}'}
    if not data or len(data) > _IMAGE_MAX_BYTES:
        return {'ok': False, 'error': '图片为空或超过 20MB 上限'}
    ext = _detect_image_ext(data)
    if not ext:
        return {'ok': False, 'error': '非有效图片文件（magic bytes 校验失败）'}
    base = f"{_safe_name(item.get('title'), 'image', 40)}_{uuid.uuid4().hex[:8]}"
    fname = f'{base}.{ext}'
    path = os.path.join(dest_dir, fname)
    try:
        with open(path, 'wb') as f:
            f.write(data)
    except OSError as e:
        return {'ok': False, 'error': f'写文件失败: {e}'}
    meta = dict(item)
    meta['file_size'] = len(data)
    meta_path = _write_meta_sidecar(path, meta, options, remark)
    return {'ok': True, 'path': path, 'filename': fname,
            'duration': None, 'meta_path': meta_path, 'meta': meta}


def download_all(items, media_type, options, remark, dest_dir):
    """
    批量下载（调用方已做限流与并发锁）。
    返回 {'results': [...], 'success': n, 'failed': [{title,error}...]}。
    单条失败不中断整批；图片间 0.5~1.5s 礼貌延时。
    """
    os.makedirs(dest_dir, exist_ok=True)
    results = []
    for it in items:
        if media_type == 'image':
            r = download_image_item(it, options, remark, dest_dir)
            time.sleep(random.uniform(0.5, 1.5))
        else:
            r = download_video_item(it, options, remark, dest_dir)
        results.append(r)
    return {
        'results': results,
        'success': sum(1 for r in results if r.get('ok')),
        'failed': [{'title': (it.get('title') or '')[:80], 'error': r.get('error')}
                   for it, r in zip(items, results) if not r.get('ok')],
    }
