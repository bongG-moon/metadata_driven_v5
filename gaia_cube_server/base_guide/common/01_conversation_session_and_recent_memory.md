# 채팅 세션과 최근 대화 보존 계약

> 상태: **보류된 향후 운영 설계 메모**. 이 문서의 영속 저장소, 최근 대화 보존, generation과 복구 규칙은 현재 기본 API 연동 구현 범위가 아니다. 현재 기준은 `../../BEGINNER_GUIDE.md`의 사용자·채널별 GAIA `session_id` 구분까지다.

## 목적

각 사용자가 자신의 CUBE 채팅에서 연속된 질문을 보낼 때 GAIA/Langflow가 같은 대화로 인식하도록 활성 `session_id`를 안정적으로 재사용한다. 동시에 서버와 GAIA/Langflow 어느 쪽에서도 전체 채팅을 무제한 문맥으로 사용하지 않도록 최근 몇 턴만 제한적으로 유지한다.

## 확정된 요구사항

1. CUBE의 각 사용자 채팅은 대응하는 GAIA `session_id`를 가진다.
2. 세션 매핑은 서버 프로세스 메모리에만 두지 않고 재시작 후에도 복원할 수 있어야 한다.
3. 전체 채팅 이력은 저장하지 않는다.
4. 사용자 메시지와 AI 응답은 설정된 최근 대화 창만 유지한다.
5. 운영 저장소 구현과 로컬·테스트용 더미 저장소 구현을 분리하고 같은 인터페이스를 사용한다.
6. 서버의 최근 대화 제한뿐 아니라 GAIA/Langflow가 같은 세션에서 실제로 조회하는 메시지 범위도 제한한다.
7. CUBE 채널 하나는 서버 설정상 하나의 GAIA Agent에만 연결한다. Agent 선택은 callback payload가 아니라 인증된 채널 설정으로 결정한다.

## 대화 식별 키

대화의 내부 식별 키에는 가능한 경우 다음 값을 함께 사용한다.

```text
environment
+ CUBE tenant/workspace
+ authenticated CUBE employee_id
+ CUBE conversation/channel_id
+ optional thread_id
```

- 사번만 사용하면 동일 사용자의 여러 채팅, 채널 또는 향후 그룹 대화를 구분하기 어렵다.
- CUBE가 안정적인 conversation ID 또는 channel ID를 제공하면 반드시 함께 사용한다.
- 현재 정책은 CUBE 채널 하나가 하나의 GAIA Agent에 고정되는 방식이다. 따라서 `svc_id`는 대화 식별 key에 넣지 않고 채널 설정에서 해석한다.
- 채널의 Agent 매핑이 바뀌면 활성 GAIA 세션 generation을 교체하고, 실행 기록에는 해석된 `svc_id`와 매핑 버전을 남긴다.
- CUBE API가 사용자당 하나의 고정 DM 채널만 보장할 때에만 사번 단독 fallback을 검토한다.
- 그룹 채팅의 안전한 기본 범위는 `사용자 + 채팅방 + thread`이다. 채팅방 전체가 문맥을 공유하는 방식은 별도 승인 후 사용한다.
- 키 구성값은 구분자를 단순 연결하지 않고 canonical JSON으로 직렬화한 뒤 HMAC-SHA-256 조회 키로 만든다. 저장 문서에는 `hmac_key_version`도 함께 기록한다.

사번은 요청 본문의 임의 필드를 그대로 신뢰하지 않는다. CUBE webhook 서명 또는 인증된 CUBE identity에서 확인하고, 해당 사용자가 대상 `svc_id`를 호출할 권한이 있는지 검사한다.

## GAIA session_id 생성 원칙

`X-Gaia-User-Id`와 요청 본문의 `user_id`에는 GAIA 권한 확인에 필요한 실제 사용자 사번을 사용한다. 반면 `session_id`에는 사번 원문을 그대로 노출하지 않는 것을 기본으로 한다.

권장 방식은 다음과 같다.

