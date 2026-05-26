#!/usr/bin/env python3
"""
新闻/文章推送脚本：Markdown → 微信兼容 HTML → 草稿箱

用法:
  # 新闻模式（默认）
  python3 push_daily.py <input.md> [--title TITLE] [--cover COVER_IMAGE] [--digest DIGEST] [--media-id MEDIA_ID]

  # 文章模式
  python3 push_daily.py --article <input.md> [--title TITLE] [--cover COVER_IMAGE] [--digest DIGEST] [--media-id MEDIA_ID]

  # 更新已有草稿（追加 --update，其余参数同上）
  python3 push_daily.py --update <input.md> [--title TITLE] [--cover COVER_IMAGE] [--digest DIGEST]
  python3 push_daily.py --update MEDIA_ID <input.md> [--title TITLE] [--cover COVER_IMAGE] [--digest DIGEST]
  python3 push_daily.py --article --update <input.md> [--title TITLE] [--cover COVER_IMAGE] [--digest DIGEST]

Markdown frontmatter 支持（在文件顶部加 YAML 区段，可省去 --title 和 --digest 参数）：
  ---
  title: 文章标题
  digest: 手动摘要（80字以内）
  ---
  # 文章标题（仍是必填）

封面图处理:
- media_id 获取优先级：命令行 --media-id > config.yaml wechat.media_id > 上传封面图
- 新闻模式：首次上传封面图，media_id 保存到 cover_media_id.txt，后续复用
- 文章模式：每次都重新上传封面图（每篇文章封面不同），不复用
- 默认封面图: ~/.md_push_wechat/封面图.png
- 文章模式默认封面图: ~/.md_push_wechat/AI文章封面.png（如存在）
- 1:1 裁剪坐标固定为 "1008,0,1872,864"

完整工作流:
  1. Markdown → 微信兼容 HTML (md2wechat_html.py，文章模式时加 --article 参数)
  2. 上传封面图（首次）
  3. 上传正文图片素材并替换 HTML 引用（支持缓存复用）
  4. 推送草稿箱 / 更新已有草稿（--update）

"""

import json
import os
import re
import ssl
import sys
import subprocess
import urllib.parse
import urllib.request

# ─── 配置 ───────────────────────────────────────────────


CONFIG_PATH = os.path.join(os.path.expanduser("~/.md_push_wechat"), "config.yaml")
API_BASE = "https://api.weixin.qq.com"
COVER_MEDIA_ID_FILE = os.path.join(os.path.expanduser("~/.md_push_wechat"), "cover_media_id.txt")
ARTICLE_COVER_MEDIA_ID_FILE = os.path.join(os.path.expanduser("~/.md_push_wechat"), "article_cover_media_id.txt")
DEFAULT_COVER = os.path.join(os.path.expanduser("~/.md_push_wechat"), "封面图.png")
DEFAULT_ARTICLE_COVER = os.path.join(os.path.expanduser("~/.md_push_wechat"), "AI文章封面.png")
MD2WECHAT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "md2wechat_html.py")
IMAGE_MAP_FILE = os.path.join(os.path.expanduser("~/.md_push_wechat"), "image_asset_map.json")
DRAFT_MEDIA_ID_FILE = os.path.join(os.path.expanduser("~/.md_push_wechat"), "draft_media_id.txt")

SUPPORTED_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")

# ─── 工具函数 ───────────────────────────────────────────

