#!/usr/bin/env python3
"""
collect.py - 보안뉴스 RSS + 네이버 수집 → data/feeds.json 저장
제목 + desc + url만 저장 (요약 없음, 크롤링 없음 → 빠름)
"""

import os, json, re, time, hashlib
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup
import pytz
from dateutil.parser import parse as parse_date

# ── 설정 ──────────────────────────────────────────────
KST           = pytz.timezone('Asia/Seoul')
FEEDS_PATH    = Path(__file__).parent.parent / 'data' / 'feeds.json'
RETENTION_HRS = 72      # 72시간(3일) 이내 기사만 유지
MAX_ARTICLES  = 200     # 최대 보관 수

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'ko-KR,ko;q=0.9',
}

SIMILARITY_THRESH = 0.85
WORD_JACCARD_THRESH = 0.20

# ── RSS 피드 목록 ─────────────────────────────────────
FEEDS = [
    # 국내 보안뉴스
    {'url': 'http://www.boannews.com/media/news_rss.xml',           'source': '보안뉴스',      'group': '보안뉴스'},
    {'url': 'http://www.boannews.com/media/news_rss.xml?skind=5',   'source': '보안뉴스긴급',  'group': '보안뉴스'},
    {'url': 'https://www.dailysecu.com/rss/allArticle.xml',         'source': '데일리시큐',    'group': '보안뉴스'},
    {'url': 'https://rss.etnews.com/04045.xml',                     'source': '전자신문',      'group': '보안뉴스'},
    # KISA
    # KISA (실제 메뉴 경로: 보안공지=205020, 취약점정보=205023, 경보단계=205024)
    {'url': 'https://www.boho.or.kr/kr/rssList.do?menuNo=205020&bbsId=B0000133', 'source': 'KISA보안공지',  'group': 'KISA'},
    {'url': 'https://www.boho.or.kr/kr/rssList.do?menuNo=205023&bbsId=B0000302', 'source': 'KISA취약점',    'group': 'KISA'},
    {'url': 'https://www.boho.or.kr/kr/rssList.do?menuNo=205024&bbsId=B0000342', 'source': 'KISA경보',      'group': 'KISA'},
    # 취약점
    {'url': 'https://api.msrc.microsoft.com/update-guide/rss',     'source': 'MSRC',          'group': '취약점'},
    {'url': 'https://www.exploit-db.com/rss.xml',                  'source': 'Exploit-DB',    'group': '취약점'},
    {'url': 'https://www.cisa.gov/cybersecurity-advisories/all.xml','source': 'CISA',         'group': '취약점'},
    {'url': 'https://isc.sans.edu/rssfeed.xml',                    'source': 'SANS ISC',      'group': '취약점'},
    {'url': 'https://cvefeed.io/rssfeed/latest.xml',               'source': 'CVEFeed',       'group': '취약점'},
    # 해외
    {'url': 'https://feeds.feedburner.com/TheHackersNews',         'source': 'TheHackerNews', 'group': '해외'},
    {'url': 'https://www.bleepingcomputer.com/feed/',              'source': 'BleepingComputer','group': '해외'},
    {'url': 'https://krebsonsecurity.com/feed/',                   'source': 'KrebsOnSecurity','group': '해외'},
    {'url': 'https://www.darkreading.com/rss.xml',                 'source': 'DarkReading',   'group': '해외'},
    {'url': 'https://feeds.feedburner.com/securityweek',           'source': 'SecurityWeek',  'group': '해외'},
]

# 네이버 검색 키워드
NAVER_KEYWORDS = ['사이버보안', '정보보안', '해킹', '취약점', '랜섬웨어', '개인정보침해', '사이버공격']

# 보안 키워드 필터 (해외 피드 필터링용)
SECURITY_KEYWORDS = [
    '해킹', '북한', '유출', '개인정보', 'cve', '취약점', '디도스',
    '사이버', 'ddos', 'ransomware', 'malware', 'phishing', 'breach',
    'exploit', 'vulnerability', 'backdoor', 'zero-day', 'zero day',
    '악성코드', '랜섬웨어', '피싱', 'cybersecurity', 'hack', 'attack',
    'threat', 'security', 'intrusion', 'botnet',
]

