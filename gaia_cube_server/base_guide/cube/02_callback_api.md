# CUBE 메시지 Callback API

## 출처와 범위

- 출처: 사용자 제공 `CUBE메세지 수신 및 CALL BACK` 예시
- 수신일: 2026-08-22
- 용도: 사용자가 CUBE에서 보낸 메시지 이벤트를 서버가 POST callback으로 수신한다.
- 예시 endpoint는 애플리케이션이 제공하는 `/api/chatops/callback`이다. CUBE가 호출할 실제 공개/내부 URL은 배포 단계에서 정한다.

## Callback 요청

초기 단순 예시는 사용자와 채널을 `process`에 포함한다.

```json
{
  "richnotificationmessage": {
    "process": {
      "processdata": "#점심 뭐 먹을까?",
      "userId": "user_12345",
      "channelId": "channel_general"
    }
  }
}
```

## 추출 경로

| 의미 | JSON 경로 | 제공 예시 |
| --- | --- | --- |
| 사용자 메시지 | `richnotificationmessage.process.processdata` | `#점심 뭐 먹을까?` |
| 사용자 ID | `richnotificationmessage.process.userId` | `user_12345` |
| 채널 ID | `richnotificationmessage.process.channelId` | `channel_general` |

현재 예시에는 CUBE message ID, tenant/workspace ID, thread ID, timestamp, 서명 또는 인증 정보가 없다. 실제 callback payload가 더 많은 필드를 포함하는지 확인해야 한다.

## FastAPI 예시로 확인된 callback envelope

추가 예시에서는 사용자와 채널 라우팅 값이 `header`에도 포함된다.

```json
{
  "richnotificationmessage": {
    "header": {
      "from": {
        "uniquename": "EMPLOYEE_ID_EXAMPLE"
      },
      "to": {
        "channelid": [
          "CHANNEL_ID_EXAMPLE"
        ]
      }
    },
    "process": {
      "processdata": "사용자 메시지"
    }
  }
}
```

정규화 우선순위는 다음과 같다.

| 의미 | 우선 경로 | 보조 경로 | 규칙 |
| --- | --- | --- | --- |
| 사용자 ID | `header.from.uniquename` | `process.userId` | 둘 다 있으면 일치해야 한다. |
| 채널 ID | `header.to.channelid[0]` | `process.channelId` | 둘 다 있으면 일치해야 한다. |
| 사용자 텍스트 | `process.processdata` | 없음 | 빈 문자열은 버튼/라디오 callback일 수 있다. |

header 값은 CUBE envelope의 라우팅 후보이며 process 값은 Rich Message의 `requestid`를 통해 전달된 상호작용 값일 수 있다. 최종 신뢰 여부는 callback 서명/인증 정책을 확인한 뒤 확정한다.

## 최초 진입 sentinel

FastAPI 예시에서 다음 값은 CUBE가 챗봇 최초 진입 시 보내는 제어 메시지로 처리한다.

```text
!@#HelloChatBot#@!
```

이 값은 일반 사용자 질문으로 GAIA에 전달하지 않고 `{"status": "ignored"}`로 ACK한다. GAIA 실행과 CUBE 답변 발송도 수행하지 않는다.

기존 자료에는 `ignore`와 `ignored`가 모두 등장하므로 실제 ACK status는 CUBE 공식 정책을 확인하기 전까지 내부 enum과 분리한다.

## Callback 응답 예시

```json
{
  "status": "success",
  "message": "🍽 오늘의 추천: 비빔밥",
  "targetUser": "user_12345"
}
```

가이드 설명에는 `status` 값으로 `success`, `ignore`, `error`가 제시되어 있지만 Python 예시는 `not_found`도 반환한다. 공식 허용값을 확인하기 전에는 이 목록을 고정 enum으로 간주하지 않는다.

응답 JSON 예시에는 `targetUser`가 있지만 Python 코드의 성공 응답에는 해당 필드가 없다. `targetUser`의 필수 여부와 실제 라우팅 사용 여부도 미확정이다.

## ACK와 사용자 메시지 발송의 구분

제공된 가이드는 callback 처리 후 다음 두 동작을 분리한다.

1. 이전 메시지 발송 API를 호출하여 사용자에게 실제 답변을 보낸다.
2. callback HTTP 요청에는 처리 `status`와 `message`를 응답한다.

따라서 callback 응답 body의 `message`가 CUBE 사용자에게 자동 표시된다고 가정하지 않는다. 실제 사용자 전달은 `01_message_send_api.md`의 Rich Notification 발송 성공 여부로 관리한다.