def load_credentials():
    """从 config.yaml 读取 wechat.appid 和 wechat.secret"""
    if not os.path.exists(CONFIG_PATH):
        print(f"ERROR: 配置文件不存在: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    appid_match = re.search(r"appid:\s+(\S+)", content)
    secret_match = re.search(r"secret:\s+(\S+)", content)
    if not appid_match or not secret_match:
        print("ERROR: 无法从配置文件中读取 appid/secret")
        sys.exit(1)
    return appid_match.group(1).strip(), secret_match.group(1).strip()


def load_config_media_id():
    """从 config.yaml 读取 wechat.media_id（封面图的永久素材 ID）"""
    if not os.path.exists(CONFIG_PATH):
        return None
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    match = re.search(r"media_id:\s*(\S+)", content)
    return match.group(1).strip() if match else None


def get_access_token(appid, secret):
    """获取微信 access_token"""
    ctx = ssl.create_default_context()
    url = f"{API_BASE}/cgi-bin/token?grant_type=client_credential&appid={appid}&secret={secret}"
    with urllib.request.urlopen(url, context=ctx) as resp:
        data = json.loads(resp.read())
    if "access_token" not in data:
        print(f"ERROR: 获取 access_token 失败: {json.dumps(data, ensure_ascii=False)}")
        sys.exit(1)
    return data["access_token"]


def upload_image(access_token, image_path):
    """上传图片到永久素材库，返回 media_id"""
    ctx = ssl.create_default_context()
    filename = os.path.basename(image_path)
    with open(image_path, "rb") as f:
        file_data = f.read()
    
    boundary = "----WechatDraftBoundary7MA4YWxkTrZu0gW"
    body_parts = []
    body_parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="media"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
    )
    body_parts.append(file_data)
    body_parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(body_parts)
    
    url = f"{API_BASE}/cgi-bin/material/add_material?access_token={access_token}&type=image"
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    with urllib.request.urlopen(req, context=ctx) as resp:
        result = json.loads(resp.read())
    
    if "media_id" not in result:
        print(f"ERROR: 封面图上传失败: {json.dumps(result, ensure_ascii=False)}")
        sys.exit(1)
    
    return result["media_id"]


def upload_content_image(access_token, image_path):
    """上传正文图片到微信永久素材库（图文消息图片接口），返回 URL

    使用 cgi-bin/material/add_material?type=image，上传到永久素材库。
    返回的 url 可直接用于图文消息正文中的 <img> src 属性。
    """
    ctx = ssl.create_default_context()
    filename = os.path.basename(image_path)
    with open(image_path, "rb") as f:
        file_data = f.read()

    boundary = "----WechatContentImageBoundary7MA4YWxkTrZu0gW"
    body_parts = []
    body_parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="media"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
    )
    body_parts.append(file_data)
    body_parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(body_parts)

    # 使用永久素材上传接口，type=image
    url = f"{API_BASE}/cgi-bin/material/add_material?access_token={access_token}&type=image"
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    with urllib.request.urlopen(req, context=ctx) as resp:
        result = json.loads(resp.read())

    image_url = result.get("url")
    if not image_url:
        print(f"ERROR: 正文图片上传失败: {json.dumps(result, ensure_ascii=False)}")
        sys.exit(1)
    return image_url


# ─── 图片解析 / 上传 / 缓存 ─────────────────────────────

def _normalize_image_token(token):
    """规范化图片标识文本（描述或文件名）用于匹配"""
    token = token.strip().lower()
    token = os.path.splitext(token)[0]
    token = re.sub(r'[\s_\-]+', '', token)
    token = re.sub(r'[^\w\u4e00-\u9fff]', '', token)
    return token


def _parse_md_image_target(target):
    """解析 Markdown 图片目标，兼容可选 title 与 <...> 包裹语法"""
    target = target.strip()
    if not target:
        return ""

    angle_match = re.match(r'^<([^>]+)>(?:\s+["\'][^"\']*["\'])?$', target)
    if angle_match:
        return angle_match.group(1).strip()

    plain_match = re.match(r'^(\S+)(?:\s+["\'][^"\']*["\'])?$', target)
    if plain_match:
        return plain_match.group(1).strip()

    return target


def _list_local_images(md_file):
    """列出 Markdown 同级目录及 images/assets 子目录可候选图片"""
    md_dir = os.path.dirname(os.path.abspath(md_file)) or "."
    candidate_dirs = [md_dir, os.path.join(md_dir, "images"), os.path.join(md_dir, "assets")]
    image_paths = []

    for d in candidate_dirs:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            path = os.path.join(d, name)
            if os.path.isfile(path) and name.lower().endswith(SUPPORTED_IMAGE_EXTS):
                image_paths.append(path)

    return image_paths


