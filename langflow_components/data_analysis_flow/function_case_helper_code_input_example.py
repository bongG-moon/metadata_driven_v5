# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: pandas Function Case Helper Library
# 역할: 선택된 pandas 분석에서만 주입하는 재사용 helper 함수 모음입니다.
# 주요 입력: 사용자 표현 (input_text) · 필수, 원본 DataFrame (frame) · 필수
# 주요 출력: 필터링된 DataFrame (result)
# 처리 흐름: pandas executor가 선택적으로 주입하는 제품 토큰 helper 예시이며, 원본 DataFrame을 바꾸지 않고 필터 결과를 반환합니다.
# 유지보수 포인트: helper는 원본 DataFrame을 변경하지 않아야 하며, executor가 주입한 record_function_case_result가 있으면 실행 근거를 기록합니다.
# =============================================================================

try:
    record_function_case_result
except NameError:
    _function_case_results = []

    # 함수 설명: `record_function_case_result()`는 선택 helper 실행 결과의 함수명·입력·행 수를 분석 근거로 기록합니다.
    def record_function_case_result(function_name, input_text, result_value, description=""):
        # 17 pandas executor가 아닌 로컬/단독 검증에서만 사용하는 fallback이다.
        # Langflow 실행 중에는 executor가 주입한 같은 이름의 함수를 그대로 사용한다.
        try:
            matched_count = len(result_value)
        except Exception:
            matched_count = 0
        _function_case_results.append(
            {
                "function_name": str(function_name or ""),
                "input_text": str(input_text or ""),
                "description": str(description or ""),
                "matched_count": int(matched_count),
            }
        )
        return result_value