1. 최초 채팅 요청 시 `gc_<random UUID>` 형태의 불투명한 GAIA 세션 ID를 생성한다.
2. 내부 대화 식별 키와 GAIA 세션 ID의 매핑을 운영 저장소에 보존한다.
3. 같은 활성 채팅에서는 같은 GAIA 세션 ID를 재사용한다.
4. 명시적인 새 대화 요청, 세션 만료 또는 운영자 초기화 시 새 세션 ID로 교체한다.
5. 하나의 논리적 CUBE 채팅과 현재 활성 GAIA 세션을 분리하여 관리한다. 세션 교체 후에도 CUBE 채팅 매핑 자체는 유지하고 `generation`만 증가시킨다.
6. 조회·생성은 활성 대화 키의 unique index와 atomic upsert/CAS를 사용하여 동시 최초 요청에서도 하나의 활성 세션만 생성한다.
7. 각 실행은 시작 시의 `generation`과 fencing token을 고정하고, 만료·초기화 후 이전 실행이 새 generation에 답변을 기록하지 못하게 검사한다.

내부 조회 키가 필요하면 사번·채널 조합의 평문을 키로 사용하지 않고 서버 비밀키 기반 HMAC을 사용할 수 있다. 실제 사번은 GAIA 호출과 CUBE 응답에 필요한 범위에서만 취급하고 로그와 URL에 넣지 않는다.

## 최근 대화 창

최근 대화에는 사용자와 AI의 메시지를 턴 단위로 보관한다.

초기 권장값은 다음과 같으며 실제 운영 요구에 따라 설정으로 조정한다.

| 항목 | 초기 권장값 | 설명 |
| --- | --- | --- |
| 최근 턴 수 | 5턴 | 사용자 5개 + AI 5개, 최대 10개 메시지 |
| 메시지별 최대 길이 | 8,000자 | 과도하게 큰 본문 저장 방지 |
| 세션 전체 최근 이력 크기 | 운영 설정값 | 문자 수뿐 아니라 UTF-8 byte 상한 적용 |
| 비활성 세션 만료 | 24시간 | 만료 후 다음 요청은 새 GAIA 세션 사용 |

이 수치는 API 제한과 사용자 대화 패턴을 검증한 뒤 확정한다. 저장 구현은 새 메시지를 추가할 때 오래된 메시지를 같은 작업에서 제거하여 상한을 항상 지킨다.

보존할 최소 메시지 항목은 다음과 같다.

```json
{
  "message_id": "CUBE_OR_INTERNAL_MESSAGE_ID",
  "turn_id": "INTERNAL_TURN_ID",
  "role": "user | assistant",
  "text": "메시지 본문",
  "created_at": "UTC timestamp"
}
```

최근 이력은 실행 상태 저장소가 아니라 마지막 N턴을 보여주는 bounded view로 취급한다. `execution_id`, GAIA 실행 상태, CUBE 전달 상태와 오류는 별도 실행/outbox 레코드에 저장한다. 사용자 입력과 AI 응답은 같은 `turn_id`로 원자적으로 추가하고 턴 단위로 제거한다. GAIA 원본 응답 전체, 인증 헤더, 토큰 및 불필요한 중복 메시지 필드는 최근 대화 창에 저장하지 않는다.

사번, 최근 메시지와 CUBE 전달 주소는 민감정보로 분류한다. 저장 시 암호화, 최소 권한, 감사와 삭제 정책을 적용하고 로그에는 마스킹한다. HMAC 조회 키와 실제 전달에 필요한 암호화된 주소를 분리한다.

## 두 개의 메모리 경계

최근 대화 제한은 다음 두 계층에 각각 적용해야 한다.

1. **서버 저장 계층**: `ConversationStore`에는 최근 N턴만 남기고 이전 메시지는 제거한다.
2. **GAIA/Langflow 문맥 계층**: 동일한 `session_id`를 계속 사용하더라도 Flow의 메모리 조회가 최근 N턴만 반환하도록 구성하거나, 활성 GAIA 세션을 정책에 따라 교체한다.

### 현재 확인된 관찰

