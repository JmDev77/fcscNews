# 사이버시큐 - 정보보안 뉴스 클리핑

## 파일 구조
```
fcscNews/
├── index.html                    ← 메인 화면 (GitHub Pages)
├── template.hwpx                 ← 뉴스레터 양식 (직접 업로드)
├── data/
│   ├── feeds.json                ← 수집된 뉴스 (자동 생성)
│   └── result_*.json             ← 요약 결과 (자동 생성)
├── scripts/
│   ├── collect.py                ← RSS 수집
│   ├── summarize.py              ← 크롤링 + Claude 요약
│   └── requirements.txt
└── .github/workflows/
    ├── collect.yml               ← 매 1시간 수집
    └── summarize.yml             ← 선택 기사 크롤링+요약
```

## 초기 설정

### 1. GitHub Secrets 등록
Settings → Secrets and variables → Actions

| 이름 | 값 |
|------|-----|
| `ANTHROPIC_API_KEY` | sk-ant-... |
| `NAVER_CLIENT_ID` | 네이버 API ID |
| `NAVER_CLIENT_SECRET` | 네이버 API Secret |

### 2. index.html 토큰 입력
```js
const GH_TOKEN = 'YOUR_TOKEN_HERE';  // ← GitHub PAT 입력
```

### 3. template.hwpx 업로드
뉴스레터 양식 파일을 저장소 루트에 업로드

### 4. GitHub Pages 활성화
Settings → Pages → main / (root)

## 접속
`https://JmDev77.github.io/fcscNews`

## 사용법
1. 기사 리스트에서 최대 5개 선택
2. 📄 뉴스레터 버튼 → 생성 클릭
3. GitHub Actions가 크롤링 + Claude 요약 실행 (1~2분)
4. 완료되면 hwpx 자동 다운로드