# 자동 태그 분류
KEYWORD_TAGS = [
    {'kw': ['랜섬웨어','ransomware'],                                           'tag': '랜섬웨어',   'cls': 'tag-red'},
    {'kw': ['해킹','침해','공격','breach','hack','intrusion','attack'],          'tag': '해킹/침해',  'cls': 'tag-red'},
    {'kw': ['취약점','패치','CVE','제로데이','vulnerability','exploit','zero'],  'tag': '취약점/CVE', 'cls': 'tag-yellow'},
    {'kw': ['AI','인공지능','딥페이크','LLM','생성형','machine learning'],       'tag': 'AI 보안',    'cls': 'tag-blue'},
    {'kw': ['북한','라자루스','킴수키','APT','lazarus','kimsuky'],               'tag': '북한/APT',   'cls': 'tag-purple'},
    {'kw': ['정책','법','규정','시행','CISA','NIST','개보위','과기정통부'],      'tag': '정책/제도',  'cls': 'tag-green'},
    {'kw': ['피싱','스미싱','phishing','smishing','scam'],                       'tag': '피싱/사기',  'cls': 'tag-orange'},
    {'kw': ['DDoS','디도스','DoS','botnet','봇넷'],                              'tag': 'DDoS',       'cls': 'tag-red'},
    {'kw': ['접속장애','서비스 장애','먹통','오류','중단'],                      'tag': '가용성',     'cls': 'tag-orange'},
    {'kw': ['클라우드','cloud','AWS','Azure','GCP'],                             'tag': '클라우드',   'cls': 'tag-blue'},
    {'kw': ['malware','악성코드','trojan','spyware','worm'],                     'tag': '악성코드',   'cls': 'tag-red'},
]

def classify(title, desc=''):
    text = (title + ' ' + desc).lower()
    for x in KEYWORD_TAGS:
        if any(k.lower() in text for k in x['kw']):
            return x['tag'], x['cls']
    return '보안', 'tag-blue'

# 네이버 원문 도메인 → 매체명 매핑
DOMAIN_MEDIA = {
    'boannews.com':        '보안뉴스',
    'dailysecu.com':       '데일리시큐',
    'etnews.com':          '전자신문',
    'zdnet.co.kr':         '지디넷코리아',
    'edaily.co.kr':        '이데일리',
    'yna.co.kr':           '연합뉴스',
    'news1.kr':            '뉴스1',
    'newsis.com':          '뉴시스',
    'mk.co.kr':            '매일경제',
    'hankyung.com':        '한국경제',
    'chosun.com':          '조선일보',
    'donga.com':           '동아일보',
    'joongang.co.kr':      '중앙일보',
    'hani.co.kr':          '한겨레',
    'mt.co.kr':            '머니투데이',
    'sedaily.com':         '서울경제',
    'asiae.co.kr':         '아시아경제',
    'kbs.co.kr':           'KBS',
    'sbs.co.kr':           'SBS',
    'ytn.co.kr':           'YTN',
    'imbc.com':            'MBC',
    'boho.or.kr':          'KISA',
    'krcert.or.kr':        'KISA',
    'itworld.co.kr':       'ITWorld',
    'ciokorea.com':        'CIOKorea',
    'inews24.com':         '아이뉴스24',
    'ddaily.co.kr':        '디지털데일리',
}

def media_from_url(url):
    try:
        host = re.sub(r'^(www|m|n)\.', '', url.split('/')[2].lower())
        for domain, name in DOMAIN_MEDIA.items():
            if domain in host:
                return name
        if 'naver.com' in host:
            return '네이버뉴스'
        # 매핑 없으면 도메인 자체를 보기 좋게 표시
        return host.split('.')[0] if host else '네이버뉴스'
    except Exception:
        return '네이버뉴스'

def make_id(url, title=''):
    return hashlib.md5((url or title).encode()).hexdigest()[:12]

def clean_html(text):
    if not text: return ''
    if '<' not in text: return text.strip()
    return BeautifulSoup(text, 'html.parser').get_text().strip()

def fmt_date(dt):
    if not dt: return datetime.now(KST).strftime('%-m.%-d.')
    try:
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        return dt.astimezone(KST).strftime('%-m.%-d.')
    except:
        return datetime.now(KST).strftime('%-m.%-d.')

def parse_dt(s):
    if not s: return datetime.now(KST)
    try:
        dt = parse_date(s)
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        return dt.astimezone(KST)
    except:
        return datetime.now(KST)

