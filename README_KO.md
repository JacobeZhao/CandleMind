<div align="center">
  <img src="docs/assets/candlemind-logo.png" alt="CandleMind Logo" width="180">
  <h1>CandleMind</h1>
  <p><strong>Binance Futures를 위한 오픈 소스 추세 추종 연구 및 자동 실행 플랫폼</strong></p>
  <p>
    <a href="README.md">简体中文</a> |
    <a href="README_EN.md">English</a> |
    <a href="README_JA.md">日本語</a> |
    <strong>한국어</strong>
  </p>
  <p>
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2ea44f.svg" alt="MIT License"></a>
    <img src="https://img.shields.io/badge/Python-3.12-3776AB.svg" alt="Python 3.12">
    <img src="https://img.shields.io/badge/FastAPI-API-009688.svg" alt="FastAPI">
    <img src="https://img.shields.io/badge/React-18-61DAFB.svg" alt="React 18">
  </p>
</div>

> [!WARNING]
> CandleMind는 기술 연구와 교육만을 위한 프로젝트이며 투자 자문이 아닙니다. 자동화 전략은 Binance Futures 테스트넷 또는 메인넷에 주문을 전송할 수 있습니다. 기본 환경은 테스트넷이며 메인넷은 서버에서 기본적으로 비활성화됩니다. 과거 연구, 예시 수치 및 백테스트는 실제 성과나 미래 수익을 보장하지 않습니다.

## 개요

CandleMind는 FastAPI와 React를 기반으로 실시간 시장 데이터, 추세 분석, 전략 설정, 거래소 주문 실행 및 계정 통계를 하나의 작업 공간에 통합합니다. **CandleMind 추세 전략**을 중심으로 시장 화면에 가용 뷰포트를 채우는 크기 조절형 캔들 차트와 실시간 AI 시장 도우미를 제공하며, 재현 가능한 오프라인 연구 인프라도 유지합니다.

설정 화면에는 전역 거래소 선택기가 있습니다. 현재 구현된 거래소는 **Binance Futures**뿐입니다. OKX, Bybit, Gate.io 및 A주는 향후 연동을 위한 미연결 자리 표시자이며, 선택해도 Binance 시장, 계정 또는 거래 요청을 전송하지 않습니다.

## 주요 기능

| 기능 | 설명 |
| --- | --- |
| 실시간 시장 | Binance WebSocket 시세, 캔들, 메인 차트 지표 및 뷰포트 반응형 크기 조절 작업 영역 |
| AI 시장 도우미 | 마감된 다중 주기 캔들을 지속적으로 분석하고 사용자 대화 지원 |
| 전략 런타임 | 선택한 종목과 거래 네트워크에 연결되는 세 가지 자동화 전략 |
| 주문 및 계정 | 미체결 주문, 체결, 주문 내역, 수익, 승률 및 손익비 통계 |
| 오프라인 연구 | 데이터 검증, 전략 평가 및 강화학습 연구 계약 |
| 거래 안전 | 테스트넷 우선, 메인넷 이중 제어, 수량 검증 및 멱등 주문 로그 |

공개 애플리케이션은 개요, 시장, 주문, 전략, 설정의 다섯 화면으로 구성됩니다. 내부 평가 기능은 연구용으로 유지되지만 공개 백테스트 화면이나 `/api/backtest/*` API는 제공하지 않습니다.

설정 화면을 여는 즉시 출구 IP를 감지하고 이후 1분마다 자동 갱신하며, 다음 감지가 완료될 때까지 이전 결과를 유지합니다. 전역 거래소 선택은 개요, 시장, 주문 등의 업무 화면에 일관되게 적용되며, 상단 바에서 종목을 전역으로 선택하고 현재 화면을 새로 고칠 수 있습니다.

## 기술 구성

| 계층 | 기술 | 위치 |
| --- | --- | --- |
| 백엔드 API | Python 3.12, FastAPI, Pandas | `backend/app/` |
| 전략 및 평가 | 자동화 전략 런타임, Backtrader 오프라인 평가 | `backend/app/strategies/` |
| 프런트엔드 | React 18, Vite, Tailwind CSS | `frontend/src/` |
| 배포 | Docker Compose, Nginx | `docker-compose.yml` |
| 외부 데이터 | 캔들, 실행 상태 및 연구 보고서 | `G:/CandleMind/CandleMind_data` |

운영 시장 데이터, 데이터베이스, 비밀 정보 및 생성 보고서는 Git에 저장하지 않습니다. 데이터 규칙은 [`docs/DATA_LAYOUT.md`](docs/DATA_LAYOUT.md)를 참고하십시오.

## 빠른 시작

### Docker Compose

Docker Desktop을 준비한 뒤 실행합니다.

```powershell
Copy-Item .env.example .env
powershell -ExecutionPolicy Bypass -File ops/dev-compose.ps1
```

실행 후 웹 <http://localhost:3000>, API <http://localhost:8000>, 상태 확인 <http://localhost:8000/api/ping>에 접속할 수 있습니다.

외부 데이터 경로를 변경하려면 `.env`를 설정합니다.

```dotenv
CANDLEMIND_DATA_ROOT=D:/CandleMind/data
CANDLEMIND_RUNTIME_ROOT=D:/CandleMind/runtime/app
```

