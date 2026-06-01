# POWERDRIVE RANK 🏓
> **한양대학교 ERICA 탁구 동아리 '탁우회' 부수 검색 및 실시간 전력 대시보드 시스템**

POWERDRIVE RANK는 대학 탁구 동아리 운영진과 부원들이 모바일 및 웹 환경에서 부원들의 이름, 부수, 사용 라켓(전형) 정보를 실시간으로 조회하고 동아리 전체의 통계 현황을 손쉽게 모니터링할 수 있도록 돕는 독립형 웹 서비스입니다.

---

## ✨ 주요 기능 및 특징
1. **로그인 없는 1초 부수 검색**: 동아리원들이 가입 및 로그인 단계 없이 링크 하나로 편리하게 부원을 검색합니다.
2. **실시간 통계 대시보드**: 
   * **부수 분포도**: collections.Counter 집계를 통해 각 부수별 인원 비율을 막대 그래프로 시각화합니다.
   * **라켓 구성비**: 쉐이크핸드와 펜홀더 비율을 직관적인 카드로 비교 분석합니다.
   * **그룹화 목록**: 부수 오름차순 및 이름순 정렬을 제공하여 대회 대진표 및 경기 매칭 시 운영 리소스를 대폭 줄여줍니다.
3. **비용 제로 데이터 소스**: 구글 스프레드시트(Google Sheets API)를 데이터베이스 소스로 사용하여 유지보수 비용을 0원으로 실현했습니다.
4. **데이터 정규화 (Normalization) 전처리**:
   * 시트 입력 오타(예: `세이크`, `팬홀더`, `3` 등)를 백엔드에서 자동으로 감지해 `쉐이크`, `펜홀더`, `3부` 등으로 실시간 보정 정제하여 데이터의 신뢰성을 보장합니다.
5. **회원 명단 일괄 업로드 매니저 (GUI & CLI 듀얼 지원)**:
   * 운영진을 위한 회원 데이터 일괄 등록 도구(`upload_members.py`)가 내장되어 있으며, GUI(데스크톱 창) 및 CLI 모드를 모두 지원합니다.

---

## 📂 프로젝트 폴더 구조
(보안 및 개인 정보 보호를 위해 `credentials.json`, `data/` 폴더, `venv/` 폴더 등은 `.gitignore`에 등록되어 깃허브 원격 저장소 업로드에서 완전히 제외됩니다.)

```text
pingpong_rank/
├── app.py                  # Flask 웹 애플리케이션 메인 소스
├── upload_members.py       # 회원 데이터 일괄 업로드 유틸리티 (GUI/CLI)
├── requirements.txt        # 설치가 필요한 외부 패키지 의존성 목록
├── .gitignore              # git 제외 설정 파일
└── templates/
    ├── base.html           # 공통 레이아웃 (Powerdrive 다크 테마)
    ├── index.html          # 메인 검색 페이지
    ├── stats.html          # 실시간 통계 대시보드 페이지
    └── proposal.html       # 1-Page 프리미엄 제안서 슬라이드 페이지
```

---

## 💻 로컬 실행 방법

### 1. 패키지 설치 및 가상환경 설정
```bash
# 가상환경 생성 및 활성화
python3 -m venv venv
source venv/bin/activate

# 필수 라이브러리 설치 (Flask, gspread, oauth2client, gunicorn)
pip install -r requirements.txt
```

### 2. 구글 시트 연동 설정
1. Google Cloud Console에서 프로젝트를 만들고 **Google Sheets API**와 **Google Drive API**를 활성화합니다.
2. 서비스 계정의 JSON 키 파일을 다운로드하여 이름을 **`credentials.json`**으로 변경한 뒤 프로젝트 루트 폴더에 넣습니다.
3. 구글 드라이브에 **`탁우회_명단`**이라는 이름의 시트를 만든 후, `credentials.json`에 있는 `client_email`로 **뷰어** 혹은 **편집자** 권한으로 공유합니다.

### 3. 로컬 서버 시작
```bash
# Flask 로컬 웹 서버 가동
python app.py
```
서버가 켜지면 브라우저에서 [http://127.0.0.1:5001](http://127.0.0.1:5001)로 접속할 수 있습니다.

---

## 📥 회원 명단 일괄 업로드 도구 사용법

### 1. 데스크톱 GUI 모드 (기본값)
터미널에서 인수 없이 실행하면 마우스로 파일을 선택하고 업로드할 수 있는 GUI 창이 뜹니다.
```bash
python upload_members.py
```

### 2. CLI 명령행 모드
원격 서버나 터미널에서 즉시 일괄 덮어쓰기(`overwrite`) 또는 이어붙이기(`append`)를 실행합니다.
```bash
# CSV 파일을 이용해 구글 시트 데이터 덮어쓰기 (기본값)
python upload_members.py "data/파일이름.csv" --mode overwrite

# 기존 구글 시트 하단에 데이터 누적하기
python upload_members.py "data/파일이름.csv" --mode append
```

---

## ☁️ Render 클라우드 배포 정보
본 서비스는 Render Web Service를 통해 자동 배포되도록 최적화되어 있습니다.
* **Build Command**: `pip install -r requirements.txt`
* **Start Command**: `gunicorn app:app`
* **Environment**: 
  * `Environment Variables` ➡️ `GOOGLE_APPLICATION_CREDENTIALS` = `/etc/secrets/credentials.json`
  * `Secret Files` ➡️ 파일명 `credentials.json`로 생성 후 서비스 키 복사-붙여넣기
