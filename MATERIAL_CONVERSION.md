# 수업 자료의 로컬 PDF / PPTX 변환

`server.material_conversion.convert_material()`은 보관된 원본을 읽어 페이지 또는 슬라이드별 Markdown과 출처를 반환한다. 원본을 수정하거나 삭제하지 않으며 LLM을 호출하지 않는다.

## 선택 근거와 의존성

사용자가 지정한 [GitHub Copilot 스킬](https://github.com/github/awesome-copilot/blob/main/skills/convert-pdf-to-md/SKILL.md), [설치 안내](https://github.com/github/awesome-copilot/blob/main/skills/convert-pdf-to-md/references/setup.md), [변환 스크립트](https://github.com/github/awesome-copilot/blob/main/skills/convert-pdf-to-md/scripts/convert_pdf_to_md.py)를 읽었다. 참조 스킬의 본문·이미지 구분과 OCR 한계 공개 원칙을 적용하고, 사용자가 허용한 대안으로 서버용 파서를 구성했다.

참조 스크립트는 본문의 페이지 연결을 보장하지 않으며, 동일 출력 폴더가 있으면 재귀 삭제하고 예외 본문을 출력한다. 사용자 로컬 변환 도구를 서버 업로드 처리에 그대로 가져오지 않는다. [Microsoft MarkItDown](https://github.com/microsoft/markitdown)은 MIT 라이선스이며 PDF/PPTX 선택 의존성을 제공하지만, 범용 URL·플러그인·클라우드 기능은 이 서버에 필요하지 않다. 그와 같은 파서 계열을 직접 제한 호출한다. MarkItDown, PyMuPDF, Office, LibreOffice, Docker, OCR 모델은 추가하지 않았다.

| 패키지 | 고정 버전 | 용도 / 공식 출처 |
| --- | --- | --- |
| pdfminer.six | 20260107 | 페이지·텍스트 배치·이미지/벡터 감지. [PyPI](https://pypi.org/project/pdfminer.six/20260107/) · [MIT 라이선스](https://github.com/pdfminer/pdfminer.six/blob/master/LICENSE) |
| python-pptx | 1.0.2 | 슬라이드·그룹·표·발표자 노트. [PyPI](https://pypi.org/project/python-pptx/1.0.2/) · [MIT 라이선스](https://github.com/scanny/python-pptx/blob/master/LICENSE) |
| defusedxml | 0.7.1 | DTD·엔터티·외부 XML 참조 차단. [PyPI](https://pypi.org/project/defusedxml/0.7.1/) |

[requirements-materials.txt](server/requirements-materials.txt)에 직접 의존성을 고정했다. Python 3.12의 Windows 네이티브 별도 가상환경에서 공식 PyPI wheel만 설치했다. 당시 전이 의존성은 cffi 2.1.1, charset-normalizer 3.5.1, cryptography 50.0.1, lxml 6.1.3, Pillow 12.3.0, pycparser 3.0, typing-extensions 4.16.0, XlsxWriter 3.2.9였다. 운영 환경에 자동 설치했다는 뜻은 아니다. GPU·ASR 모델은 변경하지 않는다.

## 호출 계약

```python
from server.material_conversion import convert_material, MaterialConversionError
result = convert_material(private_file_path, "pdf", cancel=shutdown.is_set)
```

`kind`는 `pdf` 또는 `pptx`이며 실제 파일 헤더도 검사한다. 경로는 인증된 업로드 저장소가 만들어야 한다. 함수는 HTTP 사용자·소유권·DB revision을 모르므로 호출자는 작업 전후 소유권과 revision을 확인해야 한다. 요청의 파일 경로를 그대로 넘기면 안 된다.

반환 객체에는 `kind`, 원본 `sha256`/`size_bytes`, `unit_count`, `units`, 전체 `markdown`, 합집합 `warnings`, `extraction_version: material-v1`이 있다. 각 unit은 `{index, source_id, text, markdown, warnings}`다. index는 1부터 연속이며 source_id는 `page:1` 또는 `slide:1`이다. 서비스는 자료 ID를 결합해 수업 간 고유 출처를 만든다.

`text`는 추출 본문, unit의 `markdown`은 HTML·링크·이미지가 실행되지 않도록 특수문자를 이스케이프한 표현이다. 전체 Markdown에는 페이지별 고정 추출 범위 안내도 포함한다. 표의 행·셀 텍스트와 발표자 노트를 보존한다. 경고 메시지 표는 `WARNING_MESSAGES`로 공개한다.

오류는 `MaterialConversionError.code`의 고정 코드만 사용한다: `unsupported_type`, `invalid_document`, `encrypted_document`, `size_limit`, `archive_limit`, `unit_limit`, `output_limit`, `timeout`, `cancelled`, `worker_failed`, `dependency_missing`, `isolation_unavailable`. 파서 예외 본문·경로·자료 내용은 로그에 출력하지 않는다.

## 추출 범위

- PDF는 페이지별 텍스트 레이어와 텍스트 주석/입력 필드 문자열을 포함한다. CJK는 원본의 글꼴 매핑에 의존한다. 표의 글자는 포함하되 셀·열 관계, 다단 읽기 순서, 수식 의미는 보장하지 않는다. 이미지·벡터 도형은 감지하고 미추출 범위를 안내한다.
- PPTX는 모든 슬라이드의 텍스트, 중첩 그룹, 표, 발표자 노트와 숨김 슬라이드를 포함한다. 차트·SmartArt의 저장 문자열은 가능한 범위에서 포함하며 시각적 관계는 해석하지 않는다. 마스터·레이아웃 상속 내용, 이미지 글자, 수식, 첨부 개체, 음성·영상은 완전 추출을 보장하지 않는다. `.ppt`와 `.pptm`은 지원하지 않는다.
- `ocr_required` 또는 `no_extractable_text` 페이지는 원본 확인·OCR 대기다. 전체 자료를 완전히 읽었다고 표시하면 안 된다. 텍스트가 일부 있어도 이미지 등 누락 안내를 통합 정리본의 출처 범위에 포함해야 한다.
- OCR·클라우드 분석·유료 LLM을 자동 호출하지 않고 외부 링크를 열지 않는다. 원본은 서비스가 별도로 보존한다.

## 자원 제한과 격리

기본 한도: 입력 32 MiB, PPTX 압축 해제 총합 128 MiB, ZIP 항목 4096개, 압축 비율 100배(1 MiB 이하 항목 제외), 400페이지/슬라이드, 출력 100만 문자, 벽시계 60초, 자식 메모리 768 MiB. 호출자는 한도를 낮출 수 있지만 높일 수는 없다. 초과 시 명시적으로 실패하며 본문을 조용히 자르지 않는다.

독립 Python 자식(`-I -B`)에 OS 필수 환경 값만 전달한다. API 키·프록시·사용자 설정 경로·PYTHONPATH는 상속하지 않는다. Windows는 일시 중단한 자식을 전용 Job에 붙이고 메모리·CPU·프로세스 수 제한을 건 후 실행한다. venv redirector를 포함한 그 작업의 자식만 종료한다. Linux는 새 세션과 메모리·CPU·파일 크기·파일 수 제한을 사용한다. 제한 설치 실패 시 변환을 시작하지 않는다.

파서는 메모리 스트림만 받으며 ZIP을 디스크에 풀지 않는다. 경로 순회·중복 이름·심볼릭 링크·암호화·매크로·압축량·XML 깊이/노드 수를 검사한다. 자식 Python의 네트워크·프로세스 실행 감사 이벤트도 차단한다. stdout은 크기가 제한된 JSON 프로토콜이고 stderr는 공개하지 않는다. 부모가 원본 해시·출처 순서·고정 경고를 재검증한다.

이는 파서 자원 소비와 프로세스 수명을 제한하는 방식이며 같은 사용자 권한의 완전한 OS 파일 접근 샌드박스는 아니다. 네이티브 라이브러리의 임의 코드 실행 취약점까지 격리한다고 주장하지 않는다. 파서 보안 업데이트 검토와 서비스 작업 동시 실행 제한(권장 1~2개)은 별도로 유지해야 한다.

## 합성 검증

`python -m unittest tests.test_material_conversion -v`는 임시 합성 PDF/PPTX만 사용한다. 페이지 출처·마지막 페이지·한글·표·그룹·발표자 노트·숨김 슬라이드·OCR 안내·원본 보존, 악성 ZIP/XML/매크로 거절, 안전한 오류와 환경변수 제한을 검사한다. 실제 자식의 시간 초과·취소 종료, 다른 프로세스 보존, Windows Job 메모리 제한도 검증한다.

실제 사용자 문서, 복잡한 수식·차트의 시각적 충실도, OCR 정확도, 긴 자료의 품질과 Linux 실기동은 미검증이다.
