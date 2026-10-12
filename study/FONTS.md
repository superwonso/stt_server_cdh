# 한글 글꼴

모든 글꼴은 SIL Open Font License 1.1입니다. `public/fonts/OFL.txt`를 함께 보관합니다. 외부 글꼴 서비스에 사용자의 브라우저 요청을 보내지 않습니다.

## 화면

`src/fonts/NotoSansKR-ui.woff2`는 Noto CJK Sans 2.004의 한국어 Regular OTF에서 초기 화면과 단원 목록에 필요한 338자를 fontTools로 추린 부분 글꼴(31,456바이트)입니다. `next/font/local`로 자체 제공합니다. 사용자 자료나 문제에 있는 나머지 글자는 시스템 한글 글꼴로 표시합니다.

원본: https://github.com/notofonts/noto-cjk/tree/Sans2.004

## PDF

`public/fonts/NotoSansKR-Regular.ttf`는 Google Fonts 공식 `ofl/notosanskr/NotoSansKR[wght].ttf`를 fontTools `varLib.instancer`로 `wght=400`에 고정한 정적 TrueType 글꼴입니다(2026-10-12 준비). 가변 원본은 중복 배포하지 않습니다.

원본과 라이선스: https://github.com/google/fonts/tree/main/ofl/notosanskr

재생성 예:

```text
python -m fontTools.varLib.instancer NotoSansKR-variable.ttf wght=400 --output NotoSansKR-Regular.ttf
```

검증에 사용한 fontTools 버전은 4.66.1입니다. 일반 설치·실행·빌드에서는 Python이나 fontTools가 필요하지 않습니다.

PDF는 `subset:false`, `features:{calt:false}`로 전체 정적 글꼴을 포함합니다. 이전 CFF/OTF 부분 포함은 일부 뷰어에서 한글이 네모로 표시됐고, 문맥 대체 글리프는 공백 폭이 달라져 긴 문장이 여백을 넘는 문제가 있었습니다. 현재 조합은 생성 PDF를 실제로 렌더링하고 글자 좌표까지 확인했습니다. PDF 파일은 작은 문제집도 약 3 MB이며, 글꼴은 내보내기를 누를 때만 읽습니다. 지원하지 않는 문자나 이모지는 안내와 함께 내보내기를 중단하며 Excel/JSON을 대안으로 제공합니다.
