FROM python:3.12
ENV PYTHONUNBUFFERED 1
ENV ENV docker
RUN mkdir /opt/sentry
ADD requirements.txt /opt/sentry/
RUN pip install -r /opt/sentry/requirements.txt
ADD . /opt/sentry/
WORKDIR /opt/sentry