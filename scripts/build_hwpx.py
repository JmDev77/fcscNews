#!/usr/bin/env python3
"""
build_hwpx.py - 요약 결과(result_*.json) + template.hwpx → 뉴스레터 HWPX

사용:  python scripts/build_hwpx.py data/result_<id>.json data/out/newsletter_<id>.hwpx
환경변수(선택):
  TEMPLATE   양식 경로 (기본: 저장소 루트 template.hwpx)
  NL_DATE    머리말 날짜 YYYY-MM-DD (기본: 실행하는 날, 한국시각)

양식(template.hwpx)은 '실제로 채워진 예시 뉴스레터'다. 이 스크립트는 예시 문단을 복제해서
서식(문단모양·글자모양)은 그대로 두고 글자만 바꾼다. 구조:
  표0  머리말: [로고] [정보보안 뉴스레터] [정보보안 뉴스레터 3539 / [ ’26. 6. 1. ]]  ← 날짜만 교체(호수는 그대로)
  표1  ● 정보보안 뉴스 요약
  표2  요약표  No | 제목 | 날짜 | 출처  × 5행
  표3  ● 정보보안 뉴스
  표4  (셀 하나) 기사별 "N. 제목 [출처]" + ○요약 2줄  × 5
                → 고정 안내문(브라우저 업데이트) → [본문]
                → 기사별 "  N. 제목 [출처]" + 본문 문단들 + "[원문기사] URL"  × 5

※ 예시 문단마다 자간(-14~+3)·줄간격(150~160%)이 손으로 미세조정돼 있어서,
  기사마다 같은 모양이 나오도록 '기본' 문단 하나씩을 골라 복제한다
  (제목: 글자모양14, 요약줄·본문: 11, 요약표: 12, 본문 문단: 문단모양31 = 줄간격160%·양쪽정렬).
"""
import copy, json, os, re, sys, zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from lxml import etree

HP = 'http://www.hancom.co.kr/hwpml/2011/paragraph'
HH = 'http://www.hancom.co.kr/hwpml/2011/head'
NS = {'hp': HP, 'hh': HH}
def q(tag): return '{%s}%s' % (HP, tag)

# ── 1페이지 한 줄 맞춤(자간 자동 줄이기) ────────────────────────────
# 글자 폭 추정 모델(글자 크기 1em 기준). template.hwpx 에 저장된 한글의 실제 줄바꿈 위치
# 128줄로 맞춰 본 값 — 127줄을 정확히 예측(휴먼명조/HCI Poppy·맑은 고딕 10pt 공통으로 맞음).
# 자간 s% 는 글자마다 s/100 em 씩 더해지는 것으로 계산(템플릿에서 사람이 줄인 -14/-7/-4 사례와 일치).
_W = dict(S=0.40, D=0.50, U=0.60, L=0.45, P=0.35, Q=0.40, B=0.40)
def est_width(text, spacing=0, scale=1.0, ratio=100):
    t = 0.0
    for ch in text:
        if '가' <= ch <= '힣' or ch in '○※…%': w = 1.0
        elif ch == ' ': w = _W['S']
        elif ch.isdigit(): w = _W['D']
        elif 'A' <= ch <= 'Z': w = _W['U']
        elif 'a' <= ch <= 'z': w = _W['L']
        elif ch in '.,:;\'’‘`|!·': w = _W['P']
        elif ch in '“”"': w = _W['Q']
        elif ch in '()[]{}': w = _W['B']
        elif '一' <= ch <= '鿿': w = 1.0          # 한자(美·中 등)
        else: w = 0.6
        t += w * scale * ratio / 100.0 + spacing / 100.0
    return t

# 글꼴별 폭 배율: 휴먼명조(5)=1.0 기준, 맑은 고딕(1)은 3~4% 넓음
#   (요약표 실제 결과: 롯데카드 칼럼 제목이 자간 -14%에서도 두 줄로 넘어간 사례 + 템플릿 5행으로 산출)
FONT_SCALE = {'1': 1.04}
# 자간만으로 안 될 때 장평(글자 폭)까지 줄이는 단계 — 장평은 최소 90%
RATIO_STEPS = (97, 95, 92, 90)

