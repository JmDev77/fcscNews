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

        # 셀렉터 순서대로 시도 - 신뢰도 높은 특정 셀렉터는 150자 기준, 
        # 범용 셀렉터(article 등)는 관련기사/광고 등이 섞여 짧게 잡힐 위험이 있어 더 높은 기준 적용
        GENERIC_SELECTORS = {'article', '.article_view', '.article-body', '.article_body'}
        best_text = ''
        for sel in BODY_SELECTORS:
            el = soup.select_one(sel)
            if el:
                text = el.get_text('\n', strip=True)
                threshold = 500 if sel in GENERIC_SELECTORS else 150
                if len(text) > threshold:
                    return clean_body(text)
                if len(text) > len(best_text):
                    best_text = text

        # fallback: <p> 태그 수집
        raw_paras = [p.get_text(strip=True) for p in soup.find_all('p')]

        # 국내 통신사 특유의 기자 서명 접두어만 제거 (본문 자체는 유지)
        # 1단계: "(서울=뉴스1)" "[헤럴드경제=문혜현 기자]" 같은 괄호 표기 제거
        # 2단계: 남은 "김민수 기자 = " 같은 기자명 접두어 제거
        LOC_PREFIX = re.compile(r'^[\(\[][^)\]]{1,25}[\)\]]\s*')
        BYLINE_NAME = re.compile(r'^[가-힣]{2,4}\s*기자\s*=?\s*')

        paras = []
        for p in raw_paras:
            if len(p) < 25:
                continue  # 너무 짧은 줄(순수 서명, 이메일 등)은 제외
            if re.search(r'copyright|저작권|무단전재|ⓒ', p, re.I):
                continue
            p = LOC_PREFIX.sub('', p)
            p = BYLINE_NAME.sub('', p)
            if p:
                paras.append(p)

        p_tag_text = '\n'.join(paras) if paras else ''

        # 셀렉터로 찾은 것(기준 미달)과 <p> 태그 수집 결과 중 더 긴 쪽 채택
        if len(p_tag_text) > len(best_text):
            best_text = p_tag_text

        if best_text:
            return clean_body(best_text)

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

    prompt = f"""뉴스 기사를 정리해줘. 아래 조건을 반드시 지켜.

조건:
• 아스키 따옴표 → 스마트 따옴표(" ")로 변경
• 맞춤법만 교정 (문장 재구성 금지)
• 한줄 요약 2개. 각각 ~돼, ~해, ~져 등으로 끝낼 것. 공백 포함 40자 이내로 반드시 지킬 것
• 날짜는 반드시 "월.일." 형식 (예: 6.21.). 없으면 오늘 {today} 사용. 연도 붙이지 말 것
• {"영문 기사이므로 제목과 본문 모두 한글로 번역할 것" if lang == 'en' else ""}

body 필드 작성 시 반드시 지킬 것 (매우 중요):
• 아래 "본문" 전체를 처음부터 끝까지 한 글자도 빠짐없이 그대로 옮겨 적을 것
• 요약하거나, 축약하거나, 일부만 골라 쓰거나, "..." 같은 생략 표시를 쓰는 것 절대 금지
• 문장을 재작성하거나 순서를 바꾸지 말 것. 오직 맞춤법/따옴표만 교정
• 본문의 첫 문장부터 시작할 것 (중간부터 시작하면 안 됨)
• 문단 구분은 원문 그대로 유지 (문단 사이 빈 줄 하나)

입력:
제목: {article.get('title','')}
출처: {article.get('source','')}
날짜: {article.get('date','')}
본문:
{body[:8000]}
URL: {article.get('url','')}

아래 형식 그대로 응답해. 마크다운이나 다른 설명 없이, 각 필드는 반드시 "{DELIM}필드명{DELIM}" 줄로 시작해서 구분할 것:

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
(본문 전체를 처음부터 끝까지 그대로, 문단마다 줄바꿈. 절대 생략하지 말 것)
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

        # 안전장치: Claude가 body를 축약/생략했는지 검증
        # 원본 크롤링 본문 대비 결과 body가 비정상적으로 짧으면(60% 미만) 원본으로 대체
        result_body = result.get('body', '')
        orig_len = len(body[:8000])
        if orig_len > 200 and len(result_body) < orig_len * 0.6:
            print(f"  ⚠️ body 축약 감지 (원본 {orig_len}자 → 결과 {len(result_body)}자) — 원본 크롤링 본문 사용")
            result['body'] = body[:8000]

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