# 주요 함수: 질문의 제품 토큰을 표준 제품 컬럼에 역할별로 매칭해 DataFrame을 필터링합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def match_product_tokens(input_text, frame, token_columns=None, output_order=None):
    # 원본 DataFrame을 변경하지 않기 위해 copy본에서 필터링을 수행한다.
    result = frame.copy()
    if result.empty:
        return result

    # 비교 안정성을 위해 값에서 영문/숫자만 남기고 대문자로 정규화한다.
    # 함수 설명: `_norm()`는 제품 token 비교를 위해 값을 영문·숫자 중심의 표준 문자열로 정규화합니다.
    def _norm(value):
        text = str('' if value is None else value).strip().upper()
        if '.' in text:
            left, right = text.split('.', 1)
            if left.lstrip('-').isdigit() and right and all(ch == '0' for ch in right):
                text = left
        return ''.join(ch for ch in text if ('A' <= ch <= 'Z') or ('0' <= ch <= '9'))

    # LEAD 컬럼은 현업 질문/원천값에서 78Lead, 152ball처럼 단위/설명이 붙는 경우가 있다.
    # 이 suffix는 LEAD 역할 비교에만 제거하고 다른 제품 속성에는 적용하지 않는다.
    # 함수 설명: `_lead_norm()`는 LEAD 값에서 Lead/Ball suffix를 제거해 숫자 역할 값으로 비교할 수 있게 합니다.
    def _lead_norm(value):
        text = _norm(value)
        for suffix in ('LEAD', 'BALL'):
            if text.endswith(suffix):
                return text[:-len(suffix)]
        return text

    # 함수 설명: `_lead_suffix_number()`는 Lead/Ball 단위 접미사가 붙은 표현에서 앞쪽 숫자만 추출합니다.
    def _lead_suffix_number(value):
        text = _norm(value)
        for suffix in ('LEAD', 'BALL'):
            if text.endswith(suffix) and text[:-len(suffix)].isdigit():
                return text[:-len(suffix)]
        return ''

    # 컬럼명은 PKG_TYPE1, MCP NO처럼 표기 차이가 있어도 같은 key로 비교한다.
    # 함수 설명: `_col_key()`는 컬럼명 표기 차이를 대문자 underscore key로 정규화합니다.
    def _col_key(value):
        text = str(value).upper()
        chars = []
        prev_sep = False
        for ch in text:
            if ('A' <= ch <= 'Z') or ('0' <= ch <= '9'):
                chars.append(ch)
                prev_sep = False
            elif not prev_sep:
                chars.append('_')
                prev_sep = True
        return ''.join(chars).strip('_')

    # 사용자 입력 문장에서 제품 식별에 필요한 token만 추출한다.
    # 공정/수량/일자처럼 제품 속성이 아닌 흔한 단어는 stopwords로 제거한다.
    # 함수 설명: `_tokens()`는 문자열을 비교 가능한 검색 token 목록으로 분리·정규화합니다.
    def _tokens(value):
        stopwords = {'PRODUCT', 'DEVICE', 'PKG', 'WIP', 'INPUT', 'OUTPUT', 'OUT', 'PRODUCTION', 'TODAY', 'YESTERDAY', 'WB', 'FCB', 'BG', 'SBM'}
        raw_items = []
        current = ''
        for ch in str(value or '').upper():
            if ('A' <= ch <= 'Z') or ('0' <= ch <= '9') or ch in '-_/':
                current += ch
            else:
                if current:
                    raw_items.append(current)
                    current = ''
        if current:
            raw_items.append(current)
        result_tokens = []
        for item in raw_items:
            cleaned = item.strip('-_/')
            if cleaned and cleaned not in stopwords and cleaned not in result_tokens:
                result_tokens.append(cleaned)
        return result_tokens

    # 표준 제품 속성 역할과 실제 데이터 컬럼 alias를 연결한다.
    role_aliases = {
        'TECH': {'TECH'},
        'DEN': {'DEN', 'DENSITY'},
        'MODE': {'MODE'},
        'PKG1': {'PKG_TYPE1', 'PKG1', 'PKG_TYP1'},
        'PKG2': {'PKG_TYPE2', 'PKG2', 'PKG_TYP2'},
        'LEAD': {'LEAD'},
        'MCP_NO': {'MCP_NO', 'MCPNO', 'MCP_SALES_NO', 'MCP_SALE_CD', 'MCPSALENO'},
        'DEVICE': {'DEVICE'},
        'DEVICE_DESC': {'DEVICE_DESC'},
        'TSV_DIE_TYP': {'TSV_DIE_TYP', 'TSV_DIE_TYPE'},
        'ORG': {'ORG', 'ORGANIZ_CD'},
        'FAMILY': {'FAMILY'},
    }

    # token_columns가 주어지면 해당 컬럼만 사용하고, 없으면 알려진 제품 속성 컬럼만 자동 선택한다.
    requested = token_columns if token_columns not in (None, '', [], {}) else []
    if requested and not isinstance(requested, (list, tuple, set)):
        requested = [requested]
    known_aliases = {alias for aliases in role_aliases.values() for alias in aliases}
    columns = [str(column) for column in requested if str(column) in result.columns] if requested else [str(column) for column in result.columns if _col_key(column) in known_aliases]
    groups = [_tokens(part) for part in str(input_text or '').split(',')]
    groups = [group for group in groups if group]
    if not columns or not groups:
        return result

    columns_by_role = {role: [] for role in role_aliases}
    columns_by_role['ALL'] = list(columns)
    alias_to_role = {alias: role for role, aliases in role_aliases.items() for alias in aliases}
    column_to_role = {}
    for column in columns:
        role = alias_to_role.get(_col_key(column))
        if role:
            column_to_role[column] = role
            columns_by_role[role].append(column)

    # 컬럼별 값을 미리 정규화해 token 매칭을 반복해도 같은 전처리를 다시 하지 않게 한다.
    normalized_values = {
        column: result[column].map(_lead_norm if column_to_role.get(column) == 'LEAD' else _norm)
        for column in columns
    }

    # 함수 설명: `_has_rows()`는 pandas boolean mask에 실제로 선택된 행이 하나 이상 있는지 확인합니다.
    def _has_rows(mask):
        return mask is not None and bool(mask.any())

    # 지정한 역할군의 컬럼들에서 token을 exact 또는 prefix 방식으로 찾는다.
    # 함수 설명: `_match()`는 선택한 제품 역할 컬럼들을 exact·contains·prefix 방식으로 OR 매칭합니다.
    def _match(roles, token, mode):
        selected_columns = []
        for role in roles:
            for column in columns_by_role.get(role, []):
                if column not in selected_columns:
                    selected_columns.append(column)
        combined = None
        for column in selected_columns:
            values = normalized_values[column]
            compare_token = _lead_norm(token) if column_to_role.get(column) == 'LEAD' else token
            if mode == 'exact':
                current = values == compare_token
            elif mode == 'contains':
                current = values.str.contains(compare_token, na=False, regex=False)
            elif mode == 'starts_with':
                current = values.str.startswith(compare_token, na=False)
            else:
                current = values == compare_token
            combined = current if combined is None else (combined | current)
        return combined

    # token 하나를 DataFrame mask로 변환한다.
    # 특수 규칙은 여기서 처리한다.
    # 함수 설명: `_token_mask()`는 제품 token 하나를 역할별 컬럼 조건이 반영된 DataFrame boolean mask로 변환합니다.
    def _token_mask(raw_token):
        raw_text = str(raw_token or '').strip().upper()
        token = _norm(raw_text)
        if not token:
            return None

        # 숫자+lead/ball 표현은 LEAD 역할 전용 token으로 처리한다. 예: 78Lead, 152ball.
        lead_suffix_number = _lead_suffix_number(raw_text)
        if lead_suffix_number:
            return _match(['LEAD'], lead_suffix_number, 'exact')

        # FC+숫자: PKG1은 FCBGA이고 LEAD는 숫자 부분이다. 예: FC12, FC78, FC344.
        if token.startswith('FC') and token[2:].isdigit():
            pkg_mask = _match(['PKG1'], 'FCBGA', 'exact')
            lead_mask = _match(['LEAD'], token[2:], 'exact')
            return None if pkg_mask is None or lead_mask is None else (pkg_mask & lead_mask)

        # F+숫자: FCBGA/VFBGA/UFBGA 등 package 종류를 특정하지 않고 LEAD만 적용한다. 예: F12, F78, F344.
        if token.startswith('F') and token[1:].isdigit():
            return _match(['LEAD'], token[1:], 'exact')

        # 영문 1자리-숫자3자리(+선택 영숫자) 패턴: MCP_NO 앞부분 입력으로 보고 prefix 조건으로 매칭한다. 예: L-218, B-123, Z-000.
        if _looks_mcp_no_prefix(raw_text):
            return _match(['MCP_NO'], token, 'starts_with')

        # X+숫자: 우선 ORG 컬럼에서 x를 제거한 숫자로 매칭한다. 예: x8, X16, x24.
        if token.startswith('X') and token[1:].isdigit():
            return _match(['ORG'], token[1:], 'exact')

        # token 모양으로 컬럼 역할을 먼저 제한하지 않고, 모든 구조화 제품 후보 속성 컬럼에서 exact 매칭한다.
        # DEVICE_DESC는 자유 텍스트 설명 컬럼이므로 token 포함 여부를 보조적으로 확인한다.
        matched = _match(['ALL'], token, 'exact')
        desc_matched = _match(['DEVICE_DESC'], token, 'contains')
        if matched is None:
            matched = desc_matched
        elif desc_matched is not None:
            matched = matched | desc_matched
        return matched if _has_rows(matched) else None

    # 함수 설명: `_looks_mcp_no_prefix()`는 입력 token이 영문 1자리-숫자 3자리 형태의 MCP_NO prefix인지 판정합니다.
    def _looks_mcp_no_prefix(value):
        text = str(value or '').strip().upper()
        if '-' not in text:
            return False
        prefix, suffix = text.split('-', 1)
        if len(prefix) != 1 or not ('A' <= prefix <= 'Z'):
            return False
        if len(suffix) < 3 or not suffix[:3].isdigit():
            return False
        return all(('A' <= ch <= 'Z') or ('0' <= ch <= '9') for ch in suffix[3:])

    # 콤마로 나뉜 제품 묶음은 OR로 결합하고, 한 제품 안의 token들은 AND로 결합한다.
    final_mask = None
    for group in groups:
        group_mask = None
        group_failed = False
        for token in group:
            current = _token_mask(token)
            if current is None:
                group_failed = True
                break
            group_mask = current if group_mask is None else (group_mask & current)
        if (group_failed or group_mask is None) and group:
            group_mask = result.index.to_series().map(lambda _: False)
        if group_mask is not None:
            final_mask = group_mask if final_mask is None else (final_mask | group_mask)

    filtered = result if final_mask is None else result[final_mask].copy()

    # 필요하면 결과 컬럼 순서를 호출자가 지정한 순서로 정리한다.
    ordered_columns = output_order if output_order not in (None, '', [], {}) else []
    if ordered_columns and not isinstance(ordered_columns, (list, tuple, set)):
        ordered_columns = [ordered_columns]
    ordered_columns = [column for column in ordered_columns if column in filtered.columns]
    if ordered_columns:
        rest = [column for column in filtered.columns if column not in ordered_columns]
        filtered = filtered[ordered_columns + rest]
    try:
        record_function_case_result('match_product_tokens', input_text, filtered, '제품 속성 token 매칭 결과')
    except Exception:
        pass
    return filtered

# 주요 함수: 여러 helper 선택 형식을 검증하기 위해 DataFrame 복사본을 그대로 반환합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def sample_passthrough_helper(input_text, frame, note=None):
    # 여러 helper를 동시에 넣는 형식을 검증하기 위한 더미 helper다.
    # 실제 분석 로직은 수행하지 않고 DataFrame copy만 반환한다.
    result = frame.copy()
    try:
        record_function_case_result('sample_passthrough_helper', input_text, result, str(note or '더미 helper 통과 결과'))
    except Exception:
        pass
    return result
