산재 재조사 판정서 워크벤치 — 실행 방법
==========================================
1) 이 폴더의 5개 파일이 다 있는지 확인:
   app.py / dashboard.html / jbpjeong_source.py / law_mapper.py / comwel_fetch.py

2) 이 폴더에서 터미널 열기

3) 서버 실행:
   [맥/리눅스]
   SERVICE_KEY="발급받은_서비스키" python3 app.py
   [윈도우 CMD]
   set SERVICE_KEY=발급받은_서비스키
   python app.py

4) 브라우저에서 http://localhost:8000 접속

* 서비스키 없이 실행하면 트리가 안 뜹니다(판정서는 라이브 조회).
* 파이썬 3 필요. 외부 라이브러리 설치 불필요(표준 라이브러리만 사용).
