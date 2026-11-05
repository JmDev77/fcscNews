#!/usr/bin/env python3
"""
crawl_test.py - 매체(도메인)별 원문 크롤링 성공률 측정
- data/feeds.json(자체개발+쉴더스) + 정보원 외부 feeds.json 의 기사 URL을 도메인별로 묶고
- 도메인마다 최신 기사 N건을 summarize.py 의 crawl() 로 실제 크롤링해 본다.
  (뉴스레터 생성 때와 '완전히 같은 함수'를 쓰므로 결과가 그대로 실전 지표가 됨)
- 결과: data/crawl_test_result.json  +  Actions 실행 화면 요약표(GITHUB_STEP_SUMMARY)

환경변수(워크플로 입력):
  SAMPLE       도메인당 테스트 기사 수 (기본 3)
  MIN_LEN      '성공'으로 칠 최소 본문 길이 (기본 300자)
  INCLUDE_JB   정보원 외부 feeds.json 포함 여부 (기본 on)
  WORKERS      동시에 테스트할 도메인 수 (기본 8, 같은 도메인은 항상 순차)
  ONLY_FAILED  on 이면 직전 결과(crawl_test_result.json)에서 '지원'이 아니었거나 '의심'이었던 도메인만 재시험

[2차 추가]
- 의심(suspect): '지원'으로 나왔지만 본문이 아닌 걸 긁은 것으로 보이는 경우
  (p태그 전체로 추출 / 첫 문장이 '많이 본 기사'·광고·AI요약 안내 등 / 샘플끼리 앞부분이 똑같음)
- 힌트(hints): 실패·짧음·의심 샘플은 페이지를 한 번 더 받아서 '본문일 가능성이 큰 요소'의
  CSS 셀렉터 후보(#id / tag.class)와 글자수를 기록 → summarize.py BODY_SELECTORS 에 바로 추가 가능

판정: 지원 = 시도한 기사 전부 성공 / 불안정 = 일부만 성공 / 미지원 = 전부 실패
실패 사유: HTTP 403 등(접속 차단 → 그 매체 나머지 기사는 건너뜀) · 연결실패 · 추출실패(페이지는
받았지만 본문을 못 찾음 → summarize.py BODY_SELECTORS 에 셀렉터를 추가하면 고칠 수 있음)
"""

import os, re, json, threading
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import summarize            # 같은 scripts 폴더의 summarize.py — crawl() 재사용
import collect              # media_from_url / CUSTOM_JSON_FEEDS / HEADERS 재사용

DATA_DIR   = Path(__file__).parent.parent / 'data'
FEEDS_PATH = DATA_DIR / 'feeds.json'
OUT_PATH   = DATA_DIR / 'crawl_test_result.json'

SAMPLE     = int(os.environ.get('SAMPLE', '3') or 3)
MIN_LEN    = int(os.environ.get('MIN_LEN', '300') or 300)
INCLUDE_JB = os.environ.get('INCLUDE_JB', 'on').lower() not in ('off', '0', 'false', 'no')
WORKERS    = int(os.environ.get('WORKERS', '8') or 8)
ONLY_FAILED = os.environ.get('ONLY_FAILED', 'off').lower() in ('on', '1', 'true', 'yes')

# 본문이 아닌 걸 긁었을 때 첫머리에 자주 나오는 문구
JUNK_HEAD = re.compile(
    r'많이 본|인기 ?기사|실시간|구독|로그인|회원가입|무단전재|저작권|AI가 자동 생성|'
    r'^learn how|^subscribe|^sign up|^advertis|^sponsored|^related|newsletter|cookie', re.I)

# ── summarize.crawl() 의 진행 로그(print)를 스레드별로 가로채서 '추출 방법'·HTTP 코드를 기록 ──
_tls = threading.local()
_real_print = print
def _capture_print(*args, **kw):
    buf = getattr(_tls, 'buf', None)
    if buf is not None:
        buf.append(' '.join(str(a) for a in args))
    else:
        _real_print(*args, **kw)