def word_jaccard(t1, t2):
    def words(t): return set(re.sub('[^가-힣a-zA-Z0-9]', ' ', t).split())
    w1, w2 = words(t1), words(t2)
    if not w1 or not w2: return 0.0
    return len(w1 & w2) / len(w1 | w2)

def is_similar(t1, t2):
    ratio = SequenceMatcher(None, t1, t2).ratio()
    if ratio >= SIMILARITY_THRESH: return True
    if ratio >= 0.45 and word_jaccard(t1, t2) >= WORD_JACCARD_THRESH: return True
    return False

def is_security_related(title, desc=''):
    text = (title + ' ' + desc).lower()
    return any(k in text for k in SECURITY_KEYWORDS)

# ── KISA 전용 HTML 목록 크롤링 (RSS 불확실 대비 폴백) ──
KISA_BOARDS = [
    {'menuNo': '205020', 'bbsId': 'B0000133', 'source': 'KISA보안공지'},
    {'menuNo': '205023', 'bbsId': 'B0000302', 'source': 'KISA취약점'},
    {'menuNo': '205024', 'bbsId': 'B0000342', 'source': 'KISA경보'},
]

def fetch_kisa_html(board):
    """RSS가 안 되는 경우를 대비해 게시판 목록 페이지를 직접 파싱"""
    url = f"https://www.boho.or.kr/kr/bbs/list.do?menuNo={board['menuNo']}&bbsId={board['bbsId']}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, 'lxml')
        items = []

        # 목록 테이블/리스트에서 상세 링크(view.do + nttId) 추출
        links = soup.select('a[href*="view.do"][href*="nttId"]')
        seen = set()
        for a in links:
            href = a.get('href', '')
            m = re.search(r'nttId=(\d+)', href)
            if not m: continue
            ntt_id = m.group(1)
            if ntt_id in seen: continue
            seen.add(ntt_id)

            title = a.get_text(strip=True)
            if not title or len(title) < 5: continue

            full_url = f"https://www.boho.or.kr/kr/bbs/view.do?bbsId={board['bbsId']}&menuNo={board['menuNo']}&nttId={ntt_id}"
            dt = datetime.now(KST)  # 목록에서 날짜 파싱 어려우면 오늘로 처리 (최신글 위주라 무방)

            tag, cls = classify(title)
            items.append({
                'id':      make_id(full_url, title),
                'title':   title,
                'desc':    '',
                'url':     full_url,
                'date':    fmt_date(dt),
                'rawDate': dt.isoformat(),
                'source':  board['source'],
                'group':   'KISA',
                'tag':     tag,
                'tagCls':  cls,
                'lang':    'ko',
            })
            if len(items) >= 20: break

        print(f"  ✅ {board['source']} (HTML): {len(items)}건")
        return items
    except Exception as e:
        print(f"  ❌ {board['source']} (HTML): {e}")
        return []

# ── RSS 수집 ──────────────────────────────────────────
def fetch_rss(feed):
    try:
        d = feedparser.parse(feed['url'],
            request_headers=HEADERS,
            agent=HEADERS['User-Agent'])

        # 진단: 파싱 실패나 HTTP 오류 시 원인 로그
        status = getattr(d, 'status', None)
        if d.get('bozo') and not d.entries:
            reason = d.get('bozo_exception', '알 수 없음')
            print(f"  ⚠️ {feed['source']}: 파싱 오류 (status={status}) - {reason}")
        elif not d.entries:
            print(f"  ⚠️ {feed['source']}: entries 없음 (status={status})")

        items = []
        cutoff = datetime.now(KST) - timedelta(hours=RETENTION_HRS)
        for e in d.entries[:30]:
            title = clean_html(e.get('title',''))
            link  = e.get('link') or e.get('id','')
            desc  = clean_html(e.get('summary') or e.get('description',''))[:400]
            pub   = e.get('published') or e.get('updated','')
            dt    = parse_dt(pub)
            if dt < cutoff: continue
            if feed['group'] == '해외' and not is_security_related(title, desc): continue
            tag, cls = classify(title, desc)
            items.append({
                'id':      make_id(link, title),
                'title':   title,
                'desc':    desc,
                'url':     link,
                'date':    fmt_date(dt),
                'rawDate': dt.isoformat(),
                'source':  feed['source'],
                'group':   feed['group'],
                'tag':     tag,
                'tagCls':  cls,
                'lang':    'en' if feed['group'] == '해외' else 'ko',
            })
        print(f"  ✅ {feed['source']}: {len(items)}건")
        return items
    except Exception as e:
        print(f"  ❌ {feed['source']}: {e}")
        return []

