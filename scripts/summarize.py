#!/usr/bin/env python3
"""
summarize.py - 선택한 기사 원문 크롤링 + Claude 요약
workflow_dispatch로 실행, 결과를 data/result_{request_id}.json 저장
"""

import os, json, re, time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import pytz
import anthropic

KST       = pytz.timezone('Asia/Seoul')
DATA_DIR  = Path(__file__).parent.parent / 'data'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'ko-KR,ko;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Referer': 'https://www.google.com/',
}

BODY_SELECTORS = [
    '#dic_area',                    # 네이버뉴스
    '#articeBody',                  # 네이버뉴스 구버전
    '#news_content',                # 보안뉴스
    '#article-view-content-div',    # 데일리시큐 등
    '[itemprop="articleBody"]',     # schema.org (매경 등)
    '.article_view',
    '.article-body',
    '.article_body',
    '.news_view',
    '.news-content',
    '.news_cnt_detail_wrap',
    '.art_txt',
    'article',
]

# ── 크롤링 ────────────────────────────────────────────
def crawl(url):
    if not url or not url.startswith('http'):
        return ''
    try:
        time.sleep(1.0)
        r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
        if r.status_code != 200:
            print(f"  HTTP {r.status_code}: {url}")
            return ''

        enc  = r.apparent_encoding or 'utf-8'
        html = r.content.decode(enc, errors='ignore')
        soup = BeautifulSoup(html, 'lxml')

        # 불필요한 태그 제거
        for tag in ['script','style','nav','header','footer','aside','iframe','figure','figcaption','noscript']:
            for t in soup.find_all(tag): t.decompose()
        for t in soup.find_all(class_=re.compile(r'ad|banner|related|share|sns|reporter|copyright', re.I)):
            t.decompose()

        # 셀렉터 순서대로 시도
        for sel in BODY_SELECTORS:
            el = soup.select_one(sel)
            if el:
                text = el.get_text('\n', strip=True)
                if len(text) > 150:
                    return clean_body(text)

        # fallback: <p> 태그 수집
        paras = [p.get_text(strip=True) for p in soup.find_all('p')]
        paras = [p for p in paras if len(p) >= 25
                 and not re.search(r'copyright|저작권|무단전재|ⓒ|기자\s*=', p, re.I)]
        if paras:
            return clean_body('\n'.join(paras))

        return ''
    except Exception as e:
        print(f"  크롤링 오류: {e}")
        return ''

def clean_body(text):
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n', text)
    return text.strip()[:5000]

# ── Claude 요약 ───────────────────────────────────────
def summarize_with_claude(article, body):
    client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY',''))
    today  = datetime.now(KST).strftime('%-m.%-d.')
    lang   = article.get('lang', 'ko')

    prompt = f"""뉴스를 줄테니 아래 조건에 맞춰 정리해.

조건:
• 아스키 따옴표 → 스마트 따옴표로 변경
• 맞춤법 교정
• 한줄 요약 2개. 각각 ~돼, ~해, ~져 등으로 끝낼 것. 공백 포함 40자 이내로 반드시 지킬 것
• 날짜는 반드시 "월.일." 형식 (예: 6.21.). 없으면 오늘 {today} 사용. 연도 붙이지 말 것
• {"영문 기사이므로 제목과 본문 모두 한글로 번역할 것" if lang == 'en' else ""}
• body는 본문 전체를 문단 단위로 정리 (줄이거나 요약하지 말 것). 문단 구분은 \\n으로

입력:
제목: {article.get('title','')}
출처: {article.get('source','')}
날짜: {article.get('date','')}
본문: {body[:4000]}
URL: {article.get('url','')}

JSON 형식으로만 응답 (마크다운 없이):
{{"title":"제목","source":"출처","date":"날짜","summary1":"요약1","summary2":"요약2","body":"본문(\\n구분)","url":"URL"}}"""

    try:
        msg = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=8000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        text = msg.content[0].text.strip()
        # JSON 파싱
        text = re.sub(r'```json|```', '', text).strip()
        result = json.loads(text)
        # 날짜 정규화
        d = result.get('date','')
        m = re.match(r'(\d{1,4})[.\/-](\d{1,2})[.\/-](\d{1,2})', d)
        if m:
            result['date'] = f"{int(m.group(2))}.{int(m.group(3))}."
        return result
    except json.JSONDecodeError:
        # 정규식 fallback
        result = {}
        for field in ['title','source','date','summary1','summary2','body','url']:
            m = re.search(f'"{field}"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"', text)
            result[field] = m.group(1) if m else ''
        return result
    except Exception as e:
        print(f"  Claude 오류: {e}")
        return None

# ── 메인 ──────────────────────────────────────────────
def main():
    articles_json = os.environ.get('ARTICLES', '[]')
    request_id    = os.environ.get('REQUEST_ID', 'default')

    try:
        articles = json.loads(articles_json)
    except:
        print("❌ ARTICLES JSON 파싱 실패")
        return

    print(f"\n{'='*50}")
    print(f"요약 시작: {len(articles)}건 | request_id: {request_id}")
    print(f"{'='*50}")

    results = []
    for i, article in enumerate(articles[:5]):
        print(f"\n[{i+1}/{len(articles)}] {article.get('title','')[:50]}")

        # 1. 원문 크롤링
        print(f"  → 크롤링 중: {article.get('url','')}")
        body = crawl(article.get('url',''))
        if body:
            print(f"  ✅ 크롤링 성공 ({len(body)}자)")
        else:
            body = article.get('desc','')
            print(f"  ⚠️ 크롤링 실패 — desc 사용 ({len(body)}자)")

        # 2. Claude 요약
        print(f"  → Claude 요약 중...")
        result = summarize_with_claude(article, body)

        if result:
            print(f"  ✅ 요약 완료")
            results.append({
                'no':       str(i + 1),
                'title':    result.get('title')    or article.get('title',''),
                'source':   result.get('source')   or article.get('source',''),
                'date':     result.get('date')      or article.get('date',''),
                'summary1': result.get('summary1',''),
                'summary2': result.get('summary2',''),
                'body':     result.get('body')      or body,
                'url':      result.get('url')       or article.get('url',''),
                'tag':      article.get('tag','보안'),
                'tagCls':   article.get('tagCls','tag-blue'),
            })
        else:
            print(f"  ❌ 요약 실패 — 원본 사용")
            results.append({
                'no':       str(i + 1),
                'title':    article.get('title',''),
                'source':   article.get('source',''),
                'date':     article.get('date',''),
                'summary1': '',
                'summary2': '',
                'body':     body,
                'url':      article.get('url',''),
                'tag':      article.get('tag','보안'),
                'tagCls':   article.get('tagCls','tag-blue'),
            })

    # 결과 저장
    DATA_DIR.mkdir(exist_ok=True)
    output = {
        'request_id': request_id,
        'created_at': datetime.now(KST).isoformat(),
        'status':     'done',
        'articles':   results,
    }
    out_path = DATA_DIR / f'result_{request_id}.json'
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"\n✅ result_{request_id}.json 저장 완료")
    print(f"{'='*50}\n")

if __name__ == '__main__':
    main()