# 줄 폭(em) — 템플릿 줄 배치 정보의 horzsize/1000
AVAIL_TABLE_TITLE = 33.34   # 요약표 제목 칸
AVAIL_BLOCK_TITLE = 46.60   # ● 정보보안 뉴스 블록의 "N. 제목 [출처]" (굵게)
AVAIL_BLOCK_LINE  = 45.57   # ○ 요약 줄
MAX_TIGHTEN = 15            # 자간은 최대 -15%까지만 (그 이상은 글자가 겹쳐 보여서 차라리 두 줄)
SAFETY = 0.965             # 추정 오차 여유 — 템플릿에서 사람이 줄인 5곳과 같은 줄·거의 같은 값이 나오도록 맞춤
SAFETY_BOLD = 0.955
SAFETY_SCALED = 0.99         # 요약표(맑은 고딕): 배율 1.04 + 이 여유 → 실제로 두 줄 났던 -14% 사례보다 확실히 더 줄임

class CharPrBank:
    """header.xml 의 글자모양을 복제해 '자간만 다른' 새 글자모양을 만들어 줌 (같은 조합은 재사용)"""
    def __init__(self, header_root):
        self.root = header_root
        self.box = header_root.find('.//hh:charProperties', NS)
        self.by_id = {c.get('id'): c for c in self.box.findall('hh:charPr', NS)}
        self.cache = {}
        self.next_id = max(int(i) for i in self.by_id) + 1

    def base_spacing(self, cid):
        sp = self.by_id[cid].find('hh:spacing', NS)
        return int(sp.get('hangul')) if sp is not None else 0

    def is_bold(self, cid):
        return self.by_id[cid].find('hh:bold', NS) is not None

    def font_scale(self, cid):
        f = self.by_id[cid].find('hh:fontRef', NS)
        return FONT_SCALE.get(f.get('hangul') if f is not None else '', 1.0)

    def base_ratio(self, cid):
        r = self.by_id[cid].find('hh:ratio', NS)
        return int(r.get('hangul')) if r is not None else 100

    def with_spacing(self, cid, spacing, ratio=None):
        ratio = ratio or self.base_ratio(cid)
        if spacing == self.base_spacing(cid) and ratio == self.base_ratio(cid):
            return cid
        key = (cid, spacing, ratio)
        if key not in self.cache:
            new = copy.deepcopy(self.by_id[cid])
            nid = str(self.next_id); self.next_id += 1
            new.set('id', nid)
            sp = new.find('hh:spacing', NS)
            ra = new.find('hh:ratio', NS)
            for k in ('hangul', 'latin', 'hanja', 'japanese', 'other', 'symbol', 'user'):
                sp.set(k, str(spacing))
                if ra is not None: ra.set(k, str(ratio))
            self.box.append(new)
            self.by_id[nid] = new
            self.box.set('itemCnt', str(len(self.box.findall('hh:charPr', NS))))
            self.cache[key] = nid
        return self.cache[key]

def fit_one_line(p, text, avail, bank):
    """문단 p(글자 run 1개)를 한 줄에 들어가도록 필요한 만큼만 자간을 줄인다. 반환: 적용 자간"""
    run = next(r for r in p.findall('hp:run', NS) if r.find('hp:t', NS) is not None)
    cid = run.get('charPrIDRef')
    base, k = bank.base_spacing(cid), bank.font_scale(cid)
    # 폭 배율(k)을 따로 맞춘 글꼴(맑은 고딕)은 이미 넉넉하게 잡았으므로 여유는 조금만
    margin = SAFETY_BOLD if bank.is_bold(cid) else (SAFETY_SCALED if k != 1.0 else SAFETY)
    limit = avail * margin
    if est_width(text, base, k) <= limit:
        return base
    # 1) 자간만 줄여 보기 (-1 ~ -15%)
    choice = None
    for s in range(base - 1, -MAX_TIGHTEN - 1, -1):
        if est_width(text, s, k) <= limit:
            choice = (s, 100); break
    # 2) 그래도 넘치면 장평도 같이 (97 → 90%), 자간은 -8%부터
    if choice is None:
        for r in RATIO_STEPS:
            for s in range(-8, -MAX_TIGHTEN - 1, -1):
                if est_width(text, s, k, r) <= limit:
                    choice = (s, r); break
            if choice: break
    if choice is None:
        # 최대로 줄여도 한 줄에 안 들어가는 긴 제목(예: 80자짜리 브리핑 제목)은 줄이지 않고 그냥 두 줄로 둔다
        # — 꽉 눌러 놓고 결국 두 줄이 되는 게 가장 보기 나쁨
        return base
    s, r = choice
    run.set('charPrIDRef', bank.with_spacing(cid, s, r))
    return (s, r) if r != 100 else s

