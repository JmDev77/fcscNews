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
MAX_ARTICLES  = 500     # 최대 보관 수

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'ko-KR,ko;q=0.9',
}

SIMILARITY_THRESH = 0.85
WORD_JACCARD_THRESH = 0.20

# ── RSS 피드 목록 ─────────────────────────────────────
FEEDS = [
    # ── 자체개발 · 국내 ────────────────────────────────
    {'url': 'https://www.boannews.com/media/news_rss.xml',          'source': '보안뉴스',      'channel': '자체개발', 'region': '국내'},
    {'url': 'https://www.dailysecu.com/rss/allArticle.xml',         'source': '데일리시큐',    'channel': '자체개발', 'region': '국내'},
    # ── 자체개발 · 해외 ────────────────────────────────
    {'url': 'https://feeds.feedburner.com/TheHackersNews',         'source': 'TheHackerNews', 'channel': '자체개발', 'region': '해외'},
    {'url': 'https://www.bleepingcomputer.com/feed/',              'source': 'BleepingComputer','channel': '자체개발', 'region': '해외'},
    {'url': 'https://krebsonsecurity.com/feed/',                   'source': 'KrebsOnSecurity','channel': '자체개발', 'region': '해외'},
    {'url': 'https://www.darkreading.com/rss.xml',                 'source': 'DarkReading',   'channel': '자체개발', 'region': '해외'},
    {'url': 'https://feeds.feedburner.com/securityweek',           'source': 'SecurityWeek',  'channel': '자체개발', 'region': '해외'},
    # ── 쉴더스 (RSS 안에 국내/해외 매체가 섞여있어 링크 도메인으로 지역 판별) ──
    {'url': 'https://security-brief.pages.dev/rss.xml',            'source': '쉴더스',        'channel': '쉴더스',   'region': None},
]

# 네이버 검색 키워드 (해킹/보안/사이버 중심) — 자체개발 · 국내로 분류
NAVER_KEYWORDS = ['해킹', '보안', '사이버', '랜섬웨어', '개인정보침해',
                  '킴수키', '라자루스', '샤이니헌터스', '실버폭스']

# 네이버 검색 결과 중 보안뉴스와 무관한 문맥(드라마/영화/게임/연예 등) 제외용 키워드
EXCLUDE_KEYWORDS = [
    '드라마', '영화', '예능', '웹툰', '배우', '출연', '방영', '시청률',
    '넷플릭스', '디즈니+', '티빙 오리지널', '왓챠',
    '게임', '스팀', 'PC방', '콘솔', '플레이스테이션', '닌텐도',
    '뮤직비디오', '아이돌', '컴백', '음원', '가수', '콘서트',
    '주식 종목', '테마주', '급등', '상한가',
    '[부고]', '[동정]', '[인사]', '[신간]', '[축사]',
]

