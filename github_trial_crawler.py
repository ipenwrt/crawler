import aiohttp
import asyncio
import re
import csv
import base64
from urllib.parse import urlparse
import os

# 常见订阅协议正则（优先协议链接，其次订阅 URL）
SUB_PATTERNS = [
    r'(?i)(vmess|vless|trojan|ss|ssr|hysteria2)://[^\s\r\n]+',
    r'(?i)https?://[^\s\r\n]{10,}(?:\?[^\s\r\n]*)?(?:#[^\s\r\n]*)?'  # 长 URL 作为订阅补充
]

async def search_github(session, token, page=1, per_page=100):
    """搜索 GitHub trial.cache 文件"""
    url = "https://api.github.com/search/code"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Mozilla/5.0",  # 避免被挡
    }
    if token:
        headers["Authorization"] = f"token {token}"
    
    params = {
        "q": 'filename:trial.cache',
        "per_page": per_page,
        "page": page
    }
    async with session.get(url, headers=headers, params=params) as resp:
        if resp.status == 403:
            print("Rate limit hit. Waiting 60s...")
            await asyncio.sleep(60)
            return await search_github(session, token, page, per_page)  # 重试
        if resp.status != 200:
            print(f"API error {resp.status}")
            return []
        data = await resp.json()
        print(f"Page {page}: total={data.get('total_count', 0)}, items={len(data.get('items', []))}")
        return data.get('items', [])

def get_raw_url(item):
    """从搜索结果获取 raw 文件 URL（修正版）"""
    try:
        full_name = item['repository']['full_name']
        default_branch = item['repository']['default_branch']
        path = item['path']
        return f"https://raw.githubusercontent.com/{full_name}/{default_branch}/{path}"
    except KeyError:
        return None

async def extract_links(content):
    """提取订阅链接，支持 Base64 解码"""
    links = set()
    
    # 直接匹配文本
    for pattern in SUB_PATTERNS:
        matches = re.findall(pattern, content, re.MULTILINE)
        links.update(matches)
    
    # 尝试整内容 Base64 解码
    try:
        decoded = base64.b64decode(content.encode('utf-8', errors='ignore')).decode('utf-8', errors='ignore')
        for pattern in SUB_PATTERNS:
            matches = re.findall(pattern, decoded, re.MULTILINE)
            links.update(matches)
    except:
        pass
    
    # 逐行 Base64 解码（常见订阅格式）
    for line in content.splitlines():
        line = line.strip()
        if len(line) > 10:  # 忽略短行
            try:
                decoded = base64.b64decode(line.encode('utf-8', errors='ignore')).decode('utf-8', errors='ignore')
                for pattern in SUB_PATTERNS:
                    matches = re.findall(pattern, decoded, re.MULTILINE)
                    links.update(matches)
            except:
                pass
    
    # 过滤明显非订阅链接（可选优化）
    filtered_links = {link for link in links if any(proto in link.lower() for proto in ['vmess', 'vless', 'trojan', 'ss://', 'ssr://', 'hy', 'hysteria'])}
    return list(filtered_links)

async def download_content(session, raw_url):
    """下载文件内容"""
    try:
        async with session.get(raw_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                return await resp.text()
            else:
                print(f"Download fail {resp.status}: {raw_url[:80]}...")
    except Exception as e:
        print(f"Download error: {e}")
    return None

async def process_item(session, item, all_links, csv_data):
    """处理单个 item（并发安全）"""
    raw_url = get_raw_url(item)
    if not raw_url:
        return
    
    content = await download_content(session, raw_url)
    if content:
        links = await asyncio.to_thread(extract_links, content)  # offload CPU to thread
        if links:
            all_links.update(links)
            repo = item['repository']['full_name']
            path = item['path']
            csv_data.append([repo, path, raw_url, len(links)])
            print(f"✓ {repo}/{path}: {len(links)} links")

async def main(token=None):
    connector = aiohttp.TCPConnector(
        limit=30, 
        limit_per_host=5,
        ttl_dns_cache=300,
        use_dns_cache=True
    )
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        
        all_links = set()
        csv_data = []
        
        page = 1
        max_pages = 10  # 限制最多1000结果，避免无限循环
        
        while page <= max_pages:
            print(f"\n--- Searching page {page} ---")
            items = await search_github(session, token, page)
            if not items:
                break
            
            # 并发处理当前页 items
            tasks = [process_item(session, item, all_links, csv_data) for item in items if get_raw_url(item)]
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        print(f"Task error: {r}")
            
            print(f"Page {page} done. Cumulative links: {len(all_links)}, files: {len(csv_data)}")
            
            if len(items) < 100:
                break
            page += 1
            await asyncio.sleep(1)  # 礼让 API
        
        # 输出文件
        print(f"\n=== 最终结果 ===")
        with open('utils.txt', 'w', encoding='utf-8') as f:
            for link in sorted(all_links):
                f.write(link.strip() + '\n')
        print(f"Total unique links: {len(all_links)} -> utils.txt")
        
        with open('trial.csv', 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['Repository', 'Path', 'Raw URL', 'Link Count'])
            writer.writerows(csv_data)
        print(f"Total trial.cache files with links: {len(csv_data)} -> trial.csv")

if __name__ == "__main__":
    token = os.getenv('GITHUB_TOKEN')
    asyncio.run(main(token))
