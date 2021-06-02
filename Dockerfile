FROM debian:sid

# Simple docker file for loggerhead
# To use, mount something on /code
# TODO(jelmer): Support toggling --export-tarballs
# TODO(jelmer): Support toggling whether writes are allowed (currently not allowed)
# TODO(jelmer): Support specifying host prefix

RUN apt update && apt install --no-install-recommends -y python3 python3-bleach python3-paste python3-pip python3-patiencediff python3-simpletal python3-dev build-essential python3-pastedeploy python3-dulwich python3-certifi python3-configobj && pip3 install breezy && apt clean && mkdir -p /logs
ADD . /opt/loggerhead
ENV PYTHONPATH=/opt/loggerhead
EXPOSE 8080/tcp
ENTRYPOINT ["/usr/bin/python3", "/opt/loggerhead/loggerhead-serve", "/code", "--host=0.0.0.0", "--port=8080", "--log-folder=/logs", "--export-tarballs", "--cache-dir=/tmp", "--prefix=/"]