def _resolve_md_image_to_file(md_file, img_target, img_alt, image_paths):
    """根据 Markdown 图片 target/alt 自动解析到本地文件路径"""
    md_dir = os.path.dirname(os.path.abspath(md_file)) or "."
    parsed_target = _parse_md_image_target(img_target)

    # 1) 已经是远程地址，直接返回
    if re.match(r'^https?://', parsed_target, re.IGNORECASE):
        return parsed_target, "remote"

    # 2) target 是本地路径（相对/绝对）
    if parsed_target:
        unquoted = urllib.parse.unquote(parsed_target)
        direct_path = unquoted if os.path.isabs(unquoted) else os.path.join(md_dir, unquoted)
        if os.path.exists(direct_path) and os.path.isfile(direct_path):
            return os.path.abspath(direct_path), "path"

    # 3) 按描述（alt）匹配本地图片文件名
    if not image_paths:
        return None, "missing"

    alt_key = _normalize_image_token(img_alt)
    target_key = _normalize_image_token(os.path.basename(parsed_target)) if parsed_target else ""
    keys = [k for k in [alt_key, target_key] if k]
    if not keys:
        return None, "missing"

    for k in keys:
        for path in image_paths:
            name_key = _normalize_image_token(os.path.basename(path))
            if k and (k == name_key or k in name_key or name_key in k):
                return os.path.abspath(path), "matched-alt"

    return None, "missing"


