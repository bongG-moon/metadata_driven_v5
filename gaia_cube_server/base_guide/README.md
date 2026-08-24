# GAIA-CUBE Base Guide

이 디렉터리는 `gaia_cube_server` 구현에 사용한 사용자 제공 API 가이드와 정리 문서를 보관한다.

## 문서 구성

- `gaia/`: GAIA Agent 호출과 최종 답변 추출 방법
- `cube/`: CUBE callback 수신과 Rich Notification 발송 형식
- `common/`: 향후 세션·최근 대화 운영을 위한 보류 메모. 현재 최소 서버의 구현 범위는 아님

## 현재 구현 기준

현재 HCP callback 서버는 아래의 한 가지 동기 흐름을 구현한다.

```text
CUBE callback POST /api/v1/receiver
  -> .env의 GAIA_API_URL 호출
  -> 최종 Chat Output 답변 추출
  -> CUBE Rich Notification 발송
  -> callback ACK 반환
```

- `GAIA_API_URL`에는 Agent까지 포함한 전체 URL을 직접 설정한다.
- CUBE 발송 payload의 `content[0].process`는 비워 두지 않는다.
- 같은 사용자와 같은 CUBE 채널은 GAIA session ID를 메모리에서 재사용한다.
- 현재는 별도 실행 모드, 데이터베이스, 작업 큐, 스케줄러, 최근 대화 조회 API를 포함하지 않는다.

현재 실행과 실제 전체 흐름 시험 절차는 [production_callback_server/PRODUCTION_SERVER_RUN_GUIDE.md](../production_callback_server/PRODUCTION_SERVER_RUN_GUIDE.md)를 따른다.

`cube/source/` 아래 파일은 당시 제공된 원문 보관본이다. 원문 안의 과거 endpoint는 현재 실행 경로가 아니다.
