# Project Structure Guide

이 문서는 현재 `bidmate-rag` 레포의 폴더와 파일 구조를 팀원들이 빠르게 이해할 수 있도록 정리한 안내서입니다.

## Top-Level Structure

```text
bidmate-rag/
├── .github/              # GitHub Actions 등 자동화 설정
├── app/                  # 데모 앱 또는 API 서빙 진입점
├── artifacts/            # 실행 중 생성되는 산출물(인덱스, 로그, 캐시)
├── configs/              # 시나리오/모델/실험 설정 파일
├── data/                 # 원본 데이터, 전처리 데이터, 평가셋
├── docker/               # 컨테이너 실행 환경
├── docs/                 # 구조 문서, 의사결정 기록, 협업 문서
├── experiments/          # 노트북, 실험 추적 자료
├── reports/              # 발표 자료, 보고서용 도표
├── scripts/              # 수동 실행용 CLI 스크립트
├── src/                  # 실제 RAG 서비스/실험 코드
├── tests/                # 단위 및 통합 테스트
├── .env.example          # 팀 공용 환경변수 템플릿
├── Makefile              # 자주 쓰는 명령어 모음
├── main.py               # 루트 실행 진입점
├── pyproject.toml        # Python 패키지/의존성 설정
└── README.md             # 프로젝트 개요와 실행 방법
```

## Directory Details

### `app/`

- `app/main.py`
  데모 실행용 엔트리포인트입니다. 나중에 FastAPI 또는 Streamlit 진입점으로 확장할 수 있습니다.
- `app/api/`
  API 라우트나 요청/응답 계층을 둘 위치입니다.
- `app/templates/`
  간단한 데모 UI 템플릿이나 정적 파일을 둘 수 있습니다.

### `configs/`

- `configs/base.yaml`
  공통 기본 설정입니다.
- `configs/providers/`
  OpenAI, Gemini, Claude, local vLLM 같은 모델 제공자별 설정을 둡니다.
- `configs/experiments/`
  어떤 시나리오를 어떤 모델 조합으로 비교할지 정의합니다.

이 구조 덕분에 코드 수정 없이 설정만 바꿔 시나리오 A/B 비교 실험을 돌리기 쉽게 만들었습니다.

### `data/`

- `data/raw/rfp/`
  제공받은 원본 RFP 문서를 둡니다.
- `data/raw/metadata/`
  `data_list.csv` 같은 메타데이터 파일을 둡니다.
- `data/processed/`
  파싱/정제/청킹 결과 같은 전처리 산출물을 둡니다.
- `data/eval/`
  평가용 질문셋, 정답셋, 평가 샘플을 둡니다.

주의:
원본 데이터는 외부 공유 금지 대상이므로 Git에 올라가지 않도록 `.gitignore`로 제외되어 있습니다.

### `artifacts/`

- `artifacts/vector_db/`
  FAISS, Chroma 등 인덱스 파일을 저장합니다.
- `artifacts/logs/`
  실행 로그나 벤치마크 로그를 둡니다.
- `artifacts/cache/`
  임시 캐시 파일을 둡니다.

`data/`는 입력 데이터 중심, `artifacts/`는 실행 중 생성되는 출력물 중심으로 역할을 나눴습니다.

### `src/bidmate_rag/`

프로젝트의 핵심 Python 패키지입니다.

- `config/`
  런타임 설정과 프롬프트 관리 코드
- `loaders/`
  PDF, HWP, 메타데이터 로딩 로직
- `preprocessing/`
  텍스트 정제와 청킹 로직
- `providers/`
  OpenAI-compatible LLM/임베딩 제공자 어댑터
- `retrieval/`
  벡터 저장소, 검색, 필터링, reranking
- `generation/`
  답변 생성과 포맷팅
- `evaluation/`
  평가셋 로딩, 지표 계산, 벤치마크 실행
- `pipelines/`
  ingest, indexing, chat 같은 end-to-end 흐름
- `storage/`
  메타데이터 저장소 등 보조 저장 계층
- `schema.py`
  문서, 청크, 응답 등 공용 데이터 구조
- `constants.py`
  전역 상수

### `scripts/`

- `scripts/ingest_data.py`
  원본 문서 파이프라인 실행용
- `scripts/build_index.py`
  임베딩 및 인덱스 생성용
- `scripts/run_eval.py`
  평가 실행용

초기에는 여기서 수동 실행하고, 안정화되면 `Makefile`이나 앱 계층과 연결하면 됩니다.

### `tests/`

- `tests/unit/`
  작은 함수와 모듈 단위 테스트
- `tests/integration/`
  파이프라인 연결 테스트

RAG 시스템은 로더, 청킹, 검색, 생성이 모두 연결되기 때문에 통합 테스트가 특히 중요합니다.

### `docs/`

- `docs/architecture.md`
  전체 시스템 구조 설명
