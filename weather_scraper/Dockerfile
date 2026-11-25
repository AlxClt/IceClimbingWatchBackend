FROM python:3.13.9

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Mode (update) and options are given as the container command
ENTRYPOINT ["python", "main.py"]
