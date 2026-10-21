FROM python:3.12-alpine
RUN apk add --no-cache --upgrade "ca-certificates>=20261020-r0"

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY tests/test_ca_store.py /tmp/test_ca_store.py
RUN python /tmp/test_ca_store.py && rm /tmp/test_ca_store.py

COPY jwt_min.py varlink_pub.py app.py ./

# Port 9081: Reefy infra-sidecar block (9080-9099). Reached by sibling
# app containers as http://reefy-app-api:9081 over the docker network;
# not host-bound.
EXPOSE 9081
# Single worker is plenty (notify is infrequent); threads handle
# concurrent uploads. The only slow step is the short per-publish MQTT
# connect.
CMD ["gunicorn", "--bind", "0.0.0.0:9081", "--workers", "1", \
     "--threads", "8", "--timeout", "120", "app:app"]
