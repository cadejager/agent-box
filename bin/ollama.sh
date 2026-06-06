#!/usr/bin/env bash
#
# Controls the ollama instance. Thin wrapper around service.sh.
exec "$( dirname -- "${BASH_SOURCE[0]}" )/service.sh" ollama "$@"
