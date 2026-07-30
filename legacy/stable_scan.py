#!/usr/bin/env python3
"""稳定扫描脚本 - 自动处理限流和错误"""

import requests
import time
import subprocess
import re
import json
import sys

def log(msg):
    print(f'[{time.strftime("%H:%M:%S")}] {msg}')
    sys.stdout.flush()

def get_token():
    try:
        result = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True)
        return result.stdout.strip()
    except:
        return None

def check_rate_limit(token):
    for attempt in range(3):
        try:
            headers = {'Authorization': f'token {token}'} if token else {}
            r = requests.get('https://api.github.com/rate_limit', headers=headers, timeout=30)
            data = r.json()
            cs = data['resources']['code_search']
            return cs['remaining'], cs['limit'], cs['reset']
        except:
            if attempt < 2:
                time.sleep(5)
            else:
                return 5, 10, int(time.time()) + 60

def wait_for_reset(token):
    remaining, limit, reset_ts = check_rate_limit(token)
    if remaining >= 3:
        return remaining

    wait = max(5, reset_ts - time.time() + 2)
    log(f'⏳ 限流: {remaining}/{limit}, 等待 {wait:.0f}s')

    while time.time() < reset_ts + 2:
        time.sleep(min(10, max(1, reset_ts - time.time() + 1)))

    remaining, _, _ = check_rate_limit(token)
    log(f'✅ 限流重置: {remaining}/10')
    return remaining

def search(query, token, page=1):
    url = f'https://api.github.com/search/code?q={query}&per_page=100&page={page}'
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.text-match+json',
        'X-GitHub-Api-Version': '2022-11-28'
    }

    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            remaining = int(r.headers.get('X-RateLimit-Remaining', 10))

            if r.status_code == 200:
                return r.json().get('items', []), remaining
            elif r.status_code in [429, 403]:
                log(f'  ⏳ 限流重试 ({attempt+1}/3)')
                time.sleep(15 + attempt * 10)
            else:
                log(f'  ❌ HTTP {r.status_code}')
                return [], remaining
        except Exception as e:
            log(f'  ❌ 超时/错误: {e}')
            time.sleep(5)

    return [], 0

def extract_keys(items):
    keys = []
    for item in items:
        for match in item.get('text_matches', []):
            fragment = match.get('fragment', '')
            found = re.findall(r'sk-(?:proj-)?[a-zA-Z0-9]{32,64}', fragment)
            for key in found:
                keys.append({
                    'key': key,
                    'repo': item.get('repository', {}).get('full_name', 'unknown'),
                    'file': item.get('path', 'unknown'),
                })
    return keys

def main():
    log('='*50)
    log('DeepSeek Key Hunter - 稳定扫描')
    log('='*50)

    token = get_token()
    if not token:
        log('❌ 未找到 GitHub token')
        return

    log(f'✅ Token: {token[:10]}...')

    # 等待限流重置
    wait_for_reset(token)

    # 加载查询
    queries = []
    with open('queries_v6.txt', 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                queries.append(line)
    log(f'📋 {len(queries)} 条查询')

    # 加载已有结果
    existing_keys = set()
    try:
        with open('results/deepseek_keys_result.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
            for item in data:
                if item.get('valid'):
                    existing_keys.add(item.get('key', ''))
    except:
        pass
    log(f'📊 {len(existing_keys)} 个已有 Key')

    # 扫描
    new_count = 0
    for i, q in enumerate(queries):
        log(f'\\n[{i+1}/{len(queries)}] {q}')

        # 等待限流
        remaining = wait_for_reset(token)
        if remaining < 2:
            log('⏸️ 限流不足，等待 60s')
            time.sleep(60)
            remaining = wait_for_reset(token)
            if remaining < 2:
                log('❌ 限流未恢复，跳过')
                continue

        # 搜索
        items, remaining = search(q, token)
        log(f'  📄 {len(items)} 结果 | 限流: {remaining}')

        # 提取 key
        new_keys = extract_keys(items)
        for k in new_keys:
            if k['key'] not in existing_keys:
                existing_keys.add(k['key'])
                new_count += 1
                log(f'    🆕 {k["key"][:25]}... ({k["repo"]})')

        # 延迟
        time.sleep(7)

    log(f'\\n{"="*50}')
    log(f'扫描完成! 新增: {new_count} | 累计: {len(existing_keys)}')
    log(f'{"="*50}')

if __name__ == '__main__':
    main()
