FROM python:3.12-slim

WORKDIR /app

# 先只複製 requirements.txt 再安裝套件，
# 這樣改程式碼時不會每次重新下載套件，只有 requirements 變動才會重跑這層
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python3", "main.py"]