2026-08-22 사용자 실행에서 동일한 GAIA `session_id`로 호출하면 대화가 계속 누적되는 동작이 확인되었다. 따라서 같은 활성 GAIA 세션을 사용자 수명 전체에 걸쳐 무기한 재사용하지 않는다. 아직 확인되지 않은 것은 누적 최대치, 자동 만료, 실제 저장 기간, 삭제 API 및 Langflow가 매 실행에서 불러오는 범위다.

서버가 최근 N턴만 저장해도 GAIA/Langflow가 동일 세션의 전체 이력을 계속 불러오면 “최근 메시지만 기억” 요구사항은 충족되지 않는다. 따라서 구현 완료 조건에는 GAIA 운영 호출로 다음을 확인하는 검증이 포함되어야 한다.

- 동일 `session_id`가 실제 대화 문맥을 유지하는지
- Langflow 메모리 컴포넌트가 조회하는 최대 메시지/턴 수
- GAIA가 메시지를 저장하는 기간과 삭제 가능 여부
- 세션 교체 후 이전 문맥이 분리되는지

우선순위는 Langflow의 메모리 조회 창을 최근 N턴으로 제한하는 방식이다. 같은 `session_id`가 계속 누적된다는 사실만으로 실제 모델 입력도 항상 전체 이력이라고 단정하지는 않으며, 운영 검증으로 구분한다. 운영 환경에서 최근 N턴 제한을 보장할 수 없다면 다음 대안을 사용한다.

- N턴 또는 비활성 만료 시 활성 `gaia_session_id`를 새 generation으로 교체한다.
- 새 세션에도 최근 문맥이 필요하면 서버의 제한된 최근 대화만 별도의 context builder로 전달한다.
- 현재 GAIA API에는 별도 history 필드가 보이지 않으므로, 최근 대화를 `message`에 합치는 방식은 실제 Flow 입력 계약을 확인한 뒤에만 사용한다.

## GAIA에 전달할 문맥

현재 제공된 GAIA API는 `message`, `user_id`, `session_id`를 받는다. 따라서 기본 동작은 다음과 같다.

- GAIA에는 현재 사용자 메시지와 안정적인 `session_id`만 전달한다.
- 로컬 최근 대화 전체를 매 요청마다 `message`에 합쳐 보내지 않는다.
- 동일 `session_id`가 실제로 GAIA/Langflow의 대화 문맥을 유지하는지는 운영 API로 검증한다.
- GAIA가 세션 문맥을 유지하지 않거나 메모리 창을 제한할 수 없다고 확인된 경우에만 최근 대화 창을 조합하는 별도 context builder를 추가한다.

이렇게 하면 GAIA가 이미 보존하는 문맥과 서버가 재전송한 문맥이 중복되는 문제를 피할 수 있다.

## 요청 처리 순서

```text
CUBE 메시지 수신
  -> CUBE 서명/identity 및 GAIA svc_id 권한 확인
  -> 범위가 포함된 입력 멱등성 키 중복 확인
  -> 사용자 + 채팅 키로 활성 GAIA session_id 조회/생성
  -> generation과 fencing token을 고정하고 같은 채팅의 요청 순서 직렬화
  -> 실행 레코드를 gaia_pending으로 기록
  -> GAIA API 호출
  -> 최종 Chat Output 답변 추출
  -> 최근 턴 저장과 CUBE outbox 생성을 원자적으로 기록
  -> outbox worker가 CUBE API로 원래 요청자/채팅에 발송
  -> 실행과 outbox 전달 상태 갱신
```

같은 채팅에서 메시지가 빠르게 연속 도착하면 동일 세션에 대한 GAIA 호출 순서가 뒤바뀌지 않도록 다중 서버에서 동작하는 채팅별 lease 또는 순차 큐를 사용한다. lease에는 `owner_id`, fencing token, 만료, 갱신 heartbeat와 장애 후 reclaim 규칙을 둔다. 외부 GAIA API 호출 중에는 데이터베이스 트랜잭션을 열어 두지 않는다.

## 재시도와 중복 방지

