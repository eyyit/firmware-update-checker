#!/bin/sh
set -eu
# Host runner only. No Docker socket is mounted into any build/test container.
mode=${1:?Expected check or build}
case "$mode" in check|build) ;; *) exit 2 ;; esac
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$script_dir"
notify_on_exit() {
  result=$?
  trap - 0
  if ! python3 -B "$script_dir/notify.py" --mode "$mode" --exit-code "$result"; then
    echo 'Firmware email submission failed; pending notifications will retry at the next scheduled run.' >&2
  fi
  exit "$result"
}
trap notify_on_exit 0
if [ "$mode" = build ]; then
  ionice -c3 nice -n 19 python3 -B orchestrate.py build --refresh-test-image
else
  ionice -c3 nice -n 19 python3 -B orchestrate.py check
fi
