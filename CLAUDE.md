# POWERDRIVE HANYANG (탁우회 사이트)

한양대 ERICA 탁구 동아리 사이트. Flask + Jinja + Tailwind CDN, 데이터는 구글 시트, 배포는 Vercel(main 자동 배포). 대화는 한국어로.

## 지금 진행 중
- 토너먼트 기능(코드·주소 이름은 league, 예전 화면 이름은 '리그전'): 브랜치 `feature/league`, 아직 main에 안 합침. 화면에는 '토너먼트'라고 쓴다('리그전'은 대회 이름). **먼저 `docs/league-handoff.md`를 읽을 것** (구조도·결정 사항·API·테스트 방법·남은 일).

## 작업 규칙
- 커밋 전 테스트: `~/.cache/claude-powerdrive-tests/`의 가짜 시트 서버(포트 5002)로 `league-api-test.py`, `league-ui-test.js`, `check.js`를 돌린다. 템플릿을 고치면 서버를 재시작한다.
- 실서비스 점수판에서 저장 버튼을 누르지 않는다(실제 경기기록 오염). 부수표 시트1은 편집하지 않는다. Vercel 봇 검문은 우회하지 않는다.
- 디자인: 세로 막대·그라데이션·뱃지 같은 장식 대신 타이포·정렬로 위계. 중복 링크 금지. 흰 카드·회색 선·빨강 강조, 다크 모드는 `rubber` 색.
- 환경변수: `SHEET_ID`(부수표), `MEMBER_WORKSHEET`(시트2), `RECORDS_SHEET_NAME`(탁우회_명단), `LEAGUE_SHEET_NAME`/`LEAGUE_SHEET_ID`(탁우회 토너먼트), `SHEET_CACHE_TTL`.
