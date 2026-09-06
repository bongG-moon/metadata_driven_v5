"""직원 DataFrame을 Portal 이름 조회용 MongoDB 컬렉션에 저장합니다.

입력 DataFrame에는 ``empno``, ``emp_nm``, ``dept_nm`` 세 컬럼만 있으면
됩니다. DataFrame이 비어 있으면 기존 MongoDB 데이터는 건드리지 않습니다.
"""

from __future__ import annotations

import os
from typing import Any

import pandas as pd

try:
    from pymongo import MongoClient
except ImportError:  # pragma: no cover - 실제 저장 시에만 필요합니다.
    MongoClient = None


REQUIRED_COLUMNS = ("empno", "emp_nm", "dept_nm")
DEFAULT_COLLECTION = "portal_employee_directory"


def replace_employee_directory(
    dataframe: pd.DataFrame,
    *,
    mongo_uri: str | None = None,
    database: str | None = None,
    collection: str | None = None,
) -> int:
    """비어 있지 않은 직원 DataFrame으로 컬렉션 전체를 교체합니다.

    ``mongo_uri``와 ``database``를 생략하면 각각 ``MONGODB_URI``와
    ``MONGODB_DATABASE`` 환경변수를 사용합니다. ``collection``은
    ``PTMORE_EMPLOYEE_DIRECTORY_COLLECTION`` 또는 기본값을 사용합니다.
    """

    missing_columns = [column for column in REQUIRED_COLUMNS if column not in dataframe]
    if missing_columns:
        raise ValueError(f"필수 컬럼이 없습니다: {', '.join(missing_columns)}")

    # 빈 DataFrame일 때는 기존 직원 목록을 삭제하지 않습니다.
    if dataframe.empty:
        return 0

    uri = str(mongo_uri or os.getenv("MONGODB_URI") or "").strip()
    database_name = str(database or os.getenv("MONGODB_DATABASE") or "").strip()
    collection_name = str(
        collection
        or os.getenv("PTMORE_EMPLOYEE_DIRECTORY_COLLECTION")
        or DEFAULT_COLLECTION
    ).strip()
    if not uri or not database_name or not collection_name:
        raise ValueError("MONGODB_URI, MONGODB_DATABASE, 컬렉션 이름을 확인해 주세요.")

    # Portal 조회 키가 문자열 사번을 사용하므로 empno도 문자열로 저장합니다.
    records_frame = dataframe.loc[:, REQUIRED_COLUMNS].copy()
    records_frame["empno"] = records_frame["empno"].map(
        lambda value: "" if pd.isna(value) else str(value).strip().removesuffix(".0")
    )
    records_frame["emp_nm"] = records_frame["emp_nm"].map(
        lambda value: "" if pd.isna(value) else str(value).strip()
    )
    records_frame["dept_nm"] = records_frame["dept_nm"].map(
        lambda value: "" if pd.isna(value) else str(value).strip()
    )
    records: list[dict[str, Any]] = records_frame.to_dict(orient="records")

    if MongoClient is None:
        raise RuntimeError("MongoDB 저장을 위해 pymongo 패키지가 필요합니다.")

    client = MongoClient(uri)
    try:
        target = client[database_name][collection_name]
        # 저장할 데이터가 있을 때만 기존 데이터를 비우고 새 DataFrame 전체를 저장합니다.
        target.delete_many({})
        target.insert_many(records)
    finally:
        client.close()

    return len(records)


# 사용 예시
# saved_count = replace_employee_directory(employee_dataframe)
# print(f"직원 {saved_count}명 저장 완료")
