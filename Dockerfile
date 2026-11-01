FROM python:3.12-alpine
WORKDIR /app
COPY app.py index.html login.html ./
COPY static ./static
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "app.py"]
