# 사이버시큐 — 정보보안 뉴스 클리핑

보안 뉴스를 30분마다 자동으로 모으고, 근무자가 기사 5개를 고르면 **원문 크롤링 → Claude 요약·번역 → 한글(HWPX) 뉴스레터**까지 자동으로 만들어 주는 시스템입니다.
지정한 기관·위협그룹 관련 기사는 두레이로 바로 알림을 보냅니다.

```
[수집]  30분마다  RSS·네이버 → data/feeds.json (최대 500건, 기사마다 크롤링 지원 여부 표시)
                              └→ 기관·위협그룹 매칭 기사 → 두레이 웹훅
[선택]  index.html(로컬)  기사 최대 5개 선택 → 생성
[생성]  GitHub Actions    원문 크롤링 → 본문 정리 → Claude 요약·번역 → 뉴스레터 HWPX
[받기]  index.html        완성된 HWPX 다운로드
```

---

## 파일 구조

```
fcscNews/
├── index.html                  ← 메인 화면 ★저장소에 올리지 않음 (GitHub 토큰 포함, 로컬 file:// 전용)
├── template.hwpx               ← 뉴스레터 한글 양식 (채워진 예시 뉴스레터 형태)
├── data/
│   ├── feeds.json              ← 수집된 기사 (자동)
│   ├── crawl_test_result.json  ← 매체별 원문 크롤링 지원 여부 (크롤링 테스트 결과)
│   ├── result_{요청ID}.json    ← 요약 결과 (자동)
│   ├── out/newsletter_{날짜}_{요청ID}.hwpx ← 완성된 뉴스레터 (자동, 30일 지나면 삭제)
│   ├── webhook_sent.json       ← 웹훅 보낸 기록 (중복 방지)
│   ├── last_heartbeat.txt / last_run.txt
├── scripts/
│   ├── collect.py              ← 뉴스 수집 + 크롤링 지원 표시 + 두레이 웹훅
│   ├── summarize.py            ← 원문 크롤링 + 본문 정리 + Claude 요약·번역
│   ├── build_hwpx.py           ← 요약 결과 + template.hwpx → 뉴스레터 HWPX
│   ├── crawl_test.py           ← 매체별 크롤링 성공률 측정 (도구)
│   └── requirements.txt
└── .github/workflows/
    ├── collect.yml             ← 30분마다 수집 (+ 외부 cron-job.org가 강제 실행)
    ├── summarize.yml           ← 선택 기사 → 요약 → HWPX 생성 (index.html이 실행)
    └── crawl_test.yml          ← 크롤링 테스트 (수동)
```

---

## 초기 설정

### GitHub Secrets (Settings → Secrets and variables → Actions)
| 이름 | 용도 |
|---|---|
| `ANTHROPIC_API_KEY` | Claude 요약·번역 |
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | 네이버 뉴스 검색 API |
| `WEBHOOK_URL` | 두레이 Incoming Hook (알림·하트비트) |

### 외부 스케줄러 (cron-job.org)
GitHub 자체 예약 실행은 자주 밀리거나 빠져서, cron-job.org가 30분마다 `collect.yml`을 강제 실행합니다.
- `POST https://api.github.com/repos/JmDev77/fcscNews/actions/workflows/collect.yml/dispatches`
- 헤더: `Authorization: Bearer <PAT>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28` / 본문: `{"ref":"main"}`
- **PAT가 만료되면 자동 수집이 멈춥니다.** (Fine-grained, 이 저장소 Actions 읽기·쓰기 권한만)

### index.html
- GitHub 토큰이 코드에 들어 있어서 **절대 저장소에 올리지 않습니다.** 근무 PC에서 `file://`로 엽니다.
- 과거 토큰이 노출돼 GitHub가 자동 폐기한 적이 있습니다.

---

## 1. 수집 — `collect.py` (30분마다)

| 채널 | 출처 | 비고 |
|---|---|---|
| 자체개발 | 보안뉴스·데일리시큐 RSS, 해외 5곳 RSS, 네이버 검색 | 약 450건 |
| 쉴더스 | security-brief RSS | 최대 50건 먼저 보장 |
| 정보원 | 외부 feeds.json (HarkjinDev) | 저장 안 함 — index.html이 직접 조회 |

