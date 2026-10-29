#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
"$script_dir/run-scheduled.sh" check
"$script_dir/run-scheduled.sh" build
