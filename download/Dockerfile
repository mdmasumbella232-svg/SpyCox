FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache gcc musl-dev

COPY prediction_bot.py .
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python3", "prediction_bot.py"]