- `docs/decision-log.md`
  주요 기술 선택 이유 기록
- `docs/collaboration/branch-strategy.md`
  팀 브랜치 전략과 네이밍 규칙
- `docs/collaboration/git-worktree-workflow.md`
  팀의 worktree 기반 브랜치 작업 규칙
- `docs/plans/`
  작업 계획 문서
- `docs/collaboration/`
  회의록, 협업 기록, 역할 분담 문서

보고서와 발표를 준비할 때 이 폴더의 기록이 큰 도움이 됩니다.

### `experiments/`

- `experiments/notebooks/`
  EDA, 청킹 실험, 검색 성능 비교용 노트북
- `experiments/tracking/`
  실험 결과 정리용 파일

### `reports/`

- `reports/figures/`
  보고서/발표용 그래프와 이미지
- `reports/slides/`
  발표 자료 초안 또는 최종본

## Recommended Team Workflow

1. `data/raw/`에 원본 문서와 메타데이터를 정리합니다.
2. `src/bidmate_rag/loaders`, `preprocessing`에서 문서 파싱과 청킹을 구현합니다.
3. `retrieval`, `providers/embeddings`, `artifacts/vector_db`를 연결해 검색 파이프라인을 만듭니다.
4. `providers/llm`, `generation`을 붙여 질의응답 체인을 만듭니다.
5. `configs/experiments/` 기준으로 모델/시나리오 비교 실험을 수행합니다.
6. `evaluation`과 `data/eval/`을 사용해 성능을 정리합니다.
7. 결과는 `docs/`, `reports/`에 남겨 발표와 제출물에 반영합니다.

## Naming and Usage Rules

- 실제 서비스 코드나 실험 코드는 가능하면 `src/bidmate_rag/` 아래에 둡니다.
- 실행 중 생성되는 파일은 `artifacts/`에 둡니다.
- 원본 데이터는 `data/raw/`에만 둡니다.
- 팀 의사결정과 실험 이유는 코드만 남기지 말고 `docs/decision-log.md`에도 간단히 기록합니다.

## Next Recommended Steps

- `pyproject.toml`에 실제 의존성 추가
- `loaders/`에 PDF/HWP 로더 구현
- `preprocessing/chunker.py`에 baseline chunking 구현
- `providers/llm/openai_compat.py`에 공통 LLM 클라이언트 구현
- `scripts/`와 `configs/`를 연결해 첫 baseline 파이프라인 실행

## Detailed Tree

아래 트리는 현재 레포의 실제 구조를 기준으로 정리한 상세 버전입니다.

주의:
- `data/raw/rfp/` 아래의 개별 원본 문서 파일들은 수가 많고 Git 추적 대상도 아니므로 트리에서는 생략했습니다.
- `__pycache__` 같은 임시 실행 파일도 제외했습니다.

