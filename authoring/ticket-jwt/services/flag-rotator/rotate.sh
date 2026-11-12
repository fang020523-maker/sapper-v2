#!/bin/sh
set -e
: "${ANSWER_FLAG:=FLAG{samplemainanswer}}"
printf '%s' "$ANSWER_FLAG" > /out/flag
