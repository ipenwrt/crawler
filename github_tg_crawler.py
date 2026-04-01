# coding=utf-8
import requests
import re
import os
import time
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

# --- 1. 配置区 ---
GH_TOKEN = os.getenv("GH_TOKEN", "")

# GitHub 搜索策略
GH_QUERIES = [
    "filename:valid-domains.txt", 
    ' "api/v1/client/subscribe?token=" extension:txt ',
    ' "v2board" AND "sub=" extension:py '
]

# 排除关键词列表 (新增 glpat- 等疑似 Token 的前缀过滤，防止 Push 失败)
EXCLUDE_KEYWORDS = [
    "louwangzhiyu", "yywhale", "nxxbbf", "slianvpn", "cloudaddy", "quickbeevpn",
    "tianmiao", "cokecloud", "boluoidc", "gpket", "fast8888", "ykxqn",
    "127.0.0.1", "localhost", "github", "google", "tencent", "apple", "cloudfront",
    "w3.org", "telegram.org", "t.me", "wikipedia", "pypi", "docker", "example",
    "microsoft", "facebook", "twitter", "jsvini", "v2cross", "vercel.app", "netlify.app",
    "glpat-", "ghp_", "gho_", "ghu_", "ghs_", "ghr_" # 常见 Token 前缀
]

# 静态资源后缀
STATIC_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".css", ".js", ".ico", ".woff", ".woff2", ".zip", ".rar")

# 订阅链接特征
SUB_PATTERNS = [
    "token=", "sub=", "/sub?", "/sub/", "subscribe=", "/subscribe?", 
    "api/v1/client/subscribe", "api/v1/client/sub"
]

# TG 频道源
TG_CHANNELS = [
    "https://t.me/s/v2board_share", "https://t.me/s/free_v2board", "https://t.me/s/v2board_channel",
    "https://t.me/s/v2ray_free_dy", "https://t.me/s/v2board_dy", "https://t.me/s/v2ray_free_clash",
    "https://t.me/s/clash_v2board", "https://t.me/s/Jichang_Share", "https://t.me/s/vpn_free_link",
    "https://t.me/s/nodes_share", "https://t.me/s/v2ray_free_nodes", "https://t.me/s/free_jichang_share",
    "https://t.me/s/jichang_shiyong", "https://t.me/s/free_node_channel", "https://t.me/s/jichang_list"
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

URL_PATTERN = re.compile(r'https?://[^\s\'"<>]+')
IP_PATTERN = re.compile(r'^(\d{1,3}\.){3}\d{1,3}(:\d+)?$')

def is_valid_url(url):
    u_low = url.strip().lower()
    if not u_low.startswith('http') or len(u_low) < 15: 
        return False
    # 增加对疑似 Token 的正则拦截 (例如 glpat- 后跟 20 位字符)
    if any(k in u_low for k in EXCLUDE_KEYWORDS): 
        return False
    if '{' in u_low or '$' in u_low: 
        return False
    if "githubusercontent" in u_low:
        return False
    if u_low.endswith(STATIC_SUFFIXES):
        return False
    return True

def http_get(url, timeout=15):
    try:
        headers = HEADERS.copy()
        if "api.github.com" in url:
            if GH_TOKEN:
                headers["Authorization"] = f"token {GH_TOKEN}"
            headers["Accept"] = "application/vnd.github.v3+json"
        
        res = requests.get(url, headers=headers, timeout=timeout)
        if res.status_code == 200:
            return res
        return None
    except:
        return None

def fetch_github():
    links = set()
    for q in GH_QUERIES:
        api_url = f"https://api.github.com/search/code?q={q}&sort=indexed&per_page=100"
        res = http_get(api_url)
        if not res: continue
        try:
            items = res.json().get('items', [])
            print(f" -> [GitHub] 关键词 '{q[:20]}...' 匹配到 {len(items)} 个文件")
            for item in items:
                raw_url = item.get('html_url', '').replace('github.com', 'raw.githubusercontent.com').replace('/blob/', '/')
                file_res = http_get(raw_url, timeout=10)
                if file_res:
                    found = URL_PATTERN.findall(file_res.text)
                    links.update([l.rstrip('.,/') for l in found if is_valid_url(l)])
        except: continue
        time.sleep(2)
    return links

def fetch_tg():
    links = set()
    for url in TG_CHANNELS:
        res = http_get(url, timeout=15)
        if res:
            found = URL_PATTERN.findall(res.text)
            valid = [l.rstrip('.,/') for l in found if is_valid_url(l)]
            links.update(valid)
            # 优化点：仅打印数量大于 0 的频道
            if len(valid) > 0:
                print(f" -> [TG] 频道: {url.split('/')[-1]} | 有效链接: {len(valid)} 条")
    return links

if __name__ == "__main__":
    start_time = time.time()
    print(f"=== BOT 启动：开始深度采集 (时间: {time.strftime('%Y-%m-%d %H:%M:%S')}) ===")
    
    with ThreadPoolExecutor(max_workers=5) as executor:
        f1 = executor.submit(fetch_github)
        f2 = executor.submit(fetch_tg)
        all_found = f1.result().union(f2.result())

    final_domains = set()
    final_subs = set()

    for link in all_found:
        try:
            parsed = urlparse(link)
            if parsed.netloc and '.' in parsed.netloc:
                if IP_PATTERN.match(parsed.netloc): continue
                
                final_domains.add(f"{parsed.scheme}://{parsed.netloc}")
                
                link_lower = link.lower()
                if any(p in link_lower for p in SUB_PATTERNS):
                    # 二次过滤，确保提交时不会因为包含敏感词被 GitHub 拦截
                    if "glpat-" not in link_lower:
                        final_subs.add(link)
        except: continue

    with open("domains.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(list(final_domains))))
    
    with open("subscribes.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(list(final_subs))))

    duration = int(time.time() - start_time)
    print("-" * 50)
    print(f"采集汇总 | 耗时: {duration}s")
    print(f" -> 原始发现总数: {len(all_found)} 条")
    print(f" -> 纯净域名: {len(final_domains)} 个")
    print(f" -> 有效订阅: {len(final_subs)} 条")
    print("-" * 50)