```text
bidmate-rag/
├── .github/                              # CI/CD 및 GitHub 자동화 설정
│   └── workflows/
│       └── ci.yml                        # 기본 CI 워크플로우 placeholder
├── app/                                  # 데모 앱 및 API 진입점
│   ├── __init__.py                       # app 패키지 표시
│   ├── main.py                           # 데모/서빙용 메인 엔트리포인트
│   ├── api/
│   │   ├── __init__.py                   # API 패키지 표시
│   │   └── routes.py                     # API 라우트 정의 위치
│   └── templates/
│       └── .gitkeep                      # 빈 디렉터리 유지용 파일
├── artifacts/                            # 실행 중 생성되는 산출물
│   ├── cache/
│   │   └── .gitkeep                      # 캐시 디렉터리 placeholder
│   ├── logs/
│   │   └── .gitkeep                      # 로그 디렉터리 placeholder
│   └── vector_db/
│       └── .gitkeep                      # 벡터 인덱스 저장 위치
├── configs/                              # 설정 파일 모음
│   ├── base.yaml                         # 공통 기본 설정
│   ├── experiments/
│   │   ├── full_rag_compare.yaml         # 전체 RAG 비교 실험 설정
│   │   └── generation_compare.yaml       # generation-only 비교 실험 설정
│   └── providers/
│       ├── claude_sonnet.yaml            # Claude provider 설정
│       ├── gemini_flash.yaml             # Gemini provider 설정
│       ├── local_vllm.yaml               # local OpenAI-compatible provider 설정
│       └── openai_gpt5mini.yaml          # OpenAI provider 설정
├── data/                                 # 입력 데이터 및 평가 데이터
│   ├── .gitkeep                          # 루트 data 디렉터리 유지용 파일
│   ├── eval/
│   │   └── .gitkeep                      # 평가셋 저장 위치
│   ├── processed/
│   │   └── .gitkeep                      # 전처리 결과 저장 위치
│   └── raw/
│       ├── metadata/
│       │   └── data_list.csv             # 제공 메타데이터 CSV
│       └── rfp/                          # 원본 RFP 문서들(hwp, pdf, docx)
├── docker/                               # 컨테이너 환경 설정
│   └── Dockerfile                        # 기본 Docker 이미지 정의
├── docs/                                 # 문서화 및 협업 기록
│   ├── architecture.md                   # 시스템 구조 설명
│   ├── decision-log.md                   # 기술 선택 이유 기록
│   ├── project-structure.md              # 현재 문서
│   ├── collaboration/
│   │   └── .gitkeep                      # 협업 문서 저장 위치
│   └── plans/
│       └── 2026-04-02-project-scaffold.md # 초기 스캐폴딩 계획 기록
├── experiments/                          # 실험 및 분석 자료
│   ├── notebooks/
│   │   └── .gitkeep                      # EDA/실험 노트북 위치
│   └── tracking/
│       └── .gitkeep                      # 실험 기록 저장 위치
├── reports/                              # 보고서와 발표 자료
│   ├── figures/
│   │   └── .gitkeep                      # 그래프/이미지 저장 위치
│   └── slides/
│       └── .gitkeep                      # 발표 자료 저장 위치
├── scripts/                              # 수동 실행용 스크립트
│   ├── build_index.py                    # 인덱스 생성 CLI
│   ├── ingest_data.py                    # 데이터 적재 CLI
│   └── run_eval.py                       # 평가 실행 CLI
├── src/                                  # 핵심 Python 소스 코드
│   └── bidmate_rag/
│       ├── __init__.py                   # 패키지 진입점
│       ├── constants.py                  # 전역 상수
│       ├── schema.py                     # 공용 데이터 스키마
│       ├── config/
│       │   ├── __init__.py               # config 패키지 표시
│       │   ├── prompts.py                # 프롬프트 템플릿 관리
│       │   └── settings.py               # 런타임 설정 로딩
│       ├── evaluation/
│       │   ├── __init__.py               # evaluation 패키지 표시
│       │   ├── benchmark.py              # 벤치마크 실행 로직
│       │   ├── dataset.py                # 평가 데이터셋 처리
│       │   └── metrics.py                # 평가 지표 계산
│       ├── generation/
│       │   ├── __init__.py               # generation 패키지 표시
│       │   ├── formatter.py              # 응답 후처리/포맷팅
│       │   └── rag_chain.py              # 답변 생성 체인
│       ├── loaders/
│       │   ├── __init__.py               # loaders 패키지 표시
│       │   ├── hwp_loader.py             # HWP 로더
│       │   ├── metadata_loader.py        # 메타데이터 로더
│       │   └── pdf_loader.py             # PDF 로더
│       ├── pipelines/
│       │   ├── __init__.py               # pipelines 패키지 표시
│       │   ├── build_index.py            # 인덱싱 파이프라인
│       │   ├── chat.py                   # 질의응답 파이프라인
│       │   └── ingest.py                 # 적재 파이프라인
│       ├── preprocessing/
│       │   ├── __init__.py               # preprocessing 패키지 표시
│       │   ├── chunker.py                # 청킹 전략 구현 위치
│       │   └── cleaner.py                # 텍스트 정제 로직
│       ├── providers/
│       │   ├── __init__.py               # provider 패키지 표시
│       │   ├── embeddings/
│       │   │   ├── __init__.py           # embedding provider 패키지 표시
│       │   │   ├── base.py               # embedding 공통 인터페이스
│       │   │   ├── hf_embedder.py        # Hugging Face embedding 어댑터
│       │   │   └── openai_embedder.py    # OpenAI embedding 어댑터
│       │   └── llm/
│       │       ├── __init__.py           # llm provider 패키지 표시
│       │       ├── base.py               # LLM 공통 인터페이스
│       │       ├── openai_compat.py      # OpenAI-compatible LLM 어댑터
│       │       └── registry.py           # provider 등록/선택 로직
│       ├── retrieval/
│       │   ├── __init__.py               # retrieval 패키지 표시
│       │   ├── filters.py                # 메타데이터 필터링
│       │   ├── reranker.py               # reranking 로직
│       │   ├── retriever.py              # 검색 orchestration
│       │   └── vector_store.py           # 벡터 저장소 연결
│       └── storage/
│           ├── __init__.py               # storage 패키지 표시
│           └── metadata_store.py         # 메타데이터 저장소 관리
├── tests/                                # 테스트 코드
│   ├── integration/
│   │   └── .gitkeep                      # 통합 테스트 위치
│   └── unit/
│       └── .gitkeep                      # 단위 테스트 위치
├── .env.example                          # 팀 공용 환경변수 예시
├── .gitignore                            # Git 제외 규칙
├── Makefile                              # 자주 쓰는 명령어 모음
├── README.md                             # 프로젝트 소개와 실행 안내
├── main.py                               # 루트 엔트리포인트, app/main.py 연결
└── pyproject.toml                        # Python 프로젝트 설정
```