# ── 네이버 API ────────────────────────────────────────
def fetch_naver(keyword):
    cid = os.environ.get('NAVER_CLIENT_ID','')
    sec = os.environ.get('NAVER_CLIENT_SECRET','')
    if not cid: return []
    try:
        r = requests.get(
            'https://openapi.naver.com/v1/search/news.json',
            params={'query': keyword, 'display': 50, 'sort': 'date'},
            headers={**HEADERS, 'X-Naver-Client-Id': cid, 'X-Naver-Client-Secret': sec},
            timeout=10
        )
        r.raise_for_status()
        items = []
        cutoff = datetime.now(KST) - timedelta(hours=RETENTION_HRS)
        for i in r.json().get('items', []):
            title = clean_html(i.get('title',''))
            link  = i.get('originallink') or i.get('link','')
            desc  = clean_html(i.get('description',''))[:400]
            dt    = parse_dt(i.get('pubDate',''))
            if dt < cutoff: continue
            tag, cls = classify(title, desc)
            source = media_from_url(link) if link else '네이버뉴스'
            items.append({
                'id':      make_id(link, title),
                'title':   title,
                'desc':    desc,
                'url':     link,
                'date':    fmt_date(dt),
                'rawDate': dt.isoformat(),
                'source':  source,
                'group':   '네이버',
                'tag':     tag,
                'tagCls':  cls,
                'lang':    'ko',
            })
        return items
    except Exception as e:
        print(f"  ❌ 네이버[{keyword}]: {e}")
        return []

# ── 메인 ──────────────────────────────────────────────
def main():
    print(f"\n{'='*50}")
    print(f"수집 시작: {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST")
    print(f"{'='*50}")

    # 기존 feeds.json 로드 (있으면 병합)
    all_items = {}
    cutoff = datetime.now(KST) - timedelta(hours=RETENTION_HRS)
    if FEEDS_PATH.exists():
        try:
            existing = json.loads(FEEDS_PATH.read_text(encoding='utf-8'))
            for a in existing.get('articles', []):
                try:
                    dt = datetime.fromisoformat(a.get('rawDate',''))
                    if dt.tzinfo is None:
                        dt = pytz.utc.localize(dt)
                    if dt.astimezone(KST) >= cutoff:
                        all_items[a['id']] = a
                except:
                    pass
            print(f"[기존 데이터] {len(all_items)}건 로드")
        except Exception as e:
            print(f"[기존 데이터] 로드 실패: {e}")

    # RSS 수집
    print("\n[RSS 수집]")
    for feed in FEEDS:
        rss_items = fetch_rss(feed)
        for item in rss_items:
            all_items[item['id']] = item
        # KISA RSS가 비어있으면 HTML 목록 크롤링으로 폴백
        if not rss_items and feed['group'] == 'KISA':
            board = next((b for b in KISA_BOARDS if b['source'] == feed['source']), None)
            if board:
                for item in fetch_kisa_html(board):
                    all_items[item['id']] = item
        time.sleep(0.3)

    # 네이버 수집
    print("\n[네이버 수집]")
    for kw in NAVER_KEYWORDS:
        for item in fetch_naver(kw):
            if item['id'] not in all_items:
                all_items[item['id']] = item
        time.sleep(0.3)

    # 제목 유사도 기반 중복 제거
    print("\n[중복 제거]")
    deduped = []
    seen_titles = []
    for item in sorted(all_items.values(), key=lambda x: x.get('rawDate',''), reverse=True):
        if any(is_similar(item['title'], t) for t in seen_titles):
            continue
        seen_titles.append(item['title'])
        deduped.append(item)

    # 최신순 정렬 + 최대 개수 제한
    deduped = deduped[:MAX_ARTICLES]

    # 저장
    FEEDS_PATH.parent.mkdir(exist_ok=True)
    output = {
        'fetched_at': datetime.now(KST).isoformat(),
        'count': len(deduped),
        'articles': deduped,
    }
    FEEDS_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"\n✅ feeds.json 저장: {len(deduped)}건")
    print(f"{'='*50}\n")

if __name__ == '__main__':
    main()
