from python:3.11

MAINTAINER brainiac2k@gmail.com

COPY . /app
WORKDIR /app

RUN pip install -r requirements.txt

CMD ["python", "cox-status.py"]
