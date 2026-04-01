# GitHub Trial Cache Crawler

**GitHub Trial Cache Crawler** 是一个自动化工具，用于搜索 GitHub 全网公开仓库中的 `trial.cache` 文件，提取其中的订阅链接（vmess、vless、ss、ssr、trojan 等协议），并生成输出文件。

**注意**：
- GitHub Search API 结果上限为 **1000 个**（无法真正“全网所有”，但覆盖大部分公开结果）。
- 每页 100 个结果，分页爬取。
- 使用 GitHub Token 提高速率限制（Actions 中自动使用 `${{ secrets.GITHUB_TOKEN }}`）。
- 提取逻辑：使用正则匹配常见订阅协议链接，支持 Base64 解码尝试。
- 去重后写入 `utils.txt`。
- 爬取记录写入 `trial.csv`（包含仓库、路径、Raw URL、提取链接数）。

## 输出文件

- `utils.txt`：提取的所有唯一订阅链接（一行一个，已去重）。
- `trial.csv`：爬取统计（Repository, Path, Raw URL, Link Count）。

## 脚本：`github_trial_crawler.py`

```python
import aiohttp
import asyncio
import re
import csv
import base64
import json
from urllib.parse import urljoin, urlparse
import os

# 常见订阅协议正则
SUB_PATTERNS = [
    r'(vmess|ss|ssr|vless|trojan|hysteria2)://[^\s\r\n]+',
    r'https?://[^\s\r\n]+(?:\?[^#\s\r\n]+)?(?:#[^\s\r\n]+)?'  # 补充订阅 URL
]

async def search_github(session, token, page=1, per_page=100):
    """搜索 GitHub trial.cache 文件"""
    url = "https://api.github.com/search/code"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {token}" if token else "token github-actions[bot]"
    }
    params = {
        "q": 'filename:trial.cache',
        "per_page": per_page,
        "page": page
    }
    async with session.get(url, headers=headers, params=params) as resp:
        if resp.status == 403:
            print("Rate limit hit. Waiting...")
            await asyncio.sleep(60)
            return []
        data = await resp.json()
        return data.get('items', [])

def get_raw_url(item):
    """从搜索结果获取 raw 文件 URL"""
    html_url = item['html_url']
    # 示例: https://github.com/user/repo/blob/main/trial.cache -> raw
    parsed = urlparse(html_url)
    path_parts = parsed.path.split('/')
    if len(path_parts) >= 4 and path_parts[-1] == 'trial.cache':
        repo = '/'.join(path_parts[3:-2])  # user/repo
        branch = path_parts[-2]
        raw_path = '/'.join(path_parts[3:])
        return f"https://raw.githubusercontent.com/{repo}/{branch}/{raw_path}"
    return None

async def extract_links(content):
    """提取订阅链接，支持 Base64 解码"""
    links = set()
    
    # 直接匹配
    for pattern in SUB_PATTERNS:
        matches = re.findall(pattern, content, re.IGNORECASE | re.MULTILINE)
        links.update(matches)
    
    # 尝试 Base64 解码整个内容或行
    try:
        decoded = base64.b64decode(content.encode()).decode('utf-8', errors='ignore')
        for pattern in SUB_PATTERNS:
            matches = re.findall(pattern, decoded, re.IGNORECASE | re.MULTILINE)
            links.update(matches)
    except:
        pass
    
    # 逐行 Base64 解码
    for line in content.splitlines():
        line = line.strip()
        if line:
            try:
                decoded = base64.b64decode(line.encode()).decode('utf-8', errors='ignore')
                for pattern in SUB_PATTERNS:
                    matches = re.findall(pattern, decoded, re.IGNORECASE | re.MULTILINE)
                    links.update(matches)
            except:
                pass
    
    return list(links)

async def download_content(session, raw_url):
    """下载文件内容"""
    try:
        async with session.get(raw_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                return await resp.text()
    except:
        pass
    return None

async def main(token=None):
    connector = aiohttp.TCPConnector(limit=50, limit_per_host=10)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        
        all_links = set()
        csv_data = []
        
        page = 1
        while True:
            print(f"Searching page {page}...")
            items = await search_github(session, token, page)
            if not items:
                break
            
            print(f"Found {len(items)} items on page {page}")
            
            tasks = []
            for item in items:
                raw_url = get_raw_url(item)
                if raw_url:
                    tasks.append(process_item(session, item, raw_url, all_links, csv_data))
            
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            
            total_count = len(items) * (page - 1) + len(items)
            print(f"Total processed so far: {total_count}")
            
            if len(items) < 100:
                break
            page += 1
        
        # 写入 utils.txt
        with open('utils.txt', 'w', encoding='utf-8') as f:
            for link in sorted(all_links):
                f.write(link + '\n')
        print(f"Total unique links: {len(all_links)}")
        
        # 写入 trial.csv
        with open('trial.csv', 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['Repository', 'Path', 'Raw URL', 'Link Count'])
            writer.writerows(csv_data)
        print(f"Total trial.cache files: {len(csv_data)}")

async def process_item(session, item, raw_url, all_links, csv_data):
    content = await download_content(session, raw_url)
    if content:
        links = await extract_links(content)
        if links:
            all_links.update(links)
            repo = item['repository']['full_name']
            path = item['path']
            csv_data.append([repo, path, raw_url, len(links)])
            print(f"✓ {repo}/{path}: {len(links)} links")

# 运行
if __name__ == "__main__":
    token = os.getenv('GITHUB_TOKEN')
    asyncio.run(main(token))
