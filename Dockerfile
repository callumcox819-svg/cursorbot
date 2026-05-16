FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir -U pip setuptools wheel

COPY requirements.prod.txt /app/requirements.prod.txt
RUN pip install --no-cache-dir -r requirements.prod.txt

COPY . /app

CMD ["python", "bot.py"]
