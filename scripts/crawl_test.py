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

import summarize            # 같은 scripts 폴더의 summarize.py — crawl() 재사용
import collect              # media_from_url / CUSTOM_JSON_FEEDS / HEADERS 재사용

DATA_DIR   = Path(__file__).parent.parent / 'data'
FEEDS_PATH = DATA_DIR / 'feeds.json'
OUT_PATH   = DATA_DIR / 'crawl_test_result.json'

SAMPLE     = int(os.environ.get('SAMPLE', '3') or 3)
MIN_LEN    = int(os.environ.get('MIN_LEN', '300') or 300)
INCLUDE_JB = os.environ.get('INCLUDE_JB', 'on').lower() not in ('off', '0', 'false', 'no')
WORKERS    = int(os.environ.get('WORKERS', '8') or 8)

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
        'head':   (body or '')[:80].replace('\n', ' '),
    }

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
        'samples':  samples,
    }
    mark = {'지원': '✅', '불안정': '⚠️', '미지원': '❌'}.get(out['verdict'], '·')
    why = ','.join(sorted({s['http'] for s in samples if s['http']}))
    with _plock:
        _real_print(f"{mark} {dom:<28} {out['ok']}/{out['tested']}  "
                    f"{', '.join(out['methods']) or '-'}  {('실패: ' + why) if why else ''}", flush=True)
    return out

_plock = threading.Lock()

def main():
    by_dom = gather()
    total_art = sum(len(v) for v in by_dom.values())
    _real_print(f"\n{'='*60}\n크롤링 테스트: 도메인 {len(by_dom)}개 · 기사 {total_art}건 · "
                f"도메인당 {SAMPLE}건 · 성공 기준 {MIN_LEN}자 이상\n{'='*60}")

    # 기사 많은 매체부터 (뉴스레터에 자주 등장하는 순)
    order = sorted(by_dom, key=lambda d: -len(by_dom[d]))
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(lambda d: run_domain(d, by_dom[d]), order))

    vc = Counter(r['verdict'] for r in results)
    # 기사 수 기준 커버리지: 지원 매체 기사 / 전체 기사
    cover = sum(r['articles'] for r in results if r['verdict'] == '지원')
    output = {
        'tested_at': datetime.now(summarize.KST).isoformat(),
        'sample_per_domain': SAMPLE,
        'min_len': MIN_LEN,
        'summary': {
            'domains': len(results),
            '지원': vc['지원'], '불안정': vc['불안정'], '미지원': vc['미지원'],
            'articles': total_art,
            'covered_articles': cover,
            'coverage_pct': round(cover * 100 / total_art, 1) if total_art else 0,
        },
        # index.html 에서 바로 쓰기 좋은 도메인 → 판정 맵
        'support_map': {r['domain']: r['verdict'] for r in results},
        'domains': results,
    }
    DATA_DIR.mkdir(exist_ok=True)
    OUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')

    s = output['summary']
    _real_print(f"\n지원 {s['지원']} · 불안정 {s['불안정']} · 미지원 {s['미지원']}  "
                f"(기사 기준 커버리지 {s['coverage_pct']}%)\n✅ {OUT_PATH.name} 저장")

    # Actions 실행 화면 요약표
    summ = os.environ.get('GITHUB_STEP_SUMMARY')
    if summ:
        L = [f"## 크롤링 테스트 결과 ({output['tested_at'][:16].replace('T', ' ')})",
             f"도메인 {s['domains']}개 · 도메인당 {SAMPLE}건 · 성공 기준 {MIN_LEN}자",
             f"**지원 {s['지원']} · 불안정 {s['불안정']} · 미지원 {s['미지원']}** · "
             f"수집 기사 기준 커버리지 **{s['coverage_pct']}%**", "",
             "| 판정 | 도메인 | 매체 | 채널 | 수집기사 | 성공/시도 | 평균길이 | 추출방법 | 실패 사유 |",
             "|---|---|---|---|---:|---:|---:|---|---|"]
        rank = {'미지원': 0, '불안정': 1, '지원': 2}
        for r in sorted(results, key=lambda x: (rank.get(x['verdict'], 3), -x['articles'])):
            mark = {'지원': '✅', '불안정': '⚠️', '미지원': '❌'}.get(r['verdict'], '')
            https = ','.join(sorted({x['http'] for x in r['samples'] if x['http']}))
            L.append(f"| {mark} {r['verdict']} | {r['domain']} | {r['media']} | {'/'.join(r['channels'])} | "
                     f"{r['articles']} | {r['ok']}/{r['tested']} | {r['avg_len']} | "
                     f"{', '.join(r['methods'])} | {https} |")
        with open(summ, 'a', encoding='utf-8') as f:
            f.write('\n'.join(L) + '\n')

if __name__ == '__main__':
    main()