summarize.print = _capture_print     # 모듈 전역 print 를 덮어씀 (summarize 안에서만 적용)

def host_of(url):
    try:
        return re.sub(r'^(www|m|n)\.', '', url.split('/')[2].lower())
    except Exception:
        return ''

def method_of(lines):
    """crawl() 로그에서 어떤 단계로 본문을 뽑았는지 추출"""
    for ln in lines:
        if 'JSON-LD' in ln:            return 'JSON-LD'
        m = re.search(r"셀렉터 '([^']+)'", ln)
        if m:                          return '셀렉터 ' + m.group(1)
        if '밀도분석' in ln:           return '밀도분석'
        if 'p태그 전체' in ln:         return 'p태그 전체'
    return ''

def http_of(lines):
    codes = [m.group(1) for ln in lines for m in [re.search(r'HTTP (\d{3})', ln)] if m]
    if codes:
        return codes[-1]
    if any('요청 실패' in ln for ln in lines):
        return '연결실패'
    if any('예외' in ln or '크롤링 오류' in ln for ln in lines):
        return '오류'
    return '추출실패'      # 페이지는 받았는데 본문을 못 찾음 → 셀렉터 추가 대상

def test_one(url):
    _tls.buf = []
    try:
        body = summarize.crawl(url)
    except Exception as e:
        body = ''
        _tls.buf.append(f'예외: {e}')
    lines, _tls.buf = _tls.buf, None
    n = len(body or '')
    status = 'ok' if n >= MIN_LEN else ('short' if n > 0 else 'fail')
    return {
        'status': status,
        'len':    n,
        'method': method_of(lines),
        'http':   http_of(lines) if status == 'fail' else '',
        'head':   (body or '')[:150].replace('\n', ' '),
    }

# ── 진단: 본문 후보 요소의 셀렉터 찾기 ─────────────────
def sel_of(el):
    if el.get('id'):
        return '#' + el['id']
    cls = [c for c in (el.get('class') or []) if not re.match(r'^(clear|cf|clearfix|row|col.*|container|wrap(per)?)$', c)]
    if cls:
        return el.name + '.' + '.'.join(cls[:2])
    return ''

def diagnose(url):
    """페이지를 다시 받아 본문으로 보이는 요소 상위 3개(셀렉터·글자수·앞부분)를 돌려줌"""
    info = {'http': '', 'size': 0, 'title': '', 'hints': [], 'note': ''}
    try:
        r = requests.get(url, headers=summarize.HEADERS, timeout=20, allow_redirects=True)
        info['http'] = str(r.status_code)
        html = r.content.decode(r.apparent_encoding or 'utf-8', errors='ignore')
        info['size'] = len(html)
        if r.url != url:
            info['note'] = '리다이렉트: ' + r.url[:120]
        soup = BeautifulSoup(html, 'lxml')
        if soup.title:
            info['title'] = soup.title.get_text(strip=True)[:80]
        for t in soup(['script', 'style', 'noscript', 'nav', 'header', 'footer', 'aside', 'form']):
            t.decompose()
        body_txt = len(soup.get_text(' ', strip=True))
        if body_txt < 400:
            info['note'] = (info['note'] + ' ' if info['note'] else '') + \
                f'페이지 텍스트 {body_txt}자뿐 → JS로 본문을 그리는 사이트(정적 크롤링 불가)일 가능성'
        cands = []
        for el in soup.find_all(['div', 'section', 'article', 'main', 'td']):
            s = sel_of(el)
            if not s:
                continue
            txt = el.get_text(' ', strip=True)
            tl = len(txt)
            if tl < 200:
                continue
            ll = sum(len(a.get_text(strip=True)) for a in el.find_all('a'))
            if ll / tl > 0.3:          # 링크가 많으면 목록/메뉴
                continue
            cands.append((el, s, tl, txt))
        # '가장 좁은' 본문 컨테이너 우선: 자식 후보 중 자기 텍스트의 80% 이상을 가진 게 있으면 자신은 뺌
        keep = []
        cand_ids = {id(c[0]) for c in cands}
        has_big_child = set()
        for c, _, c_tl, _ in cands:
            for p in c.parents:          # 후보 c의 조상 중 후보인 것: c가 그 조상 텍스트의 80% 이상이면 조상 제외
                if id(p) in cand_ids:
                    p_tl = next(x[2] for x in cands if x[0] is p)
                    if c_tl >= p_tl * 0.8:
                        has_big_child.add(id(p))
        for el, s, tl, txt in cands:
            if id(el) not in has_big_child:
                keep.append((s, tl, txt))
        keep.sort(key=lambda x: -x[1])
        seen = set()
        for s, tl, txt in keep:
            if s in seen:
                continue
            seen.add(s)
            info['hints'].append({'selector': s, 'len': tl, 'head': txt[:70]})
            if len(info['hints']) >= 3:
                break
    except Exception as e:
        info['note'] = f'진단 실패: {e}'
    return info

def suspect_reason(samples):
    oks = [s for s in samples if s['status'] == 'ok']
    if not oks:
        return ''
    if all(s['method'] == 'p태그 전체' for s in oks):
        return 'p태그 전체로만 추출(본문 영역을 못 찾음)'
    if any(JUNK_HEAD.search(s['head'][:40]) for s in oks):
        return '첫머리가 본문이 아님: ' + next(s['head'][:40] for s in oks if JUNK_HEAD.search(s['head'][:40]))
    heads = [s['head'][:40] for s in oks]
    if len(heads) >= 2 and len(set(heads)) == 1:
        return '서로 다른 기사인데 앞부분이 똑같음(공통 영역을 긁음)'
    return ''

def verdict(ok, short, fail):
    total = ok + short + fail
    if total == 0:        return '미측정'
    if ok == total:       return '지원'
    if ok == 0:           return '미지원'
    return '불안정'

# ── 테스트 대상 수집 ───────────────────────────────────
def gather():
    arts = []
    if FEEDS_PATH.exists():
        data = json.loads(FEEDS_PATH.read_text(encoding='utf-8'))
        for a in data.get('articles', []):
            arts.append({'url': a.get('url', ''), 'title': a.get('title', ''),
                         'source': a.get('sourceGroup') or a.get('source', ''),
                         'channel': a.get('channel', ''), 'rawDate': a.get('rawDate', '')})
    if INCLUDE_JB:
        for feed in collect.CUSTOM_JSON_FEEDS:
            try:
                r = requests.get(feed['url'], headers=collect.HEADERS, timeout=15)
                r.raise_for_status()
                d = r.json()
                for key in ('security_news', 'vulnerability', 'reputation'):
                    for a in d.get(key, []):
                        link = a.get('link', '')
                        arts.append({'url': link, 'title': a.get('title_ko') or a.get('title', ''),
                                     'source': collect.media_from_url(link) if link else '',
                                     'channel': '정보원', 'rawDate': a.get('published', '')})
            except Exception as e:
                _real_print(f'⚠️ 정보원 feeds.json 조회 실패: {e}')

    by_dom = defaultdict(list)
    seen = set()
    for a in arts:
        u = a['url']
        if not u or not u.startswith('http') or u in seen:
            continue
        seen.add(u)
        d = host_of(u)
        if d:
            by_dom[d].append(a)
    # 도메인별 최신순 → 앞에서 SAMPLE 건
    for d in by_dom:
        by_dom[d].sort(key=lambda x: x.get('rawDate', ''), reverse=True)
    return by_dom