- `environment + CUBE tenant/workspace + channel/thread + message_id`를 입력 멱등성 키로 사용한다. CUBE의 `message_id`가 전역 고유하다고 가정하지 않는다.
- 같은 메시지가 재수신되면 GAIA를 다시 실행하지 않는다.
- GAIA 실행 성공 후 CUBE 발송만 실패했다면 저장된 최종 답변을 재발송하고 GAIA를 다시 호출하지 않는다.
- GAIA 실행 실패와 CUBE 발송 실패 상태를 분리한다.
- 사용자 메시지와 AI 응답은 한 쌍의 실행 ID로 연결한다.
- 실행 상태는 최소한 `claimed -> gaia_pending -> gaia_succeeded -> delivery_pending -> delivered | failed`를 구분한다.
- 답변 저장과 CUBE outbox 생성은 가능한 경우 하나의 데이터베이스 트랜잭션으로 처리한다.
- 현재 GAIA API에는 요청 멱등성 키가 보이지 않는다. HTTP timeout이 발생하면 GAIA가 실제 실행했는지 모호할 수 있으므로 자동 재호출이 중복 실행을 만들 수 있다는 점을 상태에 기록하고 운영 재처리 정책을 별도로 둔다.
- MongoDB TTL 정리는 즉시 실행되지 않으므로 조회 시에도 `expires_at <= now`를 확인하고 CAS로 세션을 교체한다.
- 최근 이력과 세션이 만료되어도 멱등성 및 outbox 기록은 별도 보존 정책에 따라 유지하여 늦은 재전송이 GAIA 중복 실행을 만들지 않게 한다.

## 스케줄 실행과 대화 세션 분리

스케줄 질문은 대화형 채팅의 최근 문맥에 영향을 받지 않는 것이 기본이다.

- CUBE 전달 대상 채팅은 동일할 수 있지만 GAIA 실행 세션은 대화형 세션과 별도 namespace를 사용한다.
- 기본 권장은 스케줄 실행마다 독립된 GAIA `session_id`를 발급하는 방식이다.
- 스케줄 자체가 이전 실행의 문맥을 이어야 하는 명시적 요구가 있을 때에만 `schedule_id`별 세션을 재사용한다.

## 운영 구현과 더미 구현

공통 `ConversationStore` 계약을 두고 구현을 분리한다. 일반 CRUD보다 다음과 같은 원자적 행위 중심 인터페이스를 사용한다.

- `claim_message`
- `get_or_create_session`
- `append_turn_and_trim`
- `save_answer_and_enqueue_delivery`
- `mark_delivered`
- `rotate_session`

- 운영: 지속성 저장소를 사용하는 `MongoConversationStore` 등의 실제 구현
- 로컬·테스트: 프로세스 내 제한 저장소를 사용하는 `InMemoryConversationStore`

운영 모드에서 저장소 연결이 실패하면 더미 저장소로 자동 전환하지 않는다. 세션 단절과 사용자 간 문맥 혼합을 막기 위해 readiness 실패로 처리한다. 더미 저장소도 injectable clock을 사용해 TTL, 잠금, 멱등성, generation fencing과 크기 상한을 동일하게 구현하며 두 저장소에 같은 contract test를 적용한다.

## 운영 저장 문서 예시

아래는 개념 예시이며 실제 컬렉션명과 필드는 구현 단계에서 확정한다.

```json
{
  "conversation_key_hash": "HMAC_VALUE",
  "hmac_key_version": 1,
  "gaia_svc_id": "GAIA_SERVICE_ID",
  "channel_agent_mapping_version": 1,
  "gaia_session_id": "gc_OPAQUE_UUID",
  "cube_route_ciphertext": "ENCRYPTED_ROUTE_DATA",
  "generation": 1,
  "fencing_token": 1,
  "active_turn_count": 0,
  "recent_messages": [],
  "created_at": "UTC timestamp",
  "last_active_at": "UTC timestamp",
  "expires_at": "UTC timestamp"
}
```

## CUBE API 수신 후 확정할 항목

- 사용자 사번 필드
- message ID와 중복 재전송 규칙
- DM conversation/channel ID와 thread ID 제공 여부
- 그룹 채팅에서 세션을 사용자별로 나눌지 채팅방 단위로 공유할지
- 새 대화/세션 초기화 이벤트 또는 명령 지원 여부
- CUBE 메시지 삭제 시 서버 최근 이력도 제거해야 하는지
