FROM python:3.12
ENV PYTHONUNBUFFERED 1
ENV ENV docker
RUN mkdir /opt/sentry
WORKDIR /opt/sentry
# Install build essentials for C-extensions
RUN apt-get update && apt-get install -y git build-essential libffi-dev libssl-dev
# Force install modernized gevent before anything else
RUN pip install --no-cache-dir gevent==23.9.1 greenlet==3.0.3
# Install the requirements (now lacking the conflicting disco/gevent lines)
ADD requirements.txt /opt/sentry/
RUN pip install --no-cache-dir -r /opt/sentry/requirements.txt
# Force install disco-py while ignoring its ancient internal gevent requirements
RUN pip install --no-cache-dir --no-deps git+https://github.com/b1naryth1ef/disco.git@master
# Add the rest of the code
ADD . /opt/sentry/