def run_domain(dom, items):
    picks = items[:SAMPLE]
    samples = []
    for a in picks:
        res = test_one(a['url'])
        res.update({'title': a['title'][:80], 'url': a['url']})
        samples.append(res)
        # 접속 자체가 막힌 매체(403/연결실패 등)는 나머지 기사도 똑같이 막히므로 건너뜀 (시간 절약)
        if res['status'] == 'fail' and res['http'] not in ('추출실패', '오류', ''):
            break
    c = Counter(s['status'] for s in samples)
    lens = [s['len'] for s in samples if s['status'] == 'ok']
    suspect = suspect_reason(samples)
    # 실패·짧음·의심이면 첫 번째 문제 샘플을 진단 (접속 차단은 진단해도 의미 없으므로 제외)
    diag = None
    bad = [s for s in samples if s['status'] != 'ok' and s['http'] not in ('403', '401', '연결실패')]
    target = bad[0] if bad else (samples[0] if suspect else None)
    if target:
        diag = diagnose(target['url'])
        diag['url'] = target['url']
    out = {
        'domain':   dom,
        'media':    collect.media_from_url('https://' + dom + '/'),
        'channels': sorted({a['channel'] for a in items if a['channel']}),
        'articles': len(items),            # 수집돼 있는 기사 수(이 매체 비중)
        'tested':   len(samples),
        'ok': c['ok'], 'short': c['short'], 'fail': c['fail'],
        'verdict':  verdict(c['ok'], c['short'], c['fail']),
        'avg_len':  round(sum(lens) / len(lens)) if lens else 0,
        'methods':  sorted({s['method'] for s in samples if s['method']}),
        'suspect':  suspect,
        'diag':     diag,
        'samples':  samples,
    }
    mark = {'지원': '✅', '불안정': '⚠️', '미지원': '❌'}.get(out['verdict'], '·')
    if suspect:
        mark = '🟡'
    why = ','.join(sorted({s['http'] for s in samples if s['http']}))
    hint = (' 힌트: ' + ' | '.join(f"{h['selector']}({h['len']})" for h in diag['hints'])) if diag and diag['hints'] else ''
    with _plock:
        _real_print(f"{mark} {dom:<28} {out['ok']}/{out['tested']}  "
                    f"{', '.join(out['methods']) or '-'}  {('실패: ' + why) if why else ''}"
                    f"{(' 의심: ' + suspect) if suspect else ''}{hint}", flush=True)
    return out

_plock = threading.Lock()

