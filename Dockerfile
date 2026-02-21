FROM python:3.12
ENV PYTHONUNBUFFERED 1
ENV ENV docker
RUN mkdir /opt/sentry
WORKDIR /opt/sentry
RUN apt-get update && apt-get install -y git build-essential libffi-dev libssl-dev
RUN pip install --no-cache-dir gevent==23.9.1 greenlet==3.0.3
ADD requirements.txt /opt/sentry/
RUN pip install --no-cache-dir -r /opt/sentry/requirements.txt
RUN pip install --no-cache-dir --no-deps git+https://github.com/b1naryth1ef/disco.git@master
ADD . /opt/sentry/
RUN sed -i "s/int(response.headers.get('X-RateLimit-Reset'))/float(response.headers.get('X-RateLimit-Reset'))/g" /usr/local/lib/python3.12/site-packages/disco/api/ratelimit.py