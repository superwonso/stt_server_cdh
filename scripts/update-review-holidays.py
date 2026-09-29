"""Validate an annual holiday JSON file and replace that public data file only.

No network, credentials or operating database access. Restart the API after the
reviewed data file is deployed to apply that year's authoritative replacement.
"""
import argparse
import json
import os
import tempfile
from datetime import date
from pathlib import Path


def validated(source, year):
    if type(year) is not int or not 2000 <= year <= 2100 or source.stat().st_size > 128 * 1024:
        raise ValueError("공휴일 연도 또는 파일 크기를 확인해 주세요.")
    document=json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(document,dict) or document.get("year")!=year or not isinstance(document.get("holidays"),list) or len(document["holidays"])>366:
        raise ValueError("year와 holidays 배열을 확인해 주세요.")
    seen=set();rows=[]
    for row in document["holidays"]:
        if not isinstance(row,dict):raise ValueError("잘못된 날짜 항목입니다.")
        value,name=row.get("date"),row.get("name")
        if not isinstance(value,str) or len(value)!=10 or date.fromisoformat(value).isoformat()!=value or not value.startswith(str(year)+"-") or value in seen:
            raise ValueError("날짜·연도·중복 항목을 확인해 주세요.")
        if not isinstance(name,str) or not 1<=len(name.strip())<=100 or any(ord(c)<32 for c in name):
            raise ValueError("공휴일 이름을 확인해 주세요.")
        seen.add(value);rows.append({"date":value,"name":name.strip()})
    origin=document.get("source","operator-supplied")
    if not isinstance(origin,str) or len(origin)>500 or any(ord(c)<32 for c in origin):
        raise ValueError("출처를 확인해 주세요.")
    return {"year":year,"source":origin,"holidays":sorted(rows,key=lambda row:row["date"])}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year",type=int,required=True)
    parser.add_argument("--input",type=Path,required=True)
    args=parser.parse_args()
    try:document=validated(args.input,args.year)
    except (ValueError,OSError,TypeError) as exc:parser.error(str(exc))
    folder=Path(__file__).resolve().parents[1]/"data"/"review-holidays"
    folder.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=folder,suffix=".tmp",delete=False) as handle:
        temporary=Path(handle.name)
        json.dump(document,handle,ensure_ascii=False,indent=2);handle.write("\n");handle.flush();os.fsync(handle.fileno())
    try:os.replace(temporary,folder/f"{args.year}.json")
    finally:
        if temporary.exists():temporary.unlink()
    print(f"공휴일 {args.year}년 {len(document['holidays'])}개를 검증했습니다. 변경 파일을 검토·배포한 뒤 API를 다시 시작하세요.")

if __name__=="__main__":main()