def main():
    by_dom = gather()
    prev = None
    if ONLY_FAILED and OUT_PATH.exists():
        prev = json.loads(OUT_PATH.read_text(encoding='utf-8'))
        redo = {r['domain'] for r in prev.get('domains', [])
                if r.get('verdict') != '지원' or r.get('suspect')}
        # 직전 '지원'이었는데 의심 판정이 아직 없던(1차 결과) 도메인도 p태그 전체였으면 재시험
        redo |= {r['domain'] for r in prev.get('domains', [])
                 if r.get('methods') and all(m == 'p태그 전체' for m in r['methods'])}
        by_dom = {d: v for d, v in by_dom.items() if d in redo}
        _real_print(f"[재시험 모드] 직전 결과에서 실패·불안정·의심 도메인 {len(redo)}개 중 "
                    f"현재 기사가 있는 {len(by_dom)}개만 시험")
    total_art = sum(len(v) for v in by_dom.values())
    _real_print(f"\n{'='*60}\n크롤링 테스트: 도메인 {len(by_dom)}개 · 기사 {total_art}건 · "
                f"도메인당 {SAMPLE}건 · 성공 기준 {MIN_LEN}자 이상\n{'='*60}")

    # 기사 많은 매체부터 (뉴스레터에 자주 등장하는 순)
    order = sorted(by_dom, key=lambda d: -len(by_dom[d]))
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(lambda d: run_domain(d, by_dom[d]), order))

    # 재시험 모드: 이번에 다시 본 도메인만 갈아끼우고 나머지는 직전 결과 유지
    if prev:
        fresh = {r['domain'] for r in results}
        results = results + [r for r in prev.get('domains', []) if r['domain'] not in fresh]
        total_art = sum(r['articles'] for r in results)

    vc = Counter(r['verdict'] for r in results)
    n_sus = sum(1 for r in results if r.get('suspect'))
    # 기사 수 기준 커버리지: '의심 아닌' 지원 매체 기사 / 전체 기사
    cover = sum(r['articles'] for r in results if r['verdict'] == '지원' and not r.get('suspect'))
    output = {
        'tested_at': datetime.now(summarize.KST).isoformat(),
        'sample_per_domain': SAMPLE,
        'min_len': MIN_LEN,
        'summary': {
            'domains': len(results),
            '지원': vc['지원'], '불안정': vc['불안정'], '미지원': vc['미지원'],
            '의심': n_sus,
            'articles': total_art,
            'covered_articles': cover,
            'coverage_pct': round(cover * 100 / total_art, 1) if total_art else 0,
        },
        # index.html 에서 바로 쓰기 좋은 도메인 → 판정 맵
        'support_map': {r['domain']: ('불안정' if r.get('suspect') and r['verdict'] == '지원' else r['verdict'])
                        for r in results},
        'domains': results,
    }
    DATA_DIR.mkdir(exist_ok=True)
    OUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')

    s = output['summary']
    _real_print(f"\n지원 {s['지원']}(의심 {s['의심']}) · 불안정 {s['불안정']} · 미지원 {s['미지원']}  "
                f"(기사 기준 커버리지 {s['coverage_pct']}%)\n✅ {OUT_PATH.name} 저장")

    # Actions 실행 화면 요약표
    summ = os.environ.get('GITHUB_STEP_SUMMARY')
    if summ:
        L = [f"## 크롤링 테스트 결과 ({output['tested_at'][:16].replace('T', ' ')})",
             f"도메인 {s['domains']}개 · 도메인당 {SAMPLE}건 · 성공 기준 {MIN_LEN}자",
             f"**지원 {s['지원']}(그중 의심 {s['의심']}) · 불안정 {s['불안정']} · 미지원 {s['미지원']}** · "
             f"수집 기사 기준 커버리지(의심 제외) **{s['coverage_pct']}%**", "",
             "| 판정 | 도메인 | 매체 | 수집기사 | 성공/시도 | 평균길이 | 추출방법 | 실패 사유 / 의심 | 셀렉터 힌트 |",
             "|---|---|---|---:|---:|---:|---|---|---|"]
        rank = {'미지원': 0, '불안정': 1, '지원': 2}
        for r in sorted(results, key=lambda x: (rank.get(x['verdict'], 3) - (0.5 if x.get('suspect') else 0),
                                                -x['articles'])):
            mark = {'지원': '✅', '불안정': '⚠️', '미지원': '❌'}.get(r['verdict'], '')
            if r.get('suspect'):
                mark = '🟡'
            https = ','.join(sorted({x['http'] for x in r['samples'] if x['http']}))
            dg = r.get('diag') or {}
            hint = ' / '.join(f"`{h['selector']}` {h['len']}" for h in dg.get('hints', []))
            if dg.get('note'):
                hint = (hint + ' ' if hint else '') + '※' + dg['note'][:60]
            why = (https + ' ' if https else '') + (r.get('suspect') or '')
            L.append(f"| {mark} {r['verdict']} | {r['domain']} | {r['media']} | "
                     f"{r['articles']} | {r['ok']}/{r['tested']} | {r['avg_len']} | "
                     f"{', '.join(r['methods'])} | {why.replace('|', '/')} | {hint.replace('|', '/')} |")
        with open(summ, 'a', encoding='utf-8') as f:
            f.write('\n'.join(L) + '\n')

if __name__ == '__main__':
    main()