def fix_no_fill(header_root):
    """'채우기 없음' 배경의 무늬색이 불투명 검정(#000000)으로 저장된 테두리/배경(borderFill 19 등)을
    투명(#FF000000)으로 맞춘다. 원본에선 줄 배치 캐시 덕에 안 보였지만, 캐시를 지우고 한글이 다시
    계산하면 요약표 제목·날짜·출처 칸 글자 위에 검은 막대로 그려졌음."""
    HC = 'http://www.hancom.co.kr/hwpml/2011/core'
    for wb in header_root.iter('{%s}winBrush' % HC):
        if wb.get('faceColor') == 'none' and not (wb.get('hatchColor') or '').upper().startswith('#FF'):
            wb.set('hatchColor', '#FF000000')

def set_keep_word(header_root):
    """문단모양(Alt+T) > 줄 나눔 기준: 한글 단위 '어절', 영어 단위 '단어'.
    ※ 실제 한글 동작으로 확인한 값 (이름만 보면 헷갈림):
      - 한글: breakNonLatinWord="BREAK_WORD" = 어절  (KEEP_WORD 이면 '정|부기관'처럼 글자 단위로 끊김.
              사용자가 Alt+T로 어절로 바꿔 저장한 파일에서 BREAK_WORD 확인)
      - 영어·숫자: breakLatinWord="KEEP_WORD" = 단어 유지  (BREAK_WORD 이면 'A|I', 'NI|H', '202|4',
              '2,|000'처럼 쪼개짐 — 같은 파일에서 확인)"""
    for b in header_root.iter('{%s}breakSetting' % HH):
        b.set('breakNonLatinWord', 'BREAK_WORD')
        b.set('breakLatinWord', 'KEEP_WORD')

ROOT_DIR = Path(__file__).resolve().parent.parent
TEMPLATE = Path(os.environ.get('TEMPLATE') or ROOT_DIR / 'template.hwpx')
KST = timezone(timedelta(hours=9))
MAX_ARTICLES = 5

# ── 문단 유틸 ────────────────────────────────────────────
def text_of(p):
    return ''.join(p.xpath('./hp:run/hp:t/text()', namespaces=NS))

def drop_layout_cache(p):
    """글자를 바꾼 문단은 줄 배치 캐시(linesegarray)를 지워 한글이 다시 계산하게 함
    (안 지우면 예전 글자 길이 기준 줄바꿈이 남아 글자가 겹쳐 보일 수 있음)"""
    for ls in p.findall('hp:linesegarray', NS):
        p.remove(ls)

def set_text(p, text, run_index=-1):
    """문단의 글자 run 하나에 text를 넣고, 나머지 글자 run의 글자는 비운다.
    run_index: 글자를 넣을 run (기본: 글자가 있는 마지막 run — 제목 앞 '  ' 들여쓰기 run 보존용)"""
    runs = [r for r in p.findall('hp:run', NS)]
    trun = [r for r in runs if r.find('hp:t', NS) is not None] or runs
    target = trun[run_index]
    for r in trun:
        if r is target:
            continue
        if r is not trun[0] or run_index == 0:
            for t in r.findall('hp:t', NS):
                r.remove(t)
    for t in target.findall('hp:t', NS):
        target.remove(t)
    t = etree.SubElement(target, q('t'))
    t.text = text
    drop_layout_cache(p)
    return p

def clone(p):
    c = copy.deepcopy(p)
    drop_layout_cache(c)
    return c

def subList_of_cell(tbl_para, row=0, col=0):
    for tc in tbl_para.iter(q('tc')):
        a = tc.find('hp:cellAddr', NS)
        if a is not None and a.get('rowAddr') == str(row) and a.get('colAddr') == str(col):
            return tc.find('hp:subList', NS)
    raise KeyError((row, col))

