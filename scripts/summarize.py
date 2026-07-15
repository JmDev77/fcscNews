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
DELIM = '@@@FIELD@@@'  # 필드 구분자 (본문에 나올 가능성 거의 없는 패턴)

def summarize_with_claude(article, body):
    client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY',''))
    today  = datetime.now(KST).strftime('%-m.%-d.')
    lang   = article.get('lang', 'ko')

    prompt = f"""뉴스를 줄테니 아래 조건에 맞춰 정리해.

조건:
• 아스키 따옴표 → 스마트 따옴표(" ")로 변경
• 맞춤법 교정
• 한줄 요약 2개. 각각 ~돼, ~해, ~져 등으로 끝낼 것. 공백 포함 40자 이내로 반드시 지킬 것
• 날짜는 반드시 "월.일." 형식 (예: 6.21.). 없으면 오늘 {today} 사용. 연도 붙이지 말 것
• {"영문 기사이므로 제목과 본문 모두 한글로 번역할 것" if lang == 'en' else ""}
• body는 본문 전체를 문단 단위로 정리 (절대 줄이거나 요약하지 말고 전체 다 포함할 것)

입력:
제목: {article.get('title','')}
출처: {article.get('source','')}
날짜: {article.get('date','')}
본문: {body[:4000]}
URL: {article.get('url','')}

아래 형식 그대로 응답해. 마크다운이나 다른 설명 없이, 각 필드는 반드시 "{DELIM}필드명{DELIM}" 줄로 시작해서 구분할 것. body는 문단 사이에 빈 줄 하나씩 넣어서 여러 줄로 작성해도 됨 (다음 {DELIM} 구분자가 나올 때까지가 전부 그 필드 내용):

{DELIM}TITLE{DELIM}
(정리된 제목)
{DELIM}SOURCE{DELIM}
(출처)
{DELIM}DATE{DELIM}
(월.일. 형식 날짜)
{DELIM}SUMMARY1{DELIM}
(요약1, 40자 이내 한 줄)
{DELIM}SUMMARY2{DELIM}
(요약2, 40자 이내 한 줄)
{DELIM}BODY{DELIM}
(본문 전체, 문단마다 줄바꿈)
{DELIM}URL{DELIM}
(원문 URL)
{DELIM}END{DELIM}"""

    try:
        msg = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=8000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        text = msg.content[0].text.strip()
        result = parse_delimited(text)
        # 날짜 정규화
        d = result.get('date','')
        m = re.match(r'(\d{1,4})[.\/-](\d{1,2})[.\/-](\d{1,2})', d)
        if m:
            result['date'] = f"{int(m.group(2))}.{int(m.group(3))}."
        return result
    except Exception as e:
        print(f"  Claude 오류: {e}")
        return None

def parse_delimited(text):
    """@@@FIELD@@@ 구분자 기반 텍스트를 파싱 (따옴표/JSON 이스케이프 문제 없음)"""
    field_map = {
        'TITLE': 'title', 'SOURCE': 'source', 'DATE': 'date',
        'SUMMARY1': 'summary1', 'SUMMARY2': 'summary2',
        'BODY': 'body', 'URL': 'url',
    }
    parts = text.split(DELIM)
    result = {}
    current_key = None
    for part in parts:
        part_stripped = part.strip()
        if part_stripped in field_map:
            current_key = field_map[part_stripped]
        elif part_stripped == 'END':
            current_key = None
        elif current_key:
            result[current_key] = part.strip()
            current_key = None
    return result

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
