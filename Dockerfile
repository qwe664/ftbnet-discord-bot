FROM python:3.12-slim

WORKDIR /app

# ws_listener.py / ptero.py 用 print() 輸出；不加這個的話 stdout 會被緩衝，
# docker logs 要累積到一定量才看得到（例如備份期間的「暫停連線」只印一次，就一直看不到）
ENV PYTHONUNBUFFERED=1

# 先只複製 requirements.txt 再安裝套件，
# 這樣改程式碼時不會每次重新下載套件，只有 requirements 變動才會重跑這層
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python3", "main.py"]