- **보관**: 최근 72시간, 최대 500건. 제목 유사도로 중복 제거.
- **크롤링 지원 표시**: 기사마다 `crawl` 값(`지원` / `불안정` / `미지원` / `미측정`)을 붙입니다. 기준은 `data/crawl_test_result.json`이고, index.html의 "크롤링 지원" 탭이 이 값으로 기사를 거릅니다. 테스트에 안 나온 새 매체는 `미측정`이라 탭에 안 들어갑니다.
- **키워드** (collect.py 상단, 주석 참고)
  - `NAVER_KEYWORDS` 네이버 검색어 (공백으로 엮으면 AND 검색 — 예: `다크웹 유출`)
  - `EXCLUDE_KEYWORDS` 있으면 버림 (연예·게임·주식 등)
  - `SECURITY_KEYWORDS` 보안 기사 필터 + 웹훅 조건
  - `KEYWORD_TAGS` 카드 배지 (표시용)

### 두레이 웹훅 (제목만 검사)
- **조건 A**: 기관명(`MONITOR_ORGS`) + 보안 키워드 → 전송
- **조건 B**: 위협그룹명(`THREAT_GROUPS`) → 보안 키워드 없이도 전송
- 짧은 약칭(`한은`, `한미`)은 단어 단위로만 매칭 (`SHORT_ORG_ALIASES`) — "신한은행"에는 안 걸림
- 중복 방지: 보낸 링크 기록 + 제목 유사도 0.80 (최근 72시간)
- 하트비트: 2시간마다 "검사 N건 · 기관매칭 M건" (끄려면 collect.yml env에 `HEARTBEAT: off`)

---

## 2. 요약 — `summarize.py` (근무자가 생성 누를 때)

1. **원문 크롤링**: JSON-LD → 매체 전용 셀렉터 → 공통 셀렉터 → 텍스트 밀도 분석 → `<p>` 전체 순으로 시도
   - 광고·관련기사 영역을 지울 때 **본문을 감싼 영역은 지우지 않음** (예전엔 class에 `ad`만 들어가도 지워서 본문이 통째로 사라지는 매체가 많았음)
   - 본문 선택자 추가: `BODY_SELECTORS`(공통) / `DOMAIN_SELECTORS`(특정 매체 전용)
2. **본문 정리 (코드로 처리)**
   - 기자 서명·이메일 제거: `[보안뉴스 홍길동 기자]`, `(서울=연합뉴스) 홍길동 기자 =`, `<홍길동 기자 a@b.com>` 등
   - 저작권 고지, 제보 안내, 「열린보도원칙」, 사진 출처, 해시태그 줄 제거
   - **숫자 천 단위 콤마**: `1000원 → 1,000원`, `515333건 → 515,333건`
     (연도·CVE 번호·전화번호·버전·URL·우편번호·영문 모델명은 그대로)
3. **Claude 요약**: 한줄 요약 2개(40자 이내, ~해/~돼 체), 맞춤법·따옴표 교정, 본문은 축약 금지
   - **영문 기사는 한글로 번역** (본문이 영문 위주면 자동 판단)
   - 한글 기사는 Claude가 본문을 60% 미만으로 줄이면 원문으로 되돌림 (번역 기사는 이 검사 제외)
4. 결과 → `data/result_{요청ID}.json`

## 3. 뉴스레터 — `build_hwpx.py`

`template.hwpx`(채워진 예시 뉴스레터)의 문단을 복제해서 **서식은 그대로, 글자만 바꿉니다.**

- **머리말 날짜**: 실행한 날(한국 시각)로 자동 변경. 호수는 양식 그대로
- **요약표 5행** / **기사별 제목 + ○ 요약 2줄** / **[본문]** (기사가 5건보다 적으면 그만큼 줄어듦)
- **1페이지 한 줄 맞춤**: 요약표 제목, "N. 제목 [출처]", ○ 요약 줄이 두 줄로 넘어가지 않도록 필요한 만큼만 자간을 줄임 (최대 -15%, 그래도 길면 장평 90%까지)
  - 글자 폭은 양식에 저장된 한글의 실제 줄바꿈 위치로 맞춘 추정값 (`est_width`, `FONT_SCALE`, `SAFETY*`)
