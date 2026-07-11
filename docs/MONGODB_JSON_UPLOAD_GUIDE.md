# MongoDB Metadata Export/Upload Guide

이 가이드는 현재 MongoDB에 저장된 v4 메타데이터를 백업하거나 다른 환경 MongoDB로 이관할 때 JSON으로 내려받고 올리는 방법을 설명합니다. v5의 정상 운영은 기존 v4 collection을 직접 공유하므로 v4에서 v5로 export/upload하거나 복사할 필요가 없습니다.

대상은 운영 flow가 읽는 core metadata 3종입니다.

- Domain metadata: `agent_v4_domain_items`
- Table catalog metadata: `agent_v4_table_catalog_items`
- Main variable/filter metadata: `agent_v4_main_flow_filters`

질의 결과 저장소인 `agent_v4_result_store`는 분석 실행 중 생성되는 runtime 데이터이므로 이 seed export/upload 대상에서 제외합니다.

## Export

현재 `.env`의 `MONGODB_URI`, `MONGODB_DATABASE`, collection 환경변수를 사용해서 3개 collection을 하나의 portable JSON bundle로 저장합니다.

```powershell
cd C:\Users\qkekt\Desktop\metadata_driven_v5
python tools\export_mongodb_metadata_to_json.py
```

기본 출력 위치:

```text
metadata_exports\mongodb_metadata_export_YYYYMMDD_HHMMSS.json
```

특정 종류만 내려받을 수도 있습니다. `main variable`은 구현상 `main-flow-filter`와 같은 대상입니다.

```powershell
python tools\export_mongodb_metadata_to_json.py --metadata-kind domain
python tools\export_mongodb_metadata_to_json.py --metadata-kind table-catalog
python tools\export_mongodb_metadata_to_json.py --metadata-kind main-variable
python tools\export_mongodb_metadata_to_json.py --metadata-kind domain,table-catalog,main-variable
```

컬렉션 이름을 직접 지정하려면 아래처럼 입력합니다.

```powershell
python tools\export_mongodb_metadata_to_json.py `
  --database datagov `
  --domain-collection agent_v4_domain_items `
  --table-catalog-collection agent_v4_table_catalog_items `
  --main-flow-filter-collection agent_v4_main_flow_filters
```

## Upload Dry Run

다른 환경에서 먼저 dry-run으로 대상 DB, collection, 문서 수를 확인합니다.

```powershell
python tools\upload_json_to_mongodb.py --dry-run --input metadata_exports\mongodb_metadata_export_YYYYMMDD_HHMMSS.json
```

`--input`을 생략하면 `metadata_exports\mongodb_metadata_export_*.json` 중 최신 파일을 사용합니다.

```powershell
python tools\upload_json_to_mongodb.py --dry-run
```

## Upload

다른 환경의 `.env`에 `MONGODB_URI`를 넣거나, PowerShell 환경변수로 지정한 뒤 실행합니다.

```powershell
$env:MONGODB_URI="<set-in-local-env>"
$env:MONGODB_DATABASE="datagov"

python tools\upload_json_to_mongodb.py --input metadata_exports\mongodb_metadata_export_YYYYMMDD_HHMMSS.json
```

기본 mode는 `upsert`입니다. 각 문서의 `_id` 기준으로 같은 문서는 교체하고, 없는 문서는 새로 추가합니다.

대상 collection 이름을 다른 환경에 맞춰 바꿀 수 있습니다.

```powershell
python tools\upload_json_to_mongodb.py `
  --input metadata_exports\mongodb_metadata_export_YYYYMMDD_HHMMSS.json `
  --database datagov `
  --domain-collection agent_v4_domain_items `
  --table-catalog-collection agent_v4_table_catalog_items `
  --main-flow-filter-collection agent_v4_main_flow_filters
```

대상 collection을 완전히 비우고 JSON 내용만 다시 넣고 싶을 때만 `replace` mode를 사용합니다.

```powershell
python tools\upload_json_to_mongodb.py `
  --input metadata_exports\mongodb_metadata_export_YYYYMMDD_HHMMSS.json `
  --mode replace
```

## Preserved Shape

export 파일은 MongoDB에 저장된 document를 변형하지 않고 bundle 안의 `collections.*.documents`에 그대로 담습니다.

```json
{
  "_export_format": "metadata_driven_v5.mongodb_metadata_bundle.v1",
  "source_database": "datagov",
  "collections": {
    "domain": {
      "collection_name": "agent_v4_domain_items",
      "documents": []
    }
  }
}
```

`ObjectId`, `datetime` 같은 BSON 타입이 있어도 재업로드할 수 있도록 MongoDB Extended JSON 형식으로 저장합니다.
등록 flow가 저장한 `raw_text`, `refined_text`, `registration_trace`, `payload` 등도 JSON 안에 있는 그대로 보존합니다.

## Current Export

현재 작업 환경에서 내려받은 파일:

```text
metadata_exports\mongodb_metadata_export_20260701_130231.json
```

문서 수:

- Domain metadata: 63
- Table catalog metadata: 9
- Main variable/filter metadata: 17