## 서버 처리 흐름

```text
callback JSON 검증
  -> CUBE identity/서명 검증
  -> header/process에서 사용자·채널을 교차 검증하고 processdata 추출
  -> HelloChatBot sentinel이면 ignored ACK 후 종료
  -> 입력 중복 확인 및 대화 세션 조회
  -> GAIA API 호출
  -> 최종 Chat Output 답변 추출
  -> CUBE 발송 outbox 저장
  -> callback ACK 반환
  -> CUBE 발송 worker가 실제 답변 전송
```

GAIA 응답 시간이 CUBE callback timeout보다 길 수 있으므로 실제 timeout 정책을 확인한 뒤 동기 처리 또는 빠른 ACK 후 비동기 처리 방식을 결정한다. 제공된 Flask 예시는 동기 처리지만 운영 계약으로 확정된 것은 아니다.

## 들여쓰기를 복원한 개념 코드

원문은 Flask 예시이며 `parse_cmd`, `run_logic`, `send_message_to_user`는 제공되지 않은 placeholder 함수다. 또한 `Flask(name)`은 일반적인 Python 표기인 `Flask(__name__)`로 복원했다. 현재 프로젝트의 실제 서버 구현은 FastAPI를 사용한다.

```python
from flask import Flask, jsonify, request

app = Flask(__name__)


@app.route("/api/chatops/callback", methods=["POST"])
def handle_chatops_callback():
    try:
        req_body = request.get_json()
        if not req_body:
            return jsonify({"status": "error", "message": "Empty body"}), 400

        try:
            process = req_body["richnotificationmessage"]["process"]
            user_msg = process["processdata"]
        except (KeyError, TypeError):
            return jsonify({"status": "error", "message": "Invalid format"}), 400

        cmd, param = parse_cmd(user_msg)
        if not cmd:
            return jsonify({"status": "ignore", "message": "Not a command"}), 200

        reply_text = run_logic(cmd, param)
        if reply_text:
            send_message_to_user(req_body, reply_text)
            return jsonify({"status": "success", "message": reply_text}), 200
        return jsonify({"status": "not_found", "message": "Unknown command"}), 200
    except Exception:
        return jsonify({"status": "error", "message": "Internal error"}), 500
```

원문의 `str(e)`를 그대로 외부 응답에 반환하면 내부 정보가 노출될 수 있으므로 정규화 예시에서는 고정된 오류 문구를 사용했다.

## 세션 매핑

검증된 `header.from.uniquename`과 `header.to.channelid[0]`은 논리적 CUBE 채팅 키의 기본 후보다. `process.userId/channelId`는 교차 검증과 호환 fallback에 사용한다. 실제 세션 키에는 CUBE tenant/workspace, thread ID와 GAIA `svc_id`도 포함하며 `../common/01_conversation_session_and_recent_memory.md`의 계약을 따른다.

## 보안과 검증

- callback의 인증/서명 또는 신뢰 가능한 송신 IP 정책이 확인되기 전에는 `userId`를 인증된 사번으로 간주하지 않는다.
- 요청 body 크기와 문자열 길이를 제한한다.
- 단순 `Dict[str, Any]`보다 Pydantic request model과 명시적인 정규화 검증을 사용한다.
- JSON 구조 오류와 인증 오류를 구분하되 내부 예외를 응답에 노출하지 않는다.
- 전체 callback payload와 사용자 메시지를 평문 로그에 남기지 않는다.
- message ID가 제공되면 범위가 포함된 입력 멱등성 키를 만들고, 동일 callback이 재전송되어도 GAIA를 다시 호출하지 않는다.

## 구현 전 확인할 항목

- CUBE가 호출하는 callback 인증/서명 방식
- callback timeout과 재전송 조건
- 공식 ACK status 및 HTTP status 계약
- 실제 message ID 경로
- tenant/workspace, conversation, channel, thread 식별자 경로
- `userId`와 발송 API `uniquename`의 실제 변환 규칙
- header와 process의 사용자·채널 값이 불일치할 때의 CUBE 공식 처리 규칙
- callback 응답의 `message`가 사용자 화면에 표시되는지 여부
- `ignore`와 `ignored` 중 공식 ACK status 값
- `targetUser`의 필요 여부. 필수가 아니라면 사번을 ACK에 반사하지 않는다.
- 긴 GAIA 실행을 위한 비동기 ACK 허용 여부
