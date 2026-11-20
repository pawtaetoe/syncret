#!/bin/sh
awk -F, 'NR > 1 { total += $3 } END { print total }' "$1"