def _load_image_asset_map():
    """读取本地图片素材映射缓存"""
    if not os.path.exists(IMAGE_MAP_FILE):
        return {}
    try:
        with open(IMAGE_MAP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_image_asset_map(image_map):
    """保存图片素材映射缓存（写入失败时优雅降级，不影响主流程）"""
    try:
        folder = os.path.dirname(IMAGE_MAP_FILE)
        os.makedirs(folder, exist_ok=True)
        with open(IMAGE_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(image_map, f, ensure_ascii=False, indent=2)
    except (PermissionError, OSError) as e:
        print(f"WARNING: 无法保存图片缓存: {e}")


def replace_local_images_with_wechat_assets(md_file, html_content, access_token):
    """上传 Markdown 内本地图片并替换 HTML 为微信素材 URL

    工作流：
    1. 从原始 Markdown 提取所有图片引用
    2. 解析到本地文件（支持相对路径、alt 模糊匹配）
    3. 查缓存（image_asset_map.json），命中则复用 URL
    4. 未命中则上传到微信永久素材库 cgi-bin/material/add_material
    5. 将 HTML 中的图片 src 替换为微信素材 URL
    """
    with open(md_file, "r", encoding="utf-8") as f:
        md_text = f.read()

    md_img_pattern = re.compile(r'!\[([^\]]*)\]\(([^)\n]+)\)')
    refs = md_img_pattern.findall(md_text)
    if not refs:
        return html_content

    image_paths = _list_local_images(md_file)
    image_map = _load_image_asset_map()
    updated_map = False
    replacements = {}

    for alt, target in refs:
        resolved, source_kind = _resolve_md_image_to_file(md_file, target, alt, image_paths)
        src_key = _parse_md_image_target(target).strip()
        if not src_key:
            continue

        if source_kind == "remote":
            replacements[src_key] = resolved
            continue

        if not resolved:
            print(f"WARNING: 未找到图片文件，跳过: alt='{alt}', target='{target}'")
            continue

        cache_key = os.path.abspath(resolved)
        wechat_url = image_map.get(cache_key, "")
        if not wechat_url:
            print(f"上传正文图片: {resolved}")
            wechat_url = upload_content_image(access_token, resolved)
            image_map[cache_key] = wechat_url
            updated_map = True
            print(f"正文图片 URL: {wechat_url}")
        else:
            print(f"复用已上传正文图片: {resolved}")

        replacements[src_key] = wechat_url
        # 兼容 HTML 中可能出现的 URL 编码形式
        replacements[urllib.parse.quote(src_key, safe="/:@+")] = wechat_url
        replacements[urllib.parse.unquote(src_key)] = wechat_url

    if updated_map:
        _save_image_asset_map(image_map)
        print(f"正文图片映射已保存: {IMAGE_MAP_FILE}")

    if not replacements:
        return html_content

    for old_src, new_src in replacements.items():
        html_content = html_content.replace(f'src="{old_src}"', f'src="{new_src}"')

    return html_content


# ─── 封面图 / 推送 ──────────────────────────────────────

def get_cover_media_id(access_token, cover_path, article_mode=False, cmd_media_id=None):
    """获取封面图 media_id

    优先级：命令行 --media-id > config.yaml wechat.media_id > 上传封面图
    新闻模式：复用已保存的 media_id（同一张封面图）
    文章模式（article_mode=True）：每次都上传新封面（每篇文章封面图不同）
    """
    # 1. 命令行指定的 media_id 优先级最高
    if cmd_media_id:
        print(f"使用命令行指定的 media_id: {cmd_media_id}")
        return cmd_media_id

    # 2. config.yaml 中配置的 media_id
    config_media_id = load_config_media_id()
    if config_media_id:
        print(f"使用 config.yaml 配置的 media_id: {config_media_id}")
        return config_media_id

    # 3. 文章模式：每次都上传新封面图（不复用）
    cache_file = ARTICLE_COVER_MEDIA_ID_FILE if article_mode else COVER_MEDIA_ID_FILE

    if article_mode:
        if not os.path.exists(cover_path):
            print(f"ERROR: 封面图不存在: {cover_path}")
            sys.exit(1)
        print(f"上传封面图: {cover_path}")
        media_id = upload_image(access_token, cover_path)
        print(f"封面图 media_id: {media_id}")
        return media_id

    # 日报模式：检查是否有已保存的 media_id
    if os.path.exists(cache_file):
        with open(cache_file, "r", encoding="utf-8") as f:
            saved = f.read().strip()
        if saved:
            print(f"复用已保存的封面图 media_id: {saved}")
            return saved
    
    # 上传新封面图
    if not os.path.exists(cover_path):
        print(f"ERROR: 封面图不存在: {cover_path}")
        print("提示: 在当前目录下放一张名为 '封面图.png' 的图片，或用 --cover 指定路径")
        sys.exit(1)
    
    print(f"上传封面图: {cover_path}")
    media_id = upload_image(access_token, cover_path)
    
    # 保存 media_id
    with open(cache_file, "w", encoding="utf-8") as f:
        f.write(media_id)
    print(f"封面图 media_id 已保存到: {cache_file}")
    
    return media_id


# 硬编码的 1:1 裁剪坐标（用于成语典故系列封面）
# 封面图尺寸 2016×864，1:1 裁剪取中心竖条
# 格式：X1_Y1_X2_Y2（归一化坐标，0~1，下划线分隔）
# 计算：X1=1008/2016=0.5，Y1=0，X2=1872/2016≈0.928571，Y2=864/864=1
HARDCODED_PIC_CROP_1_1 = "0.5_0_0.928571_1"


MAX_CONTENT_LENGTH = 20000  # 微信草稿接口单篇字符限制
MAX_TITLE_LENGTH = 64  # 微信图文标题限制（草稿接口实际支持 64 字符）


def _truncate_title(title, max_len=MAX_TITLE_LENGTH):
    """截断标题到指定长度，保留后缀

    当标题含后缀（如「...（上）」「...（下）」）时，截断原文部分确保后缀不丢失。
    无后缀时直接截断。
    """
    if len(title) <= max_len:
        return title

    # 检测常见中文后缀：全角括号（上）（中）（下）（续）或（N）
    suffix_match = re.search(r'（[^）]*）\s*$', title)
    if suffix_match:
        suffix = suffix_match.group(0)
        available = max_len - len(suffix)
        if available > 4:  # 至少留 4 个字
            return title[:available] + suffix

    return title[:max_len]


def split_html_content(html_content, title):
    """将超限的 HTML 内容按 H2 标签拆分为多篇

    拆分策略：
    1. 提取 body style、封面 section、正文 section 外层标签
    2. 在正文 section 内按 H2 切割
    3. 按字符预算分组合并，每篇保留封面 + 正文片段
    4. 首篇保留原始标题，后续篇加 (上/中/下) 后缀
    5. 尾栏 section 只出现在最后一篇

    返回: [(part_title, part_html), ...]
    """
    # 1. 提取 body style
    body_style_match = re.search(r'<body\s+style="([^"]+)"', html_content)
    body_style = body_style_match.group(1) if body_style_match else ""

    # 2. 提取封面 section（从 <!-- 封面 --> 注释到下一个顶层 section 之前）
    cover_match = re.search(
        r'(<!-- 封面 -->.*?</section>)',
        html_content, re.DOTALL
    )
    cover_html = cover_match.group(1) if cover_match else ""

    # 3. 提取正文 section 的开标签（含 style）
    #    正文中还有代码块、引用块等子 section，需要精确匹配
    content_open_match = re.search(
        r'(<!-- 正文区域[^\n]*\n\s*-->\s*)(<section\s+style="[^"]+">)',
        html_content
    )
    if content_open_match:
        comment_part = content_open_match.group(1)
        content_section_open = content_open_match.group(2)
    else:
        # 回退：找 body 后第一个带 font-size/line-height 的 section
        fallback = re.search(
            r'(<section\s+style="[^"]*font-size[^"]*">)',
            html_content
        )
        comment_part = ""
        content_section_open = fallback.group(1) if fallback else '<section>'

    # 4. 提取尾栏 section（AI 模式的固定尾栏，最后一篇才保留）
    tail_match = re.search(
        r'(<section\s+style="[^"]*text-align:center[^"]*">.*?</section>\s*</body>)',
        html_content, re.DOTALL
    )
    tail_html = tail_match.group(1) if tail_match else ""

    # 5. 提取正文 section 内的内容（去掉外层标签）
    body_match = re.search(r'<body[^>]*>(.*)</body>', html_content, re.DOTALL)
    if not body_match:
        return [(title, html_content)]
    body_inner = body_match.group(1)

    # 找到正文 section 的开始位置
    if content_open_match:
        # 正文内容从 content_section_open 之后开始
        content_start = content_open_match.end()
    else:
        content_start = len(cover_html) + 10  # 估算

    # 提取正文内容（从正文 section 开始到尾栏之前）
    if tail_match:
        # 从 body_inner 中找到尾栏的位置
        tail_in_body = body_inner.find(tail_html.split("</body>")[0].strip())
        if tail_in_body > 0:
            content_area = body_inner[content_start:tail_in_body]
        else:
            content_area = body_inner[content_start:]
    else:
        content_area = body_inner[content_start:]

    # 6. 按 H2 拆分
    h2_pattern = re.compile(r'(<h2\s+style="[^"]+">)(.*?)(</h2>)', re.DOTALL)
    h2_positions = []
    for m in h2_pattern.finditer(content_area):
        # 只匹配顶层 H2（不在子 section 内的）
        # 简单启发：检查 H2 前面的 section 嵌套深度
        prefix = content_area[:m.start()]
        open_sections = prefix.count('<section')
        close_sections = prefix.count('</section')
        if open_sections - close_sections <= 1:  # 顶层
            h2_positions.append((m.start(), m.end(), m.group(2).strip()))

    if len(h2_positions) < 2:
        return [(title, html_content)]

    # 7. 切分为 H2 块
    chunks = []
    for idx, (start, end, h2_text) in enumerate(h2_positions):
        if idx + 1 < len(h2_positions):
            chunk = content_area[start:h2_positions[idx + 1][0]]
        else:
            chunk = content_area[start:]
        chunks.append((h2_text, chunk))

    # 8. 分组合并（每篇不超过 MAX_CONTENT_LENGTH）
    # 模板开销估算
    template_head = f'<!DOCTYPE html><html><head><meta charset="utf-8"></head><body style="{body_style}">\n'
    template_foot = '\n</body>\n</html>'
    per_article_overhead = len(template_head) + len(cover_html) + len(comment_part) + len(content_section_open) + len("</section>") + len(template_foot) + 100

    articles_content = []  # 每篇的正文块列表
    current_chunks = []
    current_len = 0

    for h2_text, chunk in chunks:
        chunk_len = len(chunk)
        if current_chunks and current_len + chunk_len > MAX_CONTENT_LENGTH - per_article_overhead:
            articles_content.append(current_chunks)
            current_chunks = []
            current_len = 0
        current_chunks.append(chunk)
        current_len += chunk_len

    if current_chunks:
        articles_content.append(current_chunks)

    if len(articles_content) == 1:
        return [(title, html_content)]

    # 9. 构建每篇完整 HTML
    parts = []
    n = len(articles_content)

    # 根据篇数选择后缀列表
    if n == 2:
        suffixes = ["（上）", "（下）"]
    elif n == 3:
        suffixes = ["（上）", "（中）", "（下）"]
    else:
        suffixes = [f"（{i+1}）" for i in range(n)]
        suffixes[-1] = "（续）"

    for i, chunk_list in enumerate(articles_content):
        merged_content = "".join(chunk_list)

        part_title = title + " " + suffixes[i]

        # 尾栏只加在最后一篇
        part_tail = tail_html if i == len(articles_content) - 1 else ""

        part_html = (
            f'{template_head}'
            f'{cover_html}\n'
            f'{comment_part}{content_section_open}'
            f'{merged_content}'
            f'</section>\n'
            f'{part_tail}'
            f'{template_foot}'
        )

        # 安全检查
        if len(part_html) > MAX_CONTENT_LENGTH:
            print(f"WARNING: 第 {i+1} 篇仍超限 ({len(part_html)} 字符)，截断处理")
            # 尝试去掉封面来省空间（非首篇）
            if i > 0:
                part_html = part_html.replace(cover_html + "\n", "")
            if len(part_html) > MAX_CONTENT_LENGTH:
                part_html = part_html[:MAX_CONTENT_LENGTH]

        parts.append((part_title, part_html))

    return parts


def push_draft(access_token, title, html_content, thumb_media_id, cover_path="", digest=""):
    """推送草稿箱（自动拆分超限内容为多篇合并推送）"""
    if len(html_content) > MAX_CONTENT_LENGTH:
        print(f"内容 {len(html_content)} 字符超过限制 {MAX_CONTENT_LENGTH}，自动拆分为多篇合并推送")
        parts = split_html_content(html_content, title)
        print(f"拆分为 {len(parts)} 篇:")
        for i, (pt, ph) in enumerate(parts):
            print(f"  第 {i+1} 篇: {pt} ({len(ph)} 字符)")
        return _push_multi_drafts(access_token, parts, thumb_media_id, cover_path, digest)

    return _push_single_draft(access_token, title, html_content, thumb_media_id, cover_path, digest)


def _make_article(title, html_content, thumb_media_id, digest="", pic_crop_1_1=""):
    """构建单篇图文消息结构"""
    article = {
        "article_type": "news",
        "title": title,
        "content": html_content,
        "thumb_media_id": thumb_media_id,
        "author": "木昆子",
        "need_open_comment": 1,
        "only_fans_can_comment": 1,
    }
    if digest:
        article["digest"] = digest
    if pic_crop_1_1:
        article["pic_crop_1_1"] = pic_crop_1_1
    return article


def _push_single_draft(access_token, title, html_content, thumb_media_id, cover_path="", digest=""):
    """推送单篇草稿"""
    # 生成摘要
    if not digest:
        plain_text = re.sub(r"<[^>]+>", "", html_content)
        plain_text = re.sub(r"\s+", " ", plain_text).strip()
        digest = plain_text[:120] if len(plain_text) > 120 else plain_text

    title = _truncate_title(title)
    print(f"标题: {title}")
    print(f"摘要: {digest[:50]}{'...' if len(digest) > 50 else ''}")
    print(f"内容长度: {len(html_content)} 字符")

    pic_crop_1_1 = HARDCODED_PIC_CROP_1_1 if cover_path else ""
    if pic_crop_1_1:
        print(f"封面图 1:1 裁剪坐标: {pic_crop_1_1}")

    article = _make_article(title, html_content, thumb_media_id, digest, pic_crop_1_1)
    payload = {"articles": [article]}

    ctx = ssl.create_default_context()
    url = f"{API_BASE}/cgi-bin/draft/add?access_token={access_token}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

    with urllib.request.urlopen(req, context=ctx) as resp:
        result = json.loads(resp.read())

    if result.get("errcode") and result["errcode"] != 0:
        print(f"ERROR: 推送失败: {json.dumps(result, ensure_ascii=False)}")
        sys.exit(1)

    media_id = result.get("media_id", "")
    print(f"✅ 推送成功! media_id: {media_id}")
    return media_id


def _push_multi_drafts(access_token, parts, thumb_media_id, cover_path="", digest=""):
    """推送多篇合并为一个图文消息（自动拆分后调用）"""
    pic_crop_1_1 = HARDCODED_PIC_CROP_1_1 if cover_path else ""

    articles = []
    for i, (part_title, part_html) in enumerate(parts):
        part_digest = digest or ""

        part_title = _truncate_title(part_title)

        article = _make_article(part_title, part_html, thumb_media_id, part_digest, pic_crop_1_1)
        articles.append(article)
        print(f"  第 {i+1} 篇: 「{part_title}」({len(part_html)} 字符)")

    payload = {"articles": articles}

    ctx = ssl.create_default_context()
    url = f"{API_BASE}/cgi-bin/draft/add?access_token={access_token}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

    with urllib.request.urlopen(req, context=ctx) as resp:
        result = json.loads(resp.read())

    if result.get("errcode") and result["errcode"] != 0:
        print(f"ERROR: 推送失败: {json.dumps(result, ensure_ascii=False)}")
        sys.exit(1)

    media_id = result.get("media_id", "")
    print(f"✅ 合并推送成功! {len(articles)} 篇, media_id: {media_id}")
    return media_id


def update_draft(access_token, media_id, title, html_content, thumb_media_id, cover_path="", digest="", index=0):
    """更新已有草稿（修改标题、正文、封面等）

    与 push_draft（新建）的区别：
    - API: cgi-bin/draft/update（而非 draft/add）
    - 需要 media_id（指定要修改的草稿）和 index（多图文中的位置）
    - articles 是单个对象而非数组
    - 不支持自动拆分（超限内容应先用 push_draft 新建后再更新）
    """
    # 生成摘要
    if not digest:
        plain_text = re.sub(r"<[^>]+>", "", html_content)
        plain_text = re.sub(r"\s+", " ", plain_text).strip()
        digest = plain_text[:120] if len(plain_text) > 120 else plain_text

    title = _truncate_title(title)
    print(f"更新草稿 media_id: {media_id}, index: {index}")
    print(f"标题: {title}")
    print(f"摘要: {digest[:50]}{'...' if len(digest) > 50 else ''}")
    print(f"内容长度: {len(html_content)} 字符")

    if len(html_content) > MAX_CONTENT_LENGTH:
        print(f"WARNING: 内容超过 {MAX_CONTENT_LENGTH} 字符，更新接口不支持自动拆分，请用 push_draft 新建")

    pic_crop_1_1 = HARDCODED_PIC_CROP_1_1 if cover_path else ""
    if pic_crop_1_1:
        print(f"封面图 1:1 裁剪坐标: {pic_crop_1_1}")

    article = _make_article(title, html_content, thumb_media_id, digest, pic_crop_1_1)

    # 更新接口：articles 是 object，不是 array
    payload = {
        "media_id": media_id,
        "index": index,
        "articles": article,
    }

    ctx = ssl.create_default_context()
    url = f"{API_BASE}/cgi-bin/draft/update?access_token={access_token}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

    with urllib.request.urlopen(req, context=ctx) as resp:
        result = json.loads(resp.read())

    if result.get("errcode") and result["errcode"] != 0:
        print(f"ERROR: 更新草稿失败: {json.dumps(result, ensure_ascii=False)}")
        sys.exit(1)

    print(f"✅ 草稿更新成功! media_id: {media_id}")
    return media_id


def extract_frontmatter(md_file):
    """从 Markdown 文件提取 frontmatter 元数据（标题、摘要等）

    支持 YAML frontmatter 格式：
    ---
    title: 标题
    digest: 摘要（可选，80字以内）
    ---
    """
    with open(md_file, "r", encoding="utf-8") as f:
        content = f.read()

    # 解析 YAML frontmatter（纯字符串解析，不依赖 yaml 模块）
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            frontmatter_block = parts[1]
            title = ""
            digest = ""
            for line in frontmatter_block.splitlines():
                line = line.strip()
                if line.startswith("title:"):
                    title = line[6:].strip().strip("\"'")
                elif line.startswith("digest:"):
                    digest = line[7:].strip().strip("\"'")
            if title:
                return title, digest

    # 回退：直接从 # 标题行提取
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip(), ""
    return "未命名日报", ""


# ─── 主入口 ─────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__.strip())
        sys.exit(1)

    args = sys.argv[1:]
    article_mode = False
    update_mode = False
    update_media_id = None

    # 识别模式参数
    if "--article" in args:
        article_mode = True
        args = [a for a in args if a != "--article"]
    if "--update" in args:
        update_mode = True
        update_idx = args.index("--update")
        args.pop(update_idx)
        # --update 后可能跟一个可选的 media_id
        if update_idx < len(args) and not args[update_idx].startswith("--"):
            update_media_id = args.pop(update_idx)

    if not args:
        print(__doc__.strip())
        sys.exit(1)

    md_file = args[0]
    title = None
    digest = None
    cover = None
    media_id = None

    i = 1
    while i < len(args):
        if args[i] == "--title" and i + 1 < len(args):
            title = args[i + 1]
            i += 2
        elif args[i] == "--cover" and i + 1 < len(args):
            cover = args[i + 1]
            i += 2
        elif args[i] == "--digest" and i + 1 < len(args):
            digest = args[i + 1]
            i += 2
        elif args[i] == "--media-id" and i + 1 < len(args):
            media_id = args[i + 1]
            i += 2
        else:
            i += 1

    # 根据模式设定默认封面图
    if cover is None:
        if article_mode and os.path.exists(DEFAULT_ARTICLE_COVER):
            cover = DEFAULT_ARTICLE_COVER
        else:
            cover = DEFAULT_COVER

    if not os.path.exists(md_file):
        print(f"ERROR: Markdown 文件不存在: {md_file}")
        sys.exit(1)

    # 1. 生成微信兼容 HTML
    base = os.path.splitext(md_file)[0]
    if article_mode:
        html_file = f"{base}_article_wechat.html"
        mode_label = "文章模式"
        cmd = [sys.executable, MD2WECHAT_SCRIPT, "--article", md_file, html_file]
    else:
        html_file = f"{base}_news_wechat.html"
        mode_label = "新闻模式"
        cmd = [sys.executable, MD2WECHAT_SCRIPT, "--news", md_file, html_file]

    print(f"步骤 1: Markdown → 微信兼容 HTML（{mode_label}）")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: HTML 生成失败:\n{result.stderr}")
        sys.exit(1)
    print(result.stdout.strip())

    # 读取 HTML 内容
    with open(html_file, "r", encoding="utf-8") as f:
        html_content = f.read()

    # 2. 获取标题和摘要（优先 frontmatter > 命令行参数 > 自动截取）
    frontmatter_title, frontmatter_digest = extract_frontmatter(md_file)
    if not title:
        title = frontmatter_title
        title = _truncate_title(title)
    if not digest:
        digest = frontmatter_digest
    if digest:
        print(f"摘要（frontmatter）: {digest}")

    # 3. 获取凭证并上传封面图
    print(f"\n步骤 2: 获取凭证并处理封面图")
    appid, secret = load_credentials()
    token = get_access_token(appid, secret)
    thumb_media_id = get_cover_media_id(token, cover, article_mode=article_mode, cmd_media_id=media_id)

    # 4. 正文图片上传与替换（支持缓存复用）
    print(f"\n步骤 3: 上传正文图片素材并替换引用")
    html_content = replace_local_images_with_wechat_assets(md_file, html_content, token)

    # 5. 推送 / 更新草稿箱
    if update_mode:
        if update_media_id:
            target_media_id = update_media_id
        elif os.path.exists(DRAFT_MEDIA_ID_FILE):
            with open(DRAFT_MEDIA_ID_FILE, "r", encoding="utf-8") as f:
                target_media_id = f.read().strip()
            print(f"从 draft_media_id.txt 读取上次草稿 media_id: {target_media_id}")
        else:
            print("ERROR: --update 模式下需要指定 media_id，或确保 draft_media_id.txt 存在")
            sys.exit(1)

        print(f"\n步骤 4: 更新草稿箱 (media_id: {target_media_id})")
        result_media_id = update_draft(token, target_media_id, title, html_content, thumb_media_id, cover_path=cover, digest=digest)

        print(f"\n完成! 草稿已更新: {result_media_id}")
    else:
        print(f"\n步骤 4: 推送草稿箱")
        result_media_id = push_draft(token, title, html_content, thumb_media_id, cover_path=cover, digest=digest)

        # 保存 media_id 供后续 --update 使用
        folder = os.path.dirname(DRAFT_MEDIA_ID_FILE)
        os.makedirs(folder, exist_ok=True)
        with open(DRAFT_MEDIA_ID_FILE, "w", encoding="utf-8") as f:
            f.write(result_media_id)
        print(f"草稿 media_id 已保存到: {DRAFT_MEDIA_ID_FILE}")

        print(f"\n完成! 草稿 media_id: {result_media_id}")


if __name__ == "__main__":
    main()
