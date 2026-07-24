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

# ── 크롤링 (다층 방어 로직) ───────────────────────────
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
]

# 본문이 아닌 UI/광고/네비게이션으로 판단할 문구 패턴
UI_JUNK = re.compile(
    r'^(hi,?\s*what are you looking for|chat with us|accept all cookies|'
    r'subscribe to (our )?newsletter|sign up for|cookie(s)? (policy|settings)|'
    r'we use cookies|click here to|share this article|read more:?|'
    r'advertisement|sponsored|related articles?|most popular|'
    r'\uad6c\ub3c5\ud558\uae30|\ub85c\uadf8\uc778|\ud68c\uc6d0\uac00\uc785|\uad11\uace0|\ubb34\ub2e8\uc804\uc7ac|\uc800\uc791\uad8c\uc790)',
    re.I
)

# 기자 서명 접두어 패턴
LOC_PREFIX  = re.compile(r'^[\(\[][^)\]]{1,25}[\)\]]\s*')
BYLINE_NAME = re.compile(r'^[\uac00-\ud7a3]{2,4}\s*\uae30\uc790\s*=?\s*')

def clean_para(p):
    """문단 정제: 기자서명 접두어 제거, UI 문구면 버림"""
    p = p.strip()
    if not p: return None
    if UI_JUNK.match(p): return None
    if re.search(r'copyright|\uc800\uc791\uad8c|\ubb34\ub2e8\uc804\uc7ac|\u24d2', p, re.I): return None
    p = LOC_PREFIX.sub('', p)
    p = BYLINE_NAME.sub('', p)
    return p if len(p) >= 15 else None

def extract_paras(element):
    """요소에서 문단 리스트 추출 (<p> 우선, 없으면 줄바꿈 분리)"""
    paras = [p.get_text(strip=True) for p in element.find_all('p')]
    if not paras:
        paras = [ln.strip() for ln in element.get_text('\n', strip=True).split('\n')]
    out = []
    for p in paras:
        c = clean_para(p)
        if c: out.append(c)
    return out

def try_jsonld(soup):
    """1단계: JSON-LD 구조화 데이터에서 articleBody 추출 (가장 정확)"""
    for tag in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(tag.string or '{}')
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        # @graph 안에 들어있는 경우도 처리
        for c in list(candidates):
            if isinstance(c, dict) and '@graph' in c:
                candidates.extend(c['@graph'] if isinstance(c['@graph'], list) else [c['@graph']])
        for c in candidates:
            if not isinstance(c, dict): continue
            body = c.get('articleBody')
            if body and isinstance(body, str) and len(body) > 100:
                return body
    return ''

def try_density(soup):
    """3단계: 텍스트 밀도 분석으로 본문 영역 자동 탐지 (처음 보는 사이트 대응)
    - <p> 태그가 가장 많이 모여있고 텍스트량이 많은 컨테이너를 본문으로 판단"""
    best_el, best_score = None, 0
    for el in soup.find_all(['div','section','article','main']):
        ps = el.find_all('p', recursive=True)
        if len(ps) < 3: continue
        text_len = sum(len(p.get_text(strip=True)) for p in ps)
        if text_len < 150: continue
        # 링크가 과하게 많으면 관련기사 목록일 가능성 -> 감점
        link_len = sum(len(a.get_text(strip=True)) for a in el.find_all('a'))
        link_ratio = link_len / max(text_len, 1)
        if link_ratio > 0.5: continue
        # 점수: 텍스트량 * 문단수 보정, 링크 비율만큼 감점
        score = text_len * (1 - link_ratio)
        # 중첩된 컨테이너 중 더 좁고 밀도 높은 쪽 선호
        if score > best_score:
            best_score, best_el = score, el
    return best_el

def crawl(url):
    if not url or not url.startswith('http'):
        return ''

    html = None
    # 재시도 + User-Agent 로테이션 (봇 차단 대응)
    for attempt, ua in enumerate(USER_AGENTS):
        try:
            time.sleep(1.0 if attempt == 0 else 2.0)
            headers = {**HEADERS, 'User-Agent': ua}
            r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
            if r.status_code == 200:
                enc = r.apparent_encoding or 'utf-8'
                html = r.content.decode(enc, errors='ignore')
                break
            print(f"  HTTP {r.status_code} (시도 {attempt+1}/{len(USER_AGENTS)}): {url}")
        except Exception as e:
            print(f"  요청 실패 (시도 {attempt+1}/{len(USER_AGENTS)}): {e}")

    if not html:
        return ''

    try:
        soup = BeautifulSoup(html, 'lxml')

        # 1단계: JSON-LD 구조화 데이터 (본문을 명시적으로 제공하는 사이트)
        jsonld_body = try_jsonld(soup)
        if jsonld_body:
            paras = [clean_para(p) for p in jsonld_body.split('\n')]
            paras = [p for p in paras if p]
            text = '\n'.join(paras)
            if len(text) > 100:
                print(f"  → JSON-LD로 추출 ({len(text)}자)")
                return clean_body(text)

        # 잡음 태그 제거 (챗봇/광고/네비게이션/관련기사)
        for tag in ['script','style','nav','header','footer','aside','iframe','figure','figcaption','noscript','form','button']:
            for t in soup.find_all(tag): t.decompose()
        for t in soup.find_all(class_=re.compile(r'ad|banner|related|share|sns|reporter|copyright|chat|widget|cookie|newsletter|comment|recommend|popular|sidebar', re.I)):
            t.decompose()
        for t in soup.find_all(id=re.compile(r'chat|widget|cookie|intercom|drift|zendesk|comment|sidebar|related', re.I)):
            t.decompose()

        candidates = []  # (텍스트, 출처설명)

        # 2단계: 알려진 셀렉터
        GENERIC_SELECTORS = {'article', '.article_view', '.article-body', '.article_body'}
        for sel in BODY_SELECTORS:
            el = soup.select_one(sel)
            if not el: continue
            paras = extract_paras(el)
            text = '\n'.join(paras)
            threshold = 500 if sel in GENERIC_SELECTORS else 150
            if len(text) > threshold:
                print(f"  → 셀렉터 '{sel}'로 추출 ({len(text)}자)")
                return clean_body(text)
            if text:
                candidates.append((text, f"셀렉터 {sel}"))

        # 3단계: 텍스트 밀도 분석 (처음 보는 사이트 자동 대응)
        dense_el = try_density(soup)
        if dense_el is not None:
            paras = extract_paras(dense_el)
            text = '\n'.join(paras)
            if len(text) > 150:
                print(f"  → 밀도분석으로 추출 ({len(text)}자)")
                return clean_body(text)
            if text:
                candidates.append((text, "밀도분석"))

        # 4단계: <p> 태그 전체 수집 (최후 폴백)
        paras = []
        for p in soup.find_all('p'):
            c = clean_para(p.get_text(strip=True))
            if c and len(c) >= 25:
                paras.append(c)
        if paras:
            candidates.append(('\n'.join(paras), "p태그 전체"))

        # 후보 중 가장 긴 것 채택
        if candidates:
            text, how = max(candidates, key=lambda x: len(x[0]))
            if len(text) >= 100:
                print(f"  → {how}로 추출 ({len(text)}자)")
                return clean_body(text)

        print("  ⚠️ 본문 추출 실패 (모든 방법 시도함)")
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