- **줄 나눔 기준**: 한글 "어절", 영어·숫자 "단어"
  - ⚠ HWPX 값 이름이 헷갈림: 한글 어절 = `breakNonLatinWord="BREAK_WORD"`, 영어 단어 = `breakLatinWord="KEEP_WORD"` (실제 한글 화면으로 확인)
- 결과 → `data/out/newsletter_{날짜}_{요청ID}.hwpx`, 경로는 결과 JSON의 `hwpx` 항목에 기록

> 양식을 바꿀 때: `template.hwpx`를 **실제로 채운 예시 뉴스레터** 형태로 저장하면 됩니다.
> 스크립트는 "※ 웹사이트", "[본문]", "[원문기사]" 문구와 1번 기사 블록 위치로 각 영역을 찾습니다.

---

## 4. 크롤링 테스트 — `crawl_test.py` (수동)

매체별로 최근 기사 3건씩 실제로 크롤링해 보고 `지원 / 불안정 / 미지원`을 판정합니다.
결과는 `data/crawl_test_result.json`에 저장되고, Actions 실행 화면에 표로 나옵니다.

- **Actions → 크롤링 테스트 → Run workflow**
  - `only_failed = on`: 지난 결과의 실패·불안정 매체만 다시 시험 (빠름)
- 실패 사유: `403` 등(사이트가 접속 차단 — 코드로 해결 어려움) / `추출실패`(본문 위치를 못 찾음 — 결과표의 **셀렉터 힌트**를 `summarize.py`에 추가하면 해결)
- 본문을 자바스크립트로 그리는 매체(노컷뉴스, 오마이뉴스, JTBC 등)는 크롤링 불가
- ⚠ 테스트를 다시 돌리면 결과 파일이 바뀌고, "크롤링 지원" 탭 구성도 그에 따라 바뀝니다

---

## 자주 하는 유지보수

| 하고 싶은 것 | 고칠 곳 |
|---|---|
| 네이버 검색어 추가 | `collect.py` `NAVER_KEYWORDS` |
| 엉뚱한 기사 걸러내기 | `collect.py` `EXCLUDE_KEYWORDS` |
| RSS 추가·교체 | `collect.py` `FEEDS` |
| 알림 기관 추가 | `collect.py` `MONITOR_ORGS` (짧은 약칭은 `SHORT_ORG_ALIASES`에도) |
| 알림 위협그룹 추가 | `collect.py` `THREAT_GROUPS` (한글·영문 표기 함께) |
| 특정 매체 본문이 안 뽑힘 | 크롤링 테스트 결과의 셀렉터 힌트 → `summarize.py` `DOMAIN_SELECTORS` |
| 본문 끝에 지워야 할 문구가 남음 | `summarize.py` `_TAIL_START` / `_DROP_LINE` |
| 뉴스레터 양식 변경 | `template.hwpx` 교체 |

### 자동 수집이 멈췄을 때
1. cron-job.org 실행 기록 확인 → 실패면 PAT 만료 확인
2. Actions 탭에서 "뉴스 수집"이 활성 상태인지 확인
3. 특정 매체만 0건 → 로그의 "응답 미리보기"로 차단·주소 변경 확인 (보안뉴스 RSS는 주소가 바뀐 적 있음)
4. 하트비트가 안 오면 수집 자체가 안 도는 것 → 1~2번부터

### 알림 재전송 테스트
`data/webhook_sent.json`을 비우면 다음 수집 때 최근 72시간 매칭 기사가 다시 전송됩니다 (한꺼번에 몰려오니 주의).

---

## 알려진 이슈
- **BleepingComputer RSS 403**: 수집은 막혀 있음 (기사 원문 크롤링은 정상)
- **접속 차단 매체**: 뉴스1, DarkReading, CISA 등은 GitHub 서버에서의 접속을 막아 원문 크롤링 불가
- **THREAT_GROUPS의 "다크웹"·"코니"**: 그룹명이 아니거나 짧아서 오탐 가능 — 알림이 과하면 제거 검토
- **진행 중**: index.html "크롤링 지원" 탭 + 완성된 HWPX 다운로드 연결