# 보안 키워드 필터 (해외 피드 필터링용 + 기관 웹훅 매칭용)
SECURITY_KEYWORDS = [
    '해킹', '북한', '유출', '개인정보', 'cve', '취약점', '디도스',
    '사이버', 'ddos', 'ransomware', 'malware', 'phishing', 'breach',
    'exploit', 'vulnerability', 'backdoor', 'zero-day', 'zero day',
    '악성코드', '랜섬웨어', '피싱', 'cybersecurity', 'hack', 'attack',
    'threat', 'security', 'intrusion', 'botnet',
    '침해', '공격', '랜섬', '보안', '정보유출', '스미싱', '디지털포렌식',
    '악성', '백도어', '제로데이', '정보보호', '사칭', '스팸',
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
    # 해외 보안 매체
    'thehackernews.com':   'TheHackerNews',
    'bleepingcomputer.com':'BleepingComputer',
    'krebsonsecurity.com': 'KrebsOnSecurity',
    'darkreading.com':     'DarkReading',
    'securityweek.com':    'SecurityWeek',
    'securityaffairs.co':  'SecurityAffairs',
    'securityaffairs.com': 'SecurityAffairs',
    'theregister.com':     'TheRegister',
    'therecord.media':     'TheRecord',
    'malwarebytes.com':    'Malwarebytes',
    'databreaches.net':    'DataBreaches.net',
    'horizon3.ai':         'Horizon3.ai',
    'wired.com':           'Wired',
    'arstechnica.com':     'ArsTechnica',
    'zdnet.com':           'ZDNet',
    'threatpost.com':      'ThreatPost',
    'cyberscoop.com':      'CyberScoop',
    'infosecurity-magazine.com': 'InfosecurityMagazine',
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

# 국내 매체로 분류할 도메인 목록 (DOMAIN_MEDIA 중 .kr / 국내 언론사)
KOREAN_DOMAINS = {
    'boannews.com','dailysecu.com','etnews.com','zdnet.co.kr','edaily.co.kr',
    'yna.co.kr','news1.kr','newsis.com','mk.co.kr','hankyung.com','chosun.com',
    'donga.com','joongang.co.kr','hani.co.kr','mt.co.kr','sedaily.com',
    'asiae.co.kr','kbs.co.kr','sbs.co.kr','ytn.co.kr','imbc.com','boho.or.kr',
    'krcert.or.kr','itworld.co.kr','ciokorea.com','inews24.com','ddaily.co.kr',
    'naver.com',
}

def region_from_url(url):
    """링크 도메인으로 국내/해외 판별 (쉴더스처럼 매체가 섞인 브리핑 피드용)"""
    try:
        host = re.sub(r'^(www|m|n)\.', '', url.split('/')[2].lower())
        if host.endswith('.kr') or any(d in host for d in KOREAN_DOMAINS):
            return '국내'
        return '해외'
    except Exception:
        return '국내'

def extract_source_outlets(description_html):
    """description(HTML) 안의 <a href> 링크들에서 매체명을 추출 (쉴더스처럼 여러 매체를 함께 링크하는 브리핑용)"""
    if not description_html:
        return []
    try:
        soup = BeautifulSoup(description_html, 'lxml')
        outlets = []
        seen = set()
        for a in soup.find_all('a', href=True):
            name = media_from_url(a['href'])
            if name and name not in seen:
                seen.add(name)
                outlets.append(name)
        return outlets
    except Exception:
        return []

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

def fmt_time(dt):
    """카드 UI 표시용: 월.일. HH:MM (hwpx 요약표에는 쓰지 않음, date 필드와 별개)"""
    if not dt: return ''
    try:
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        return dt.astimezone(KST).strftime('%-m.%-d. %H:%M')
    except:
        return ''

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

# ── RSS 수집 ──────────────────────────────────────────
def fetch_rss(feed):
    try:
        # feedparser.parse(url)는 자체 타임아웃이 없어 응답 없는 서버에 무한 대기할 수 있음
        # requests로 명시적 타임아웃을 걸고 받은 내용을 feedparser에 넘김
        r = requests.get(feed['url'], headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"  ❌ {feed['source']}: HTTP {r.status_code}")
            return []
        d = feedparser.parse(r.content)

        # 진단: 파싱 실패나 HTTP 오류 시 원인 로그
        if d.get('bozo') and not d.entries:
            reason = d.get('bozo_exception', '알 수 없음')
            print(f"  ⚠️ {feed['source']}: 파싱 오류 - {reason}")
        elif not d.entries:
            print(f"  ⚠️ {feed['source']}: entries 없음")

        items = []
        cutoff = datetime.now(KST) - timedelta(hours=RETENTION_HRS)
        for e in d.entries[:30]:
            title    = clean_html(e.get('title',''))
            link     = e.get('link') or e.get('id','')
            raw_desc = e.get('summary') or e.get('description','')
            desc     = clean_html(raw_desc)[:400]
            pub      = e.get('published') or e.get('updated','')
            dt       = parse_dt(pub)
            if dt < cutoff: continue
            # 부고/동정/인사 등 공지성 기사, 무관 카테고리 기사 제외
            combined = title + ' ' + desc
            if any(kw in combined for kw in EXCLUDE_KEYWORDS):
                continue

            # region 결정: FEEDS에 명시돼 있으면 그대로, 없으면(쉴더스) 링크 도메인으로 판별
            region = feed.get('region') or (region_from_url(link) if link else '국내')

            # 국내 종합성 피드(데일리시큐 등)와 해외 지역 기사는 보안 키워드로 필터링
            if feed['source'] in ('데일리시큐',) or region == '해외':
                if not is_security_related(title, desc): continue
            tag, cls = classify(title, desc)

            # 쉴더스처럼 여러 매체를 링크로 묶어 브리핑하는 피드는
            # 대표 링크(link) 도메인을 실제 출처로, description 안의 관련 매체들도 함께 표시
            display_source = feed['source']
            source_group   = feed['source']  # 소소탭 그룹핑용 (접미사 없는 순수 매체명)
            if feed['source'] == '쉴더스' and link:
                base = media_from_url(link)
                display_source = base
                source_group   = base
                related = extract_source_outlets(raw_desc)
                related = [r for r in related if r != base]
                if related:
                    display_source = f"{base} 외 {len(related)}곳"

            items.append({
                'id':      make_id(link, title),
                'title':   title,
                'desc':    desc,
                'url':     link,
                'date':      fmt_date(dt),
                'datetime':  fmt_time(dt),
                'rawDate': dt.isoformat(),
                'source':      display_source,
                'sourceGroup': source_group,
                'channel': feed['channel'],
                'region':  region,
                'tag':     tag,
                'tagCls':  cls,
                'lang':    'en' if region == '해외' else 'ko',
            })
        print(f"  ✅ {feed['source']}: {len(items)}건")
        return items
    except Exception as e:
        print(f"  ❌ {feed['source']}: {e}")
        return []

# ── 고객사(정보원) 자체 JSON 피드 ─────────────────────
CUSTOM_JSON_FEEDS = [
    {
        'url':     'https://raw.githubusercontent.com/HarkjinDev/boannews-rss/main/feeds.json',
        'source':  '정보원',
        'channel': '정보원',
        'key':     'security_news',  # 기사 배열이 담긴 최상위 키
    },
]

def fetch_custom_json(feed):
    # 정보원 JSON의 source 코드값 -> 실제 매체 표시명
    SOURCE_DISPLAY = {
        'boannews':         '보안뉴스',
        'dailysecu':        '데일리시큐',
        'thehackernews':    'TheHackerNews',
        'bleepingcomputer': 'BleepingComputer',
        'securityweek':     'SecurityWeek',
        'krebsonsecurity':  'KrebsOnSecurity',
        'darkreading':      'DarkReading',
    }
    try:
        r = requests.get(feed['url'], headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"  ❌ {feed['source']}: HTTP {r.status_code}")
            return []
        data = r.json()
        articles = data.get(feed['key'], [])

        items = []
        cutoff = datetime.now(KST) - timedelta(hours=RETENTION_HRS)
        for a in articles:
            title = clean_html(a.get('title_ko') or a.get('title',''))
            desc  = clean_html(a.get('summary_ko') or a.get('summary',''))[:400]
            link  = a.get('link','')
            dt    = parse_dt(a.get('published',''))
            if dt < cutoff: continue
            combined = title + ' ' + desc
            if any(kw in combined for kw in EXCLUDE_KEYWORDS):
                continue
            lang = a.get('lang','ko')
            if lang != 'ko' and not is_security_related(title, desc):
                continue

            # 실제 원문 매체명 결정: naver 유입이면 링크 도메인으로 추정, 그 외는 코드값 매핑
            raw_source = a.get('source', '')
            if raw_source == 'naver':
                display_source = media_from_url(link) if link else '네이버뉴스'
            else:
                display_source = SOURCE_DISPLAY.get(raw_source, raw_source or feed['source'])

            # region 판별: lang이 ko가 아니면 해외, 그 외엔 링크 도메인으로 최종 확인
            if lang != 'ko':
                region = '해외'
            else:
                region = region_from_url(link) if link else '국내'

            tag, cls = classify(title, desc)
            items.append({
                'id':      make_id(link, title),
                'title':   title,
                'desc':    desc,
                'url':     link,
                'date':      fmt_date(dt),
                'datetime':  fmt_time(dt),
                'rawDate': dt.isoformat(),
                'source':      display_source,
                'sourceGroup': display_source,
                'channel': feed['channel'],
                'region':  region,
                'tag':     tag,
                'tagCls':  cls,
                'lang':    lang,
            })
        print(f"  ✅ {feed['source']}: {len(items)}건")
        return items
    except Exception as e:
        print(f"  ❌ {feed['source']}: {e}")
        return []

# ── 기관 모니터링 웹훅 ─────────────────────────────────
# 제목/요약에 아래 기관명이 들어간 기사를 웹훅으로 전송
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', '')   # GitHub Actions Secret
WEBHOOK_SENT_PATH = Path(__file__).parent.parent / 'data' / 'webhook_sent.json'

# 하트비트: 실행 여부 추적용 "수집 완료, 매칭 N건" 알림.
# 끄고 싶으면 Actions env에 HEARTBEAT=off. 기본값은 켜짐(on).
HEARTBEAT_ENABLED = os.environ.get('HEARTBEAT', 'on').lower() not in ('off', '0', 'false', 'no')
# 하트비트 최소 전송 간격(시간). 매 실행(30분)마다 울리면 시끄러우므로 2시간에 1번만.
# env HEARTBEAT_INTERVAL_HRS 로 조정 가능. (기관매칭 알림은 이 간격과 무관하게 즉시 전송)
HEARTBEAT_INTERVAL_HRS = float(os.environ.get('HEARTBEAT_INTERVAL_HRS', '2'))
HEARTBEAT_PATH = Path(__file__).parent.parent / 'data' / 'last_heartbeat.txt'

MONITOR_ORGS = [
    '기획예산처', '동행복권', '재정경제부', '글로벌지식협력단지', '관세청',
    '조달청', '국가데이터처', '한국은행', '한국수출입은행', '한국조폐공사',
    '한국투자공사', '한국재정정보원', 'dBrain', '국세청', '한미전략투자공사','재경부','재경위','기획처','한은','디브레인','재정경제기획위원회','한미'
]

# 짧은 약칭은 단어 경계로만 매칭 (예: '한은'이 '신한은행'의 일부로 걸리는 오탐 방지).
# 이 목록의 단어는 앞/뒤에 한글·영숫자가 바로 붙어 있으면 다른 단어의 일부로 보고 제외.
SHORT_ORG_ALIASES = {'한은','한미'}

# 위협 그룹(APT·랜섬웨어·해킹그룹). 그룹명 자체가 보안 주제이므로
# 제목에 그룹명이 있으면 보안키워드 없이도 웹훅 전송. (dBrain처럼 영문은 대소문자 무시)
THREAT_GROUPS = [
    '샤이니헌터스', 'ShinyHunters',
    '킴수키', 'Kimsuky',
    '라자루스', 'Lazarus',
    '실버폭스', 'SilverFox', 'Silver Fox',
    '안다리엘', 'Andariel',
    '김수키',                       # 표기 변형
    'APT37', 'APT38', 'APT28', 'APT29',
    'BlueNoroff', '블루노로프',
    'ScarCruft', '스카크루프트',
    'Konni', '코니',
    '락빗', 'LockBit',
    '블랙캣', 'BlackCat', 'ALPHV',
    '클롭', 'Clop', 'Cl0p',
    '알피에이치씨','에베레스트','Everest'                 # (여유분, 필요시 제거)
]

# 웹훅 중복 전송 방지용 제목 유사도 (수집용 is_similar와 별개, 안전하게 0.80)
WEBHOOK_SIM_THRESH = 0.80
def is_similar_webhook(t1, t2):
    return SequenceMatcher(None, t1, t2).ratio() >= WEBHOOK_SIM_THRESH

def _contains_bounded(text, kw):
    """kw가 text에 '단어 단위'로 들어있는지. 앞뒤에 한글/영숫자가 바로 붙으면 제외."""
    for m in re.finditer(re.escape(kw), text):
        s, e = m.start(), m.end()
        before = text[s-1] if s > 0 else ''
        after  = text[e]   if e < len(text) else ''
        boundary = re.compile(r'[가-힣A-Za-z0-9]')
        if boundary.match(before or ' '):   # 앞에 글자 붙음 → 다른 단어 일부
            continue
        if boundary.match(after or ' '):     # 뒤에 글자 붙음 → 다른 단어 일부
            continue
        return True
    return False

def match_orgs(title, desc=''):
    """제목+요약에서 매칭된 기관명 리스트 반환.
    - dBrain은 대소문자 무시
    - 짧은 약칭(SHORT_ORG_ALIASES)은 단어 경계로만 매칭(신한은행 등 오탐 방지)
    """
    text = (title or '') + ' ' + (desc or '')
    text_lower = text.lower()
    hits = []
    for org in MONITOR_ORGS:
        if org == 'dBrain':
            if 'dbrain' in text_lower:
                hits.append(org)
        elif org in SHORT_ORG_ALIASES:
            if _contains_bounded(text, org):
                hits.append(org)
        elif org in text:
            hits.append(org)
    return hits

def match_threat_groups(title, desc=''):
    """제목+요약에서 매칭된 위협 그룹명 리스트 반환 (영문은 대소문자 무시)"""
    text = (title or '') + ' ' + (desc or '')
    text_lower = text.lower()
    hits = []
    for g in THREAT_GROUPS:
        if re.search(r'[A-Za-z]', g):   # 영문 포함 → 대소문자 무시 비교
            if g.lower() in text_lower:
                hits.append(g)
        else:
            if g in text:
                hits.append(g)
    return hits

def load_webhook_sent():
    """
    이미 웹훅으로 보낸 기사 기록 로드. 형식: {link: {"ts": ISO시각, "title": 제목}}
    (구버전 {link: "ISO시각"} 형식도 자동 호환 — title 없이 로드됨)
    """
    if WEBHOOK_SENT_PATH.exists():
        try:
            raw = json.loads(WEBHOOK_SENT_PATH.read_text(encoding='utf-8'))
            out = {}
            for link, v in raw.items():
                if isinstance(v, dict):
                    out[link] = {'ts': v.get('ts', ''), 'title': v.get('title', '')}
                else:  # 구버전: 값이 시각 문자열
                    out[link] = {'ts': v, 'title': ''}
            return out
        except Exception:
            return {}
    return {}

def mark_sent(sent, link, title):
    """보낸 기사 기록 추가 (link + 제목 + 현재시각)"""
    sent[link] = {'ts': datetime.now(KST).isoformat(), 'title': title}

def sent_titles_within_retention(sent):
    """과거 기록 중 RETENTION_HRS 이내에 보낸 기사들의 제목 목록 (유사도 비교용)"""
    cutoff = datetime.now(KST) - timedelta(hours=RETENTION_HRS)
    titles = []
    for v in sent.values():
        t = v.get('title', '')
        if not t:
            continue
        try:
            dt = datetime.fromisoformat(v.get('ts', ''))
            if dt.tzinfo is None:
                dt = KST.localize(dt)
            if dt >= cutoff:
                titles.append(t)
        except Exception:
            titles.append(t)  # 시각 파싱 실패 시 안전하게 포함
    return titles

def save_webhook_sent(sent):
    """보낸 기록 저장 (RETENTION_HRS보다 오래된 항목은 정리)"""
    cutoff = datetime.now(KST) - timedelta(hours=RETENTION_HRS)
    pruned = {}
    for link, v in sent.items():
        ts = v.get('ts', '') if isinstance(v, dict) else v
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = KST.localize(dt)
            if dt >= cutoff:
                pruned[link] = v if isinstance(v, dict) else {'ts': ts, 'title': ''}
        except Exception:
            pruned[link] = v if isinstance(v, dict) else {'ts': ts, 'title': ''}
    WEBHOOK_SENT_PATH.parent.mkdir(exist_ok=True)
    WEBHOOK_SENT_PATH.write_text(json.dumps(pruned, ensure_ascii=False, indent=2), encoding='utf-8')

def send_webhook(title, link, orgs, source='', channel=''):
    """Dooray/Slack 호환 Incoming Webhook 전송. 실패해도 예외 안 던짐."""
    if not WEBHOOK_URL:
        return False
    org_str = ', '.join(orgs)
    # 출처 라인: 📍채널 · 매체  (어느 경로로 수집됐는지 구분용)
    loc = ' · '.join([x for x in (channel, source) if x])
    loc_line = f"📍{loc}\n" if loc else ''
    text = f"🏛 [{org_str}] 관련 기사\n{loc_line}{title}\n{link}"
    try:
        r = requests.post(
            WEBHOOK_URL,
            json={'text': text},
            headers={'Content-Type': 'application/json'},
            timeout=10,
        )
        if r.status_code in (200, 204):
            return True
        print(f"  ⚠️ 웹훅 응답 {r.status_code}: {r.text[:120]}")
        return False
    except Exception as e:
        print(f"  ⚠️ 웹훅 전송 실패: {e}")
        return False

def send_webhook_text(text):
    """임의 텍스트를 웹훅으로 전송 (하트비트 등). 실패해도 예외 안 던짐."""
    if not WEBHOOK_URL:
        return False
    try:
        r = requests.post(
            WEBHOOK_URL,
            json={'text': text},
            headers={'Content-Type': 'application/json'},
            timeout=10,
        )
        if r.status_code in (200, 204):
            return True
        print(f"  ⚠️ 웹훅 응답 {r.status_code}: {r.text[:120]}")
        return False
    except Exception as e:
        print(f"  ⚠️ 웹훅 전송 실패: {e}")
        return False

def process_org_webhooks(our_articles):
    """
    기관명 매칭 → 미전송 기사만 웹훅 발송.
    검사 대상: (1) 우리가 수집한 전체 기사, (2) 정보원 외부 feeds.json 전체(보안뉴스+취약점+평판)
    """
    if not WEBHOOK_URL:
        print("\n[기관 웹훅] WEBHOOK_URL 미설정 — 건너뜀")
        return 0, 0
    print("\n[기관 웹훅] 기관명 매칭 검사")

    sent = load_webhook_sent()

    # 검사 대상 통합 (link, title, desc, source, channel) 튜플
    candidates = []
    for a in our_articles:
        candidates.append((a.get('url',''), a.get('title',''), a.get('desc',''),
                           a.get('source',''), a.get('channel','')))

    # 정보원 외부 feeds.json 전체(3개 배열) 추가로 검사
    JB_CAT = {'security_news': '정보원·보안뉴스', 'vulnerability': '정보원·취약점', 'reputation': '정보원·평판'}
    for feed in CUSTOM_JSON_FEEDS:
        try:
            r = requests.get(feed['url'], headers=HEADERS, timeout=15)
            if r.status_code == 200:
                data = r.json()
                cutoff = datetime.now(KST) - timedelta(hours=RETENTION_HRS)
                for key in ('security_news', 'vulnerability', 'reputation'):
                    for a in data.get(key, []):
                        dt = parse_dt(a.get('published',''))
                        if dt < cutoff:
                            continue
                        title = clean_html(a.get('title_ko') or a.get('title',''))
                        desc  = clean_html(a.get('summary_ko') or a.get('summary',''))
                        link  = a.get('link','')
                        src   = a.get('source','')
                        candidates.append((link, title, desc, src, JB_CAT.get(key, '정보원')))
        except Exception as e:
            print(f"  ⚠️ 정보원 feeds.json 재조회 실패: {e}")

    # 매칭 + 미전송 기사만 전송
    sent_count = 0
    seen_links = set()
    # 유사도 비교 대상 = 과거 72시간 이내 보낸 제목(사이클 넘나드는 중복 차단) + 이번 실행 제목
    sent_titles = sent_titles_within_retention(sent)
    for link, title, desc, source, channel in candidates:
        if not link or link in seen_links:
            continue
        seen_links.add(link)
        if link in sent:          # 이미 보낸 기사(링크 동일)
            continue
        # 전송 조건 (제목만 검사):
        #  (A) 기관명 + 보안키워드 둘 다  → 기관 관련 보안기사
        #  (B) 위협그룹명               → 그룹명 자체가 보안 주제이므로 보안키워드 불필요
        orgs    = match_orgs(title)
        groups  = match_threat_groups(title)
        hit_org   = bool(orgs) and is_security_related(title)
        hit_group = bool(groups)
        if not (hit_org or hit_group):
            continue
        # 알림 라벨 = 매칭된 기관명 + 위협그룹명 (둘 다 표시)
        labels = (orgs if hit_org else []) + groups
        # 같은 사건이 (여러 채널로 / 다른 수집 사이클에) 들어온 경우:
        # 제목 유사도(0.80)로 중복 판단해 한 번만 전송. 과거 보낸 제목과도 비교.
        # 중복으로 걸러진 것도 '보낸 것'으로 기록해 다음 실행에서 다시 안 오게 함.
        if any(is_similar_webhook(title, t) for t in sent_titles):
            mark_sent(sent, link, title)
            print(f"  ⏭  (유사중복) [{', '.join(labels)}] {title[:36]}")
            continue
        if send_webhook(title, link, labels, source, channel):
            mark_sent(sent, link, title)
            sent_titles.append(title)
            sent_count += 1
            print(f"  📤 [{', '.join(labels)}] ({channel}) {title[:36]}")
            time.sleep(0.5)       # 웹훅 rate limit 여유

    save_webhook_sent(sent)
    checked_count = len(seen_links)   # 중복 제거 후 실제로 검사한 고유 기사 수
    print(f"[기관 웹훅] 검사 {checked_count}건 · {sent_count}건 전송 (기록 {len(sent)}건 보관)")
    return checked_count, sent_count

def send_heartbeat(checked_count, matched_count):
    """
    하트비트 전송. 매 실행마다가 아니라 마지막 전송 후 HEARTBEAT_INTERVAL_HRS(기본 2h) 지났을 때만.
    마지막 전송 시각은 data/last_heartbeat.txt 에 기록.
    """
    if not WEBHOOK_URL or not HEARTBEAT_ENABLED:
        return
    now_dt = datetime.now(KST)

    # 마지막 전송 시각 확인 — 2시간 안 지났으면 건너뜀
    if HEARTBEAT_PATH.exists():
        try:
            last = datetime.fromisoformat(HEARTBEAT_PATH.read_text(encoding='utf-8').strip())
            if last.tzinfo is None:
                last = KST.localize(last)
            if now_dt - last < timedelta(hours=HEARTBEAT_INTERVAL_HRS):
                print(f"[하트비트] 간격 미도달 — 건너뜀 (마지막 {last.strftime('%H:%M')})")
                return
        except Exception:
            pass  # 파싱 실패 시 그냥 전송

    now = now_dt.strftime('%m-%d %H:%M')
    if matched_count > 0:
        text = f"✅ [수집완료 {now}] 검사 {checked_count}건 · 기관매칭 {matched_count}건 전송"
    else:
        text = f"✅ [수집완료 {now}] 검사 {checked_count}건 · 기관매칭 없음"
    send_webhook_text(text)
    # 전송 성공 여부와 무관하게 시각 기록(실패해도 2시간 뒤 재시도되도록)
    try:
        HEARTBEAT_PATH.parent.mkdir(exist_ok=True)
        HEARTBEAT_PATH.write_text(now_dt.isoformat(), encoding='utf-8')
    except Exception:
        pass
    print(f"[하트비트] 전송: {text}")

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
            # 드라마/영화/게임/연예 등 보안뉴스와 무관한 문맥은 제외
            combined = title + ' ' + desc
            if any(kw in combined for kw in EXCLUDE_KEYWORDS):
                continue
            tag, cls = classify(title, desc)
            source = media_from_url(link) if link else '네이버뉴스'
            items.append({
                'id':      make_id(link, title),
                'title':   title,
                'desc':    desc,
                'url':     link,
                'date':      fmt_date(dt),
                'datetime':  fmt_time(dt),
                'rawDate': dt.isoformat(),
                'source':      source,
                'sourceGroup': source,
                'channel': '자체개발',
                'region':  '국내',
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
    # 정보원은 더 이상 feeds.json에 저장하지 않음(정보원 탭이 외부 feeds.json 직접 사용).
    # 기존에 쌓여있던 정보원 기사는 아래 필터에서 자동 정리됨.
    VALID_CHANNELS = {'자체개발', '쉴더스'}
    VALID_REGIONS  = {'국내', '해외'}
    if FEEDS_PATH.exists():
        try:
            existing = json.loads(FEEDS_PATH.read_text(encoding='utf-8'))
            stale_count = 0
            for a in existing.get('articles', []):
                if a.get('channel') not in VALID_CHANNELS or a.get('region') not in VALID_REGIONS:
                    stale_count += 1
                    continue
                try:
                    dt = datetime.fromisoformat(a.get('rawDate',''))
                    if dt.tzinfo is None:
                        dt = pytz.utc.localize(dt)
                    if dt.astimezone(KST) >= cutoff:
                        all_items[a['id']] = a
                except:
                    pass
            print(f"[기존 데이터] {len(all_items)}건 로드 (예전 그룹값 {stale_count}건 정리됨)")
        except Exception as e:
            print(f"[기존 데이터] 로드 실패: {e}")

    # RSS 수집
    print("\n[RSS 수집]")
    for feed in FEEDS:
        rss_items = fetch_rss(feed)
        for item in rss_items:
            all_items[item['id']] = item
        time.sleep(0.3)

    # 정보원(외부 feeds.json)은 feeds.json에 저장하지 않음 —
    # index.html이 정보원 탭에서 외부 feeds.json을 직접 받아 표시하므로 중복 저장 불필요.
    # (단, 웹훅 검사는 아래 process_org_webhooks에서 외부 feeds.json을 직접 조회함)

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

    # 최대 개수 제한 — 단순 [:500]이면 실시간으로 쏟아지는 자체개발이 다 차지하고
    # 소량 피드(쉴더스)가 전부 밀려나므로, 채널별 최소 보장 후 나머지를 채운다.
    # (deduped는 이미 최신순 정렬된 상태)
    SHIELDUS_MIN = 50   # 쉴더스 최소 보장 건수(최신순, 있으면 이만큼은 무조건 남김)
    shieldus = [a for a in deduped if a.get('channel') == '쉴더스']
    others   = [a for a in deduped if a.get('channel') != '쉴더스']
    keep_shieldus = shieldus[:SHIELDUS_MIN]
    keep_others   = others[:MAX_ARTICLES - len(keep_shieldus)]
    # 합친 뒤 다시 최신순 정렬
    deduped = sorted(keep_shieldus + keep_others,
                     key=lambda x: x.get('rawDate', ''), reverse=True)
    print(f"[개수 제한] 전체 {len(deduped)}건 (쉴더스 {len(keep_shieldus)} + 그외 {len(keep_others)})")

    # 저장
    FEEDS_PATH.parent.mkdir(exist_ok=True)
    output = {
        'fetched_at': datetime.now(KST).isoformat(),
        'count': len(deduped),
        'articles': deduped,
    }
    FEEDS_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"\n✅ feeds.json 저장: {len(deduped)}건")

    # 기관명 매칭 웹훅 전송 (우리 수집분 + 정보원 외부 feeds.json 전체 대상)
    checked, matched = process_org_webhooks(deduped)

    # 하트비트: 매 실행마다 '검사한 총 건수(정보원 포함) + 매칭 건수' 알림
    send_heartbeat(checked, matched)

    print(f"{'='*50}\n")

if __name__ == '__main__':
    main()
