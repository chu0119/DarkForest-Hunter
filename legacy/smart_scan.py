#!/usr/bin/env python3
"""智能扫描脚本 - 自动等待 GitHub 限流重置"""

import requests
import time
import subprocess
import re
import json
import os
import sys

def get_token():
    """获取 GitHub token"""
    try:
        result = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True)
        return result.stdout.strip()
    except:
        return None

def check_rate_limit(token):
    """检查 Code Search API 限流"""
    for attempt in range(3):
        try:
            headers = {'Authorization': f'token {token}'} if token else {}
            r = requests.get('https://api.github.com/rate_limit', headers=headers, timeout=20)
            data = r.json()
            cs = data['resources']['code_search']
            return cs['remaining'], cs['limit'], cs['reset']
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                # 超时情况下假设限流正常
                return 5, 10, int(time.time()) + 60

def wait_for_rate_limit(token):
    """等待限流重置"""
    remaining, limit, reset_ts = check_rate_limit(token)

    if remaining > 2:
        print(f'✅ 限流正常: {remaining}/{limit}')
        return True

    now = time.time()
    wait = max(5, reset_ts - now + 2)

    print(f'⏳ 限流预警: {remaining}/{limit}, 等待 {wait:.0f}s...')
    print(f'   重置时间: {time.strftime("%H:%M:%S", time.localtime(reset_ts))}')

    # 每 10 秒检查一次
    while wait > 0:
        time.sleep(min(10, wait))
        remaining, _, _ = check_rate_limit(token)
        if remaining > 2:
            print(f'✅ 限流已重置: {remaining}/{limit}')
            return True
        wait = max(0, reset_ts - time.time() + 2)

    return remaining > 0

def search_github(query, token, page=1, per_page=100):
    """搜索 GitHub Code"""
    url = f'https://api.github.com/search/code?q={query}&per_page={per_page}&page={page}'
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.text-match+json',
        'X-GitHub-Api-Version': '2022-11-28'
    }

    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=20)
            remaining = int(r.headers.get('X-RateLimit-Remaining', 10))

            if r.status_code == 200:
                return r.json().get('items', []), remaining
            elif r.status_code in [429, 403]:
                wait = 10 + (2 ** attempt) * 5
                print(f'  ⏳ 限流重试: 等待 {wait}s')
                time.sleep(wait)
            else:
                print(f'  ❌ HTTP {r.status_code}')
                return [], remaining
        except Exception as e:
            print(f'  ❌ 错误: {e}')
            time.sleep(3)

    return [], 0

def extract_keys(items):
    """从搜索结果中提取 key"""
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
                    'url': item.get('html_url', ''),
                    'source': 'github_code_search'
                })
    return keys

def load_existing_keys():
    """加载已有结果"""
    existing = set()
    try:
        with open('results/deepseek_keys_result.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
            for item in data:
                if item.get('valid'):
                    existing.add(item.get('key', ''))
    except:
        pass
    return existing

def load_queries(filename='queries_v6.txt'):
    """加载查询"""
    queries = []
    with open(filename, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                queries.append(line)
    return queries

def main():
    print('='*60)
    print('DeepSeek Key Hunter - 智能扫描')
    print('='*60)

    # 获取 token
    token = get_token()
    if not token:
        print('❌ 未找到 GitHub token')
        return

    print(f'✅ GitHub Token: {token[:10]}...')

    # 等待限流重置
    if not wait_for_rate_limit(token):
        print('❌ 限流未重置，退出')
        return

    # 加载数据
    existing_keys = load_existing_keys()
    queries = load_queries()

    print(f'📊 已有 {len(existing_keys)} 个有效 Key')
    print(f'📋 加载 {len(queries)} 条查询')

    # 扫描
    new_count = 0
    total_scanned = 0

    for i, q in enumerate(queries):
        try:
            print(f'\n[{i+1}/{len(queries)}] {q}')

            # 等待限流
            if not wait_for_rate_limit(token):
                print('⏸️  限流耗尽，暂停 60s')
                time.sleep(60)
                if not wait_for_rate_limit(token):
                    print('❌ 限流未恢复，退出')
                    break

            # 搜索
            items, remaining = search_github(q, token)
            total_scanned += len(items)
            print(f'  📄 结果: {len(items)} | 限流剩余: {remaining}')

            # 提取 key
            new_keys = extract_keys(items)
            for k in new_keys:
                if k['key'] not in existing_keys:
                    existing_keys.add(k['key'])
                    new_count += 1
                    print(f'    🆕 {k["key"][:25]}... ({k["repo"]})')

            # 延迟
            time.sleep(7)
        except Exception as e:
            print(f'  ⚠️ 查询出错: {e}, 继续下一个')
            time.sleep(10)

    # 总结
    print(f'\n{"="*60}')
    print(f'扫描完成!')
    print(f'  总扫描: {total_scanned} 个结果')
    print(f'  新增 Key: {new_count} 个')
    print(f'  累计 Key: {len(existing_keys)} 个')
    print(f'{"="*60}')

if __name__ == '__main__':
    main()
