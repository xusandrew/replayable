FROM replayable/agent-base:local

COPY anthropic_workload.sh /usr/local/bin/replayable-anthropic-demo
RUN chmod +x /usr/local/bin/replayable-anthropic-demo

WORKDIR /workspace
CMD ["replayable-anthropic-demo"]
