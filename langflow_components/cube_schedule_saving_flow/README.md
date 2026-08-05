# CUBE Schedule Saving Flow

외부 Schedule Authoring 환경에서 `cube.schedule.v1` 문서를 MongoDB에 등록하는 Langflow 1.9.2 standalone Flow입니다.

기본값은 `dry_run=true`입니다. 실제 저장 전 Writer 노드에서 MongoDB URI, database, collection과 dry-run toggle을 확인합니다.

예시 요청:

```text
작업자 2000000에게 평일 오전 8시마다 "전일 생산 판정 Report를 만들어줘"라고 CUBE 질의를 보내는 스케줄을 등록해줘. 채널 ID는 500000000이야.
```

저장 문서는 `next_run_at`, lease, outbox를 포함하지 않습니다. 해당 실행 상태는 CUBE Scheduler Server의 별도 runtime DB가 관리합니다.