def clean(s):
    return re.sub(r'\s+', ' ', (s or '').replace('\\n', ' ')).strip()

# ── 본문 ─────────────────────────────────────────────────
def build(result, out_path, nl_date=None):
    nl_date = nl_date or datetime.now(KST).date()
    arts = (result.get('articles') or [])[:MAX_ARTICLES]
    if not arts:
        raise SystemExit('기사가 없습니다')

    with zipfile.ZipFile(TEMPLATE) as z:
        files = {i.filename: z.read(i.filename) for i in z.infolist()}
        order = [i.filename for i in z.infolist()]

    root = etree.fromstring(files['Contents/section0.xml'])
    header = etree.fromstring(files['Contents/header.xml'])
    bank = CharPrBank(header)
    set_keep_word(header)
    fix_no_fill(header)
    tightened = []
    tops = root.findall('hp:p', NS)          # 최상위 문단 5개 = 표 5개
    t_head, _t1, t_sum, _t3, t_main = tops[:5]

    # 1) 머리말 날짜: [ ’26. 6. 1. ] → 실행일
    cell = subList_of_cell(t_head, 0, 2)
    date_p = cell.findall('hp:p', NS)[1]
    runs = [r for r in date_p.findall('hp:run', NS) if r.find('hp:t', NS) is not None]
    runs[-1].find('hp:t', NS).text = f"{nl_date.year % 100:02d}. {nl_date.month}. {nl_date.day}. ]"
    drop_layout_cache(date_p)

    # 2) 요약표 (행 1~5)
    tbl = t_sum.find('.//hp:tbl', NS)
    trs = tbl.findall('hp:tr', NS)
    for i in range(1, 6):
        tr = trs[i]
        if i > len(arts):
            tbl.remove(tr)
            continue
        a = arts[i - 1]
        vals = [str(i), clean(a.get('title')), clean(a.get('date')), clean(a.get('source'))]
        for tc in tr.findall('hp:tc', NS):
            col = int(tc.find('hp:cellAddr', NS).get('colAddr'))
            p = tc.find('hp:subList', NS).find('hp:p', NS)
            set_text(p, vals[col], run_index=0)
            # 손으로 자간을 좁혀 둔 칸(글자모양 32·34)은 기본(12)으로 되돌림
            for r in p.findall('hp:run', NS):
                if r.get('charPrIDRef') in ('32', '34'):
                    r.set('charPrIDRef', '12')
            if col == 1:   # 제목 칸은 한 줄에 들어가도록 자간 자동 조정
                s = fit_one_line(p, vals[col], AVAIL_TABLE_TITLE, bank)
                if s: tightened.append(('요약표', s, vals[col]))
    tbl.set('rowCnt', str(1 + len(arts)))

    # 3) 큰 셀: 요약 블록 + 고정 안내문 + 본문
    sl = subList_of_cell(t_main, 0, 0)
    ps = sl.findall('hp:p', NS)
    # 템플릿 위치(위 docstring 구조) — 표식 문구로 찾아서 고정 위치에 덜 의존
    idx_notice = next(i for i, p in enumerate(ps) if text_of(p).startswith('※ 웹사이트'))
    idx_bonmun = next(i for i, p in enumerate(ps) if text_of(p).strip() == '[본문]')
    first_title = next(i for i, p in enumerate(ps) if i > idx_bonmun and re.match(r'\s*1\.\s', text_of(p)))
    first_src   = next(i for i, p in enumerate(ps) if i > first_title and text_of(p).startswith('[원문기사]'))

    # 복제용 원본 문단
    lead_blank = ps[0]                      # 셀 맨 앞 빈 줄
    S_TITLE, S_SP1, S_LINE, S_SP2, S_SP3 = ps[1], ps[2], ps[3], ps[4], ps[6]   # 1번 기사 요약 블록
    fixed_mid  = ps[idx_notice - 1: first_title]   # 마지막 요약 뒤 빈 줄 ~ 안내문 ~ [본문] ~ '  '
    B_TITLE    = ps[first_title]
    B_BLANK    = ps[first_title + 1]
    B_PARA     = next(p for p in ps[first_title + 2:first_src]
                      if text_of(p) and all(r.get('charPrIDRef') == '11' for r in p.findall('hp:run', NS)))
    B_SRC      = ps[first_src]
    B_SEP      = ps[first_src + 1]          # 기사 사이 '  ' 줄

    new = [lead_blank]
    for i, a in enumerate(arts, 1):
        title_line = f"{i}. {clean(a.get('title'))} [{clean(a.get('source'))}]"
        lines = [(S_TITLE, title_line, AVAIL_BLOCK_TITLE),
                 (S_LINE, '○ ' + clean(a.get('summary1')), AVAIL_BLOCK_LINE),
                 (S_LINE, '○ ' + clean(a.get('summary2')), AVAIL_BLOCK_LINE)]
        for k, (tmpl, txt, avail) in enumerate(lines):
            p = set_text(clone(tmpl), txt, run_index=0)
            s = fit_one_line(p, txt, avail, bank)
            if s: tightened.append(('요약블록', s, txt))
            new.append(p)
            if k == 0: new.append(clone(S_SP1))
            if k == 1: new.append(clone(S_SP2))
        if i < len(arts):
            new.append(clone(S_SP3))
    new.extend(fixed_mid)

    for i, a in enumerate(arts, 1):
        if i > 1:
            new.append(clone(B_SEP))
        new.append(set_text(clone(B_TITLE), f"{i}. {clean(a.get('title'))} [{clean(a.get('source'))}]"))
        new.append(clone(B_BLANK))
        body = (a.get('body') or '').replace('\\n', '\n')
        for para in [clean(x) for x in body.split('\n')]:
            if not para:
                continue
            new.append(set_text(clone(B_PARA), para, run_index=0))
            new.append(clone(B_BLANK))
        new.append(set_text(clone(B_SRC), f"[원문기사] {clean(a.get('url'))}", run_index=0))

    for p in ps:
        sl.remove(p)
    for p in new:
        sl.append(p)

    # 크기가 바뀐 표(요약표·본문 셀)를 담은 바깥 문단의 줄 배치 캐시도 지워서 한글이 높이를 다시 계산하게 함
    drop_layout_cache(t_sum)
    drop_layout_cache(t_main)

    files['Contents/section0.xml'] = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)
    files['Contents/header.xml']   = etree.tostring(header, xml_declaration=True, encoding='UTF-8', standalone=True)
    for where, s, txt in tightened:
        desc = f"자간 {s[0]:+d}% 장평 {s[1]}%" if isinstance(s, tuple) else f"자간 {s:+d}%"
        print(f"  {desc}  [{where}] {txt[:40]}")

    # 4) 미리보기 텍스트(탐색기 미리보기용)도 새 내용으로
    prv = [f"<정보보안 뉴스레터><[ ’{nl_date.year % 100:02d}. {nl_date.month}. {nl_date.day}. ]>",
           "<● 정보보안 뉴스 요약>"] + \
          [f"<{i}><{clean(a.get('title'))}><{clean(a.get('date'))}><{clean(a.get('source'))}>" for i, a in enumerate(arts, 1)]
    if 'Preview/PrvText.txt' in files:
        files['Preview/PrvText.txt'] = '\r\n'.join(prv).encode('utf-8')

    # 5) 다시 묶기 — mimetype 은 맨 앞·무압축이어야 한글이 HWPX로 인식
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, 'w') as z:
        for name in order:
            ct = zipfile.ZIP_STORED if name == 'mimetype' else zipfile.ZIP_DEFLATED
            z.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), files[name], compress_type=ct)
    return out_path

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    res_path = Path(sys.argv[1])
    result = json.loads(res_path.read_text(encoding='utf-8'))
    d = os.environ.get('NL_DATE')
    nl_date = datetime.strptime(d, '%Y-%m-%d').date() if d else None
    out = build(result, sys.argv[2], nl_date)
    print(f"✅ {out} 생성 ({len((result.get('articles') or [])[:MAX_ARTICLES])}건)")
    # index.html 이 받아갈 파일 경로를 결과 JSON에 기록 (저장소 루트 기준 상대경로)
    try:
        result['hwpx'] = str(Path(out).resolve().relative_to(ROOT_DIR)).replace('\\', '/')
    except ValueError:
        result['hwpx'] = str(out)
    res_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')

if __name__ == '__main__':
    main()
