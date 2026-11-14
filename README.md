# HSdeck Mean Streets Of Gadgetzan 카드 API

## 단일 원본과 생성 방향

이 저장소의 카드별 정본은 data/<cardId>.json이다.

- card: 현재 표시용 스펙 캐시다. 현재 카드 값은 과거 최초 스펙으로 덮어쓰지 않는다.
- timeline.initialSpec: 최초 사용일이 확인된 카드의 해당 날짜 전체 스펙이다.
- timeline.patches: 이후 날짜별 실제 변경 필드만 기록한다. ownTags는 nativeTags로 정규화하며, 참조 태그는 timeline.tagReferences에 분리한다.
- timeline.knownEvents: 최초 사용일은 미확정이지만 변경일만 확인된 카드의 사건이다. from이나 최초일을 추정하지 않는다.
- release-policy.json: 한국 시간 기준 최초 사용일과 확정 상태만 관리한다. 날짜 미확정 카드는 date: null, status: pending이다.
- artworkProvenance: 원화·황금 리소스의 원본 출처 메타데이터다. 카드 타임라인과 별도로 보존한다.

timeline-index.json, history-audit.json, pending-history.json은 조사 근거 및 회귀 검증용이다. 빌드 결과를 덮어쓰는 원본으로 사용하지 않는다.

## API 생성

    python tools/build-api.py

빌더는 data/*.json과 release-policy.json을 읽고 아래 3개 파일만 생성한다.

- cards.json: 현재 카드 스펙
- render.json: 현재 카드 스펙과 날짜별 역방향 이력
- history.json: DBF ID 기반 기존 호환 이력

재생성 결과가 현재 파일과 다르면 변경 파일을 출력한다. --check는 파일을 쓰지 않고 차이만 검사한다.

    python tools/build-api.py
    python tools/build-api.py
    python tools/build-api.py --check

## 부재·삭제 규칙

JSON의 0, false, null, 빈 문자열, 빈 배열은 각각 실제 값으로 보존한다. 키가 없는 상태는 별도로 취급한다. 기존 역방향 API는 과거에 키가 없었던 상태를 삭제 연산으로 표현하는 규약이 확인되지 않았으므로, 빌더는 그런 이력을 null로 위장하지 않고 오류로 중단한다.

## 텍스트 및 카드 리소스

카드 설명의 <br></br> 줄바꿈을 그대로 보존한다. art/, golden/, artworkProvenance의 원본 리소스와 식별자는 이 빌드 과정에서 변경하지 않는다.


## API 경량화 및 편집 지침 (2026-11-14)

- 공개 사용 자료는 `cards.json` 목록, `data/<CARD_ID>.json` 한국 날짜별 카드 정보·능력/효과 변경, `art/` 일반 카드 원화, `golden/` 황금 원화·애니메이션, `frame/` 프레임·텍스처·셰이더입니다. 토큰/자식 카드 ID 및 실제 자산 연결을 보존합니다.
- `render.json`, `history.json` 및 `*-date.json` 집계는 중복/호환 출력이므로 Git에 재커밋·배포하지 않습니다. CI 빌드가 생성하더라도 검사 시점의 임시 파일로 취급합니다. 새로운 schema/legacy 래퍼를 만들지 않습니다.
- 원본이 날짜 키 형식이 아니라면 출시/최초 사용 근거와 필드를 보존한 무손실 변환 후 사용합니다. 베타 북미 첫 실사용일의 한국 날짜 환산, 모험 지구 실제 개방, 이벤트 실제 개시, 부모로부터 토큰 생성 가능일을 적용하며 사전 탑재일은 사용하지 않습니다.
- 과거 잘못 기입한 날짜 키는 삭제·통합하고, 이후 날짜에는 변경 필드만 넣습니다. 표시 개행은 `<br></br>`, 원시 증거의 개행은 임의 치환하지 않습니다. 황금/프레임 원본 바이너리 파일을 재인코딩하지 않습니다.
- 실제 재생성·날짜 검증에 쓰이는 `tools/`, `.github/`, 정책·감사 자료는 참조를 검증한 다음 삭제 여부를 결정합니다. 추정 기록을 검증된 날짜로 만들지 않습니다.
