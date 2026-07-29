FROM python:3.12-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl faketime \
    && library_path="$(dpkg-query -L libfaketime | awk '/\/faketime\/libfaketime.so.1$/ { print; exit }')" \
    && if [ "$library_path" != "/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1" ]; then \
         mkdir -p /usr/lib/x86_64-linux-gnu/faketime; \
         ln -s "$library_path" \
           /usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1; \
       fi \
    && test -e /usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1 \
    && rm -rf /var/lib/apt/lists/*

COPY curl_workload.sh /usr/local/bin/replayable-curl-workload
RUN chmod +x /usr/local/bin/replayable-curl-workload

WORKDIR /workspace
CMD ["replayable-curl-workload"]
