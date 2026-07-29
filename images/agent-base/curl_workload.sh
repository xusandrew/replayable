#!/bin/sh
set -eu

attempt=1
while [ "$attempt" -le 3 ]; do
  curl --silent --show-error https://api.github.com/zen
  printf '\n'
  attempt=$((attempt + 1))
done

curl \
  --silent \
  --show-error \
  --request POST \
  --header 'content-type: application/json' \
  --data '{"x":1}' \
  https://httpbin.org/post
printf '\n'