### 로컬 개발

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend/requirements-dev.txt
python -m uvicorn backend.app.main:app --reload --env-file .env --port 8000
```

다른 터미널에서 프런트엔드를 실행합니다.

```powershell
cd frontend
npm ci
npm run dev
```

Vite는 기본적으로 <http://localhost:5173>에서 실행되며 API 요청을 백엔드로 프록시합니다.

## 설정 및 거래 안전

1. `.env.example`에서 `.env`를 만들고 비밀 정보, 데이터베이스 또는 실행 로그를 커밋하지 마십시오.
2. 설정 화면에서 Binance 및 AI Provider 자격 증명을 입력하고 `trader.db`와 `secret.key`를 함께 백업하십시오.
3. 설정 화면을 여는 동안 연결 진단용 출구 IP를 1분마다 감지합니다. 이 결과는 거래소 API 권한이나 IP 허용 목록 설정을 대체하지 않습니다.
4. 클라우드 AI Base URL은 신뢰할 수 있는 HTTPS 호스트만 허용합니다. 로컬 Provider는 루프백 또는 RFC1918 주소를 사용할 수 있습니다. 자세한 내용은 [`docs/AI_CONFIGURATION.md`](docs/AI_CONFIGURATION.md)를 참고하십시오.
5. 메인넷 거래에는 testnet 검증, 서버 측 스위치 및 화면의 명시적 확인이 모두 필요합니다.
6. 실제 자금을 사용하기 전에 전략, 포지션 크기, 레버리지, 손절 및 거래소 권한을 독립적으로 검토하십시오.

Binance 재시도, 쿨다운, IP 진단 및 주문 확인 규칙은 [`docs/BINANCE_RESILIENCE.md`](docs/BINANCE_RESILIENCE.md)를 참고하십시오. 거래소 선택, 영속성 및 미연결 공급자 격리 규칙은 [`docs/EXCHANGE_PROVIDERS.md`](docs/EXCHANGE_PROVIDERS.md)를 참고하십시오.

## 강화학습 연구

저장소에는 EMA 특성 기반 추세 추종 강화학습 연구 인프라가 있으며 특성 공학, 데이터 릴리스, 수명 주기 및 출처 검증 계약을 포함합니다. 이는 오프라인 실험과 재현성만을 위한 것이며 **온라인 추론, 주문 결정 또는 실거래 실행에 연결되어 있지 않습니다**. 자세한 경계는 [`docs/research/RL_RESEARCH_STATUS.md`](docs/research/RL_RESEARCH_STATUS.md)를 참고하십시오.

## 테스트

```powershell
# 전체 격리 검증
powershell -ExecutionPolicy Bypass -File ops/verify.ps1

# 개별 검증
python -m pytest backend/tests -q
cd frontend
npm test
npm run build
```

검증은 임시 데이터 디렉터리를 사용하며 G 드라이브의 운영 데이터를 변경하지 않습니다.

## 저장소 구조

```text
CandleMind/
|-- backend/app/        # API, 서비스, 전략 및 런타임
|-- backend/scripts/    # 데이터 유지보수 및 오프라인 평가
|-- backend/tests/      # 단위, 계약, 보안 및 회귀 테스트
|-- frontend/src/       # 페이지, 컴포넌트, 상태 및 API 클라이언트
|-- docs/               # 데이터, 연구, 보안 및 운영 문서
|-- ops/                # 배포 및 격리 검증 스크립트
`-- docker-compose.yml  # 컨테이너 구성
```

## 기여

기여하기 전에 [`CONTRIBUTING.md`](CONTRIBUTING.md), [`AGENTS.md`](AGENTS.md), [`docs/README.md`](docs/README.md)를 읽고 전체 검증을 실행하십시오. 보안 문제는 [`SECURITY.md`](SECURITY.md)에 따라 비공개로 신고하십시오.

## 감사의 글

<table><tr><td align="center" width="240"><a href="https://netapi.cc/"><img src="docs/assets/netapi-logo.png" alt="NetAPI Logo" width="210"></a></td><td>CandleMind에 Token을 지원해 주신 <a href="https://netapi.cc/"><strong>NetAPI.cc</strong></a>에 감사드립니다. 하나의 API 키로 주요 AI 모델을 사용하며 지능형 라우팅과 사용량 기반 결제를 이용할 수 있습니다.</td></tr></table>

## 커뮤니티

AI 자동 거래 커뮤니티에서 퀀트 연구, 엔지니어링 및 위험 관리에 대해 논의할 수 있습니다.

<p align="center"><img src="docs/assets/wechat-trading-community.jpg" alt="AI 자동 거래 커뮤니티 QR 코드" width="360"></p>

[이미지가 표시되지 않으면 CDN에서 열기](https://testingcf.jsdelivr.net/gh/JacobeZhao/CandleMind@main/docs/assets/wechat-trading-community.jpg)

## 라이선스

CandleMind는 [MIT License](LICENSE)로 배포됩니다. 프로젝트를 사용, 수정 또는 배포할 때 저작권과 라이선스 고지를 유지하십시오. 제3자 의존성에는 각 라이선스가 적용됩니다.
