<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DarkForest Hunter</title>
<style>
  :root {
    --bg: #080d15;
    --text: #c8d6e5;
    --accent: #4dabf7;
    --accent2: #20c997;
    --warn: #ff6b6b;
    --surface: #0f1923;
    --border: #1a2d3d;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; line-height: 1.7; }
  .container { max-width: 900px; margin: 0 auto; padding: 40px 24px; }

  /* Language toggle */
  .lang-bar { display: flex; justify-content: flex-end; gap: 8px; margin-bottom: 40px; }
  .lang-btn { padding: 6px 16px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); color: var(--text); cursor: pointer; font-size: 14px; transition: 0.2s; }
  .lang-btn.active { border-color: var(--accent); color: var(--accent); background: rgba(77,171,247,0.08); }
  .lang-btn:hover { border-color: var(--accent); }

  /* Sections language visibility */
  .zh, .en { display: none; }
  body.zh .zh { display: inherit; }
  body.zh .en { display: none; }
  body.en .en { display: inherit; }
  body.en .zh { display: none; }

  /* For block elements */
  body.zh .zh-block { display: block; }
  body.zh .en-block { display: none; }
  body.en .en-block { display: block; }
  body.en .zh-block { display: none; }

  /* Hero */
  .hero { text-align: center; padding: 60px 0 50px; }
  .hero h1 { font-size: 48px; font-weight: 800; background: linear-gradient(135deg, #4dabf7 0%, #20c997 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 12px; }
  .hero .tagline { font-size: 20px; color: #7a8ba0; }
  .hero .subtitle { font-size: 15px; color: #566577; margin-top: 8px; }

  .badges { display: flex; justify-content: center; gap: 12px; margin-top: 24px; flex-wrap: wrap; }
  .badge { padding: 5px 14px; border-radius: 20px; font-size: 13px; font-weight: 600; }
  .badge.python { background: rgba(55,118,179,0.2); color: #5b9bd5; border: 1px solid rgba(55,118,179,0.3); }
  .badge.scanners { background: rgba(32,201,151,0.15); color: #20c997; border: 1px solid rgba(32,201,151,0.25); }
  .badge.queries { background: rgba(255,107,107,0.15); color: #ff8787; border: 1px solid rgba(255,107,107,0.25); }

  /* Dark Forest Quote */
  .quote-box { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 28px 32px; margin: 30px 0; position: relative; }
  .quote-box::before { content: '"'; position: absolute; top: 10px; left: 16px; font-size: 60px; color: var(--accent); opacity: 0.3; font-family: Georgia, serif; line-height: 1; }
  .quote-box p { font-style: italic; font-size: 16px; padding-left: 24px; color: #9aafc4; }

  /* Sections */
  section { margin: 48px 0; }
  h2 { font-size: 26px; font-weight: 700; color: #e8ecf2; margin-bottom: 18px; padding-bottom: 10px; border-bottom: 2px solid var(--border); }
  h3 { font-size: 19px; font-weight: 600; color: #d0d8e2; margin: 28px 0 12px; }
  p { margin: 10px 0; }

  /* Code blocks */
  code { background: var(--surface); padding: 2px 8px; border-radius: 4px; font-size: 14px; color: var(--accent2); border: 1px solid var(--border); }
  pre { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 18px 22px; overflow-x: auto; margin: 16px 0; font-size: 14px; }
  pre code { border: none; padding: 0; color: var(--text); }

  /* Table */
  table { width: 100%; border-collapse: collapse; margin: 16px 0; }
  th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--border); }
  th { color: #a0b0c0; font-weight: 600; font-size: 14px; }
  td { font-size: 14px; }

  /* Forest visual */
  .forest-line { text-align: center; color: #1a3d2a; font-size: 20px; margin: 30px 0; letter-spacing: 8px; user-select: none; }

  /* Warning box */
  .warning-box { background: rgba(255,107,107,0.06); border: 1px solid rgba(255,107,107,0.25); border-radius: 12px; padding: 22px 28px; margin: 30px 0; }
  .warning-box h3 { color: var(--warn); margin-top: 0; }

  /* Button */
  .btn { display: inline-block; padding: 10px 24px; border-radius: 8px; background: var(--accent); color: #fff; text-decoration: none; font-weight: 600; font-size: 14px; transition: 0.2s; }
  .btn:hover { background: #5bb8f8; }

  hr { border: none; border-top: 1px solid var(--border); margin: 40px 0; }

  .footer { text-align: center; color: #566577; font-size: 13px; margin-top: 60px; }
  .footer a { color: var(--accent); }

  @media (max-width: 640px) {
    .hero h1 { font-size: 32px; }
    .container { padding: 20px 16px; }
  }
</style>
<script>
document.addEventListener('DOMContentLoaded', function() {
  var saved = localStorage.getItem('dkfh-lang') || 'zh';
  setLang(saved);
  document.querySelectorAll('.lang-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      setLang(this.dataset.lang);
    });
  });
  function setLang(lang) {
    document.body.className = lang;
    localStorage.setItem('dkfh-lang', lang);
    document.querySelectorAll('.lang-btn').forEach(function(b) {
      b.classList.toggle('active', b.dataset.lang === lang);
    });
  }
});
</script>
</head>
<body class="zh">

<div class="container">

<!-- Language Toggle -->
<div class="lang-bar">
  <button class="lang-btn active" data-lang="zh">中文</button>
  <button class="lang-btn" data-lang="en">English</button>
</div>

<!-- Hero -->
<div class="hero">
  <h1>🌲 DarkForest Hunter</h1>
  <div class="zh tagline">宇宙就是一座黑暗森林，每个文明都是带枪的猎人</div>
  <div class="en tagline">The universe is a dark forest. Every civilization is an armed hunter.</div>
  <div class="zh subtitle">— 《三体II：黑暗森林》 刘慈欣</div>
  <div class="en subtitle">— <em>The Dark Forest</em>, Cixin Liu</div>
  <div class="badges">
    <span class="badge python">Python 3.10+</span>
    <span class="badge scanners">14 种扫描源</span>
    <span class="badge queries">238 条搜索模式</span>
  </div>
</div>

<!-- Dark Forest Quote -->
<div class="quote-box">
  <p class="zh">
    在 GitHub 这片代码森林中，数以千万计的开发者日复一日地提交代码。<br>
    每一行 <code>API_KEY=sk-...</code> 都是一次"广播"——<br>
    一个暴露了自己坐标的文明。<br>
    我们，是这片森林里的猎人。<br>
    不是为了猎杀，而是为了在别人开枪之前，<br>
    <strong>告诉他们：你暴露了。</strong>
  </p>
  <p class="en">
    In the code forest of GitHub, millions of developers commit code every day.<br>
    Every line of <code>API_KEY=sk-...</code> is a "broadcast" —<br>
    a civilization revealing its coordinates.<br>
    We are the hunters in this forest.<br>
    Not to destroy, but to warn —<br>
    <strong>before someone else pulls the trigger.</strong>
  </p>
</div>

<!-- Project Motivations -->
<section>
  <h2 class="zh">🔭 项目背景</h2>
  <h2 class="en">🔭 Why This Exists</h2>

  <p class="zh">
    DeepSeek 已经成为全球开发者最常用的 AI API 之一。每天，成千上万的开发者将 API Key
    硬编码在配置文件、测试脚本、Jupyter Notebook、Docker Compose 甚至 GitHub Actions 中，
    然后不小心推送到公开仓库。
  </p>
  <p class="zh">
    我们最初做这个工具，是为了研究一个朴素的命题：<strong>在公开代码中，到底有多少
    DeepSeek key 被意外泄露？</strong> 几次扫描之后，我们发现数字远比想象中惊人——
    不仅有 key，而且很多<strong>还有高额余额</strong>。这意味着这些 key 在被我们发现之前，
    已经暴露了几个月甚至更久，却无人知晓。
  </p>
  <p class="zh">
    这和《三体》中的黑暗森林法则惊人地相似——每个泄露的 key 都是一次广播，
    暴露了自己的位置。只不过，在安全领域，猎人可能是：自动化脚本、加密货币矿工、
    数据窃贼，或其他恶意行为者。我们把这个工具开源，是希望让<strong>善意的猎人
    先到达现场</strong>。
  </p>

  <p class="en">
    DeepSeek has become one of the most widely used AI APIs. Every day, thousands of developers
    hardcode API keys in config files, test scripts, Jupyter Notebooks, Docker Compose files,
    and GitHub Actions — then accidentally push to public repositories.
  </p>
  <p class="en">
    We built this tool to answer a simple question: <strong>how many DeepSeek keys are
    exposed in public code?</strong> The answer shocked us — not just keys, but many with
    <strong>significant balances</strong>. These keys had been sitting exposed for months,
    completely unnoticed.
  </p>
  <p class="en">
    This mirrors the Dark Forest theory from Liu Cixin's <em>Three-Body Problem</em>: every
    leaked key is a broadcast revealing coordinates. Except in cybersecurity, the hunters
    could be: automated bots, crypto miners, data thieves, or worse. We open-source this tool
    so that <strong>ethical hunters find the prey first</strong>.
  </p>
</section>

<div class="forest-line">🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲</div>

<!-- What it does -->
<section>
  <h2 class="zh">🎯 它能做什么</h2>
  <h2 class="en">🎯 What It Does</h2>

  <p class="zh">
    全自动扫描 <strong>14 个平台</strong>，使用 <strong>238 条搜索模式</strong>，
    发现公开暴露的 DeepSeek API Key，然后<strong>验证有效性</strong>并<strong>查询余额</strong>。
  </p>
  <p class="en">
    Automatically scans <strong>14 platforms</strong> with <strong>238 search patterns</strong>
    to find publicly exposed DeepSeek API keys, then <strong>validates</strong> each one and
    <strong>checks the balance</strong>.
  </p>

  <h3 class="zh">覆盖的扫描源</h3>
  <h3 class="en">Scanning Sources</h3>
  <table>
    <tr><th class="zh">类别</th><th class="en">Category</th><th class="zh">来源</th><th class="en">Sources</th></tr>
    <tr><td class="zh">代码托管</td><td class="en">Code Hosting</td><td>GitHub Code Search, GitHub Gist, GitHub Issues, GitHub Commits, GitLab, Gitee</td></tr>
    <tr><td class="zh">AI 平台</td><td class="en">AI Platforms</td><td>HuggingFace (Models, Datasets, Spaces)</td></tr>
    <tr><td class="zh">包管理器</td><td class="en">Package Registries</td><td>PyPI, npm</td></tr>
    <tr><td class="zh">开发者社区</td><td class="en">Dev Communities</td><td>Stack Overflow</td></tr>
    <tr><td class="zh">镜像/归档</td><td class="en">Archives</td><td>Docker Hub, Wayback Machine, Common Crawl</td></tr>
    <tr><td class="zh">实时监控</td><td class="en">Real-time</td><td>GitHub Events (PushEvent stream)</td></tr>
  </table>

  <h3 class="zh">主要用途</h3>
  <h3 class="en">Use Cases</h3>
  <ul>
    <li class="zh"><strong>安全研究</strong> — 量化分析 API key 泄露的规模和模式</li>
    <li class="en"><strong>Security Research</strong> — Quantify the scale of API key exposure</li>
    <li class="zh"><strong>企业安全审计</strong> — 扫描你的组织仓库，确保没有 key 意外泄露</li>
    <li class="en"><strong>Org Auditing</strong> — Scan your organization's repos for accidental leaks</li>
    <li class="zh"><strong>漏洞赏金</strong> — 发现泄露 key 后通过 HackerOne/Bugcrowd 进行负责任披露</li>
    <li class="en"><strong>Bug Bounty</strong> — Find exposed keys for responsible disclosure programs</li>
    <li class="zh"><strong>安全意识教育</strong> — 用真实数据展示硬编码凭据的风险</li>
    <li class="en"><strong>Security Education</strong> — Show real-world consequences of hardcoded credentials</li>
  </ul>
</section>

<div class="forest-line">🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲</div>

<!-- Quick Start -->
<section>
  <h2 class="zh">🚀 快速开始</h2>
  <h2 class="en">🚀 Quick Start</h2>

  <pre><code>pip install aiohttp requests
gh auth login          # 可选，提升速率限制
python ultimate_scan.py  # 全量扫描</code></pre>

  <p class="zh">或者挑一个轻量的：</p>
  <p class="en">Or start light:</p>

  <table>
    <tr><th>脚本</th><th class="zh">说明</th><th class="en">Description</th><th class="zh">时长</th><th class="en">Duration</th></tr>
    <tr><td><code>ultimate_scan.py</code></td><td class="zh">全 5 阶段扫描</td><td class="en">Full 5-phase scan</td><td>10-14h</td></tr>
    <tr><td><code>expanded_scan.py</code></td><td class="zh">扩展多源扫描</td><td class="en">Expanded multi-source</td><td>3-5h</td></tr>
    <tr><td><code>max_scan.py</code></td><td class="zh">最大吞吐量</td><td class="en">Max throughput</td><td>2h</td></tr>
    <tr><td><code>deep_scan.py --hours 3</code></td><td class="zh">深度优化扫描</td><td class="en">Deep optimized</td><td class="zh">自定义</td><td class="en">Custom</td></tr>
    <tr><td><code>quick_batch.py</code></td><td class="zh">快速测试</td><td class="en">Quick test</td><td>15min</td></tr>
  </table>
</section>

<!-- Warning -->
<div class="warning-box">
  <h3>⚠️ Disclaimer / 免责声明</h3>
  <p class="zh">
    本工具仅用于<strong>授权的安全研究、渗透测试和凭据审计</strong>。使用本工具发现
    的 API Key 不应被用于未经授权的访问。作者不对任何滥用行为承担责任。
    如果你在扫描中发现了属于你的 key，请立即在 DeepSeek 平台轮换。
  </p>
  <p class="en">
    This tool is for <strong>authorized security research, penetration testing, and
    credential auditing only</strong>. Do not use discovered keys for unauthorized access.
    The authors assume no liability for misuse. If you discover your own key during a scan,
    rotate it immediately on the DeepSeek platform.
  </p>
</div>

<!-- Structure -->
<section>
  <h2 class="zh">📁 项目结构</h2>
  <h2 class="en">📁 Project Structure</h2>

  <pre><code>DarkForest-Hunter/
├── scanner_engine.py        # 核心引擎
├── scanners/
│   ├── base.py              # 基础扫描器
│   ├── github_gist.py       # Gist 扫描
│   ├── github_issues.py     # Issues/PRs 扫描
│   ├── github_commits.py    # 提交历史 + diff
│   ├── github_events.py     # 实时 PushEvent
│   ├── gitlab.py            # GitLab
│   ├── gitee.py             # Gitee (码云)
│   ├── huggingface.py       # HuggingFace
│   ├── pypi.py              # PyPI
│   ├── npm_registry.py      # npm
│   ├── stackoverflow.py     # Stack Overflow
│   ├── docker.py            # Docker Hub
│   ├── commoncrawl.py       # Common Crawl
│   └── wayback.py           # Wayback Machine
├── ultimate_scan.py         # 终极扫描脚本
├── queries_v4.txt           # 查询库
├── results/                 # 扫描结果
├── README.md                # 本文件
└── USAGE.md                 # 详细使用指南</code></pre>
</section>

<!-- License -->
<section>
  <h2 class="zh">📄 许可证</h2>
  <h2 class="en">📄 License</h2>
  <p>MIT License — 详见 <a href="LICENSE" style="color:var(--accent)">LICENSE</a></p>
</section>

<div class="forest-line">🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲</div>

<div class="footer">
  <p class="zh">
    "宇宙就是一座黑暗森林，每个文明都是带枪的猎人。" — 刘慈欣《三体II：黑暗森林》<br>
    愿善意的猎人率先抵达。
  </p>
  <p class="en">
    "The universe is a dark forest. Every civilization is an armed hunter." — Cixin Liu, <em>The Dark Forest</em><br>
    May the ethical hunters reach the prey first.
  </p>
</div>

</div>
</body>
</html>
