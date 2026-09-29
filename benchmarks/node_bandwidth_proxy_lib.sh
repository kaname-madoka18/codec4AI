#!/usr/bin/env bash

# Runtime-only node aggregate bandwidth limiter for the 3.4 benchmark.

nodebw_install_squid() {
  local squid_bin proxychains_bin
  squid_bin="$(command -v squid || true)"
  proxychains_bin="$(command -v proxychains4 || true)"
  if [[ -n "${squid_bin}" && -n "${proxychains_bin}" ]]; then
    printf '%s\n' "${squid_bin}"
    return 0
  fi

  unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "Squid/proxychains are absent and apt-get is unavailable" >&2
    return 1
  fi

  local os_id os_codename apt_state source_list
  os_id="$(. /etc/os-release && printf '%s' "${ID:-}")"
  os_codename="$(. /etc/os-release && printf '%s' "${VERSION_CODENAME:-}")"
  if [[ "${os_id}" != "ubuntu" || -z "${os_codename}" ]]; then
    echo "unsupported base OS: id=${os_id} codename=${os_codename}" >&2
    return 1
  fi

  apt_state="$(mktemp -d /tmp/codec-3-4-nodebw-apt.XXXXXX)"
  source_list="${apt_state}/aliyun.sources.list"
  cat >"${source_list}" <<EOF
deb https://mirrors.aliyun.com/ubuntu/ ${os_codename} main restricted universe multiverse
deb https://mirrors.aliyun.com/ubuntu/ ${os_codename}-updates main restricted universe multiverse
deb https://mirrors.aliyun.com/ubuntu/ ${os_codename}-security main restricted universe multiverse
EOF
  export DEBIAN_FRONTEND=noninteractive
  apt-get \
    -o "Dir::Etc::sourcelist=${source_list}" \
    -o "Dir::Etc::sourceparts=-" \
    -o "Acquire::Retries=3" \
    update >&2
  apt-get \
    -o "Dir::Etc::sourcelist=${source_list}" \
    -o "Dir::Etc::sourceparts=-" \
    -o "Acquire::Retries=3" \
    install -y --no-install-recommends squid proxychains4 >&2
  rm -rf -- "${apt_state}"

  squid_bin="$(command -v squid || true)"
  if [[ -z "${squid_bin}" && -x /usr/sbin/squid ]]; then
    squid_bin=/usr/sbin/squid
  fi
  test -x "${squid_bin:?Squid install completed but binary is missing}"
  test -x "$(command -v proxychains4)"
  printf '%s\n' "${squid_bin}"
}

nodebw_write_proxychains_config() {
  local state_dir="$1"
  local port="${2:-3128}"
  local config="${state_dir}/proxychains.conf"
  cat >"${config}" <<EOF
strict_chain
quiet_mode
tcp_read_time_out 15000
tcp_connect_time_out 8000
localnet 127.0.0.0/255.0.0.0
localnet ::1/128
[ProxyList]
http 127.0.0.1 ${port}
EOF
  export NODEBW_PROXYCHAINS_CONFIG="${config}"
  export NODEBW_PROXYCHAINS_BIN
  NODEBW_PROXYCHAINS_BIN="$(command -v proxychains4)"
  test -x "${NODEBW_PROXYCHAINS_BIN}"
  echo "NODE_BANDWIDTH_PROXYCHAINS_READY binary=${NODEBW_PROXYCHAINS_BIN} config=${NODEBW_PROXYCHAINS_CONFIG}"
}

nodebw_start_proxy() {
  local bandwidth_mib="$1"
  local state_dir="$2"
  local port="${3:-3128}"
  case "${bandwidth_mib}" in
    20|50|100|200|400|800) ;;
    *) echo "invalid node bandwidth: ${bandwidth_mib} MiB/s" >&2; return 2 ;;
  esac

  local squid_bin rate_bytes worker_count worker_rate_bytes config
  squid_bin="$(nodebw_install_squid)"
  rate_bytes="$((bandwidth_mib * 1024 * 1024))"
  worker_count=1
  if [[ "${bandwidth_mib}" == "800" ]]; then
    worker_count=2
  fi
  worker_rate_bytes="$((rate_bytes / worker_count))"

  mkdir -p "${state_dir}/logs"
  config="${state_dir}/squid.conf"
  cat >"${config}" <<EOF
http_port 127.0.0.1:${port}
workers ${worker_count}
visible_hostname codec-ai-3-4-node-bandwidth
pid_filename ${state_dir}/squid.pid
coredump_dir ${state_dir}
cache_mem 0 MB
maximum_object_size 0 KB
cache deny all
access_log stdio:${state_dir}/logs/access.log squid
cache_log ${state_dir}/logs/cache.log
cache_store_log none
logfile_rotate 0
acl localhost_only src 127.0.0.1/32 ::1
http_access allow localhost_only
http_access deny all
delay_pools 1
delay_initial_bucket_level 0
delay_class 1 1
delay_access 1 allow localhost_only
delay_parameters 1 ${worker_rate_bytes}/${worker_rate_bytes}
shutdown_lifetime 1 seconds
EOF

  if ! id proxy >/dev/null 2>&1; then
    echo "Squid package did not create the proxy user" >&2
    return 1
  fi
  chown -R proxy:proxy "${state_dir}"

  if (( worker_count == 1 )); then
    "${squid_bin}" -N -f "${config}" -d 1 >"${state_dir}/logs/stdout.log" 2>"${state_dir}/logs/stderr.log" &
    NODEBW_SQUID_PID=$!
  else
    "${squid_bin}" -f "${config}" -d 1 >"${state_dir}/logs/stdout.log" 2>"${state_dir}/logs/stderr.log" &
    local launcher_pid=$!
    wait "${launcher_pid}"
    local attempt
    for attempt in $(seq 1 100); do
      if [[ -s "${state_dir}/squid.pid" ]]; then
        NODEBW_SQUID_PID="$(tr -cd '0-9' <"${state_dir}/squid.pid")"
        break
      fi
      sleep 0.1
    done
  fi
  export NODEBW_SQUID_PID
  test -n "${NODEBW_SQUID_PID:-}"
  export NODEBW_PROXY_URL="http://127.0.0.1:${port}"
  export NODEBW_SQUID_ACCESS_LOG="${state_dir}/logs/access.log"
  export NODEBW_SQUID_CONFIG="${config}"
  export NODEBW_RATE_BYTES="${rate_bytes}"
  export NODEBW_SQUID_WORKERS="${worker_count}"
  export NODEBW_WORKER_RATE_BYTES="${worker_rate_bytes}"

  local attempt
  for attempt in $(seq 1 100); do
    if ! kill -0 "${NODEBW_SQUID_PID}" 2>/dev/null; then
      echo "Squid exited during startup" >&2
      tail -100 "${state_dir}/logs/stderr.log" >&2 || true
      return 1
    fi
    if python3 - "${port}" 2>/dev/null <<'PY'
import socket
import sys

with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=0.2):
    pass
PY
    then
      break
    fi
    sleep 0.1
  done
  python3 - "${port}" <<'PY'
import socket
import sys

with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=1):
    pass
PY

  export HTTP_PROXY="${NODEBW_PROXY_URL}"
  export HTTPS_PROXY="${NODEBW_PROXY_URL}"
  export ALL_PROXY="${NODEBW_PROXY_URL}"
  export http_proxy="${NODEBW_PROXY_URL}"
  export https_proxy="${NODEBW_PROXY_URL}"
  export all_proxy="${NODEBW_PROXY_URL}"
  export NO_PROXY="127.0.0.1,localhost"
  export no_proxy="127.0.0.1,localhost"
  echo "NODE_BANDWIDTH_PROXY_READY bandwidth_mib=${bandwidth_mib} rate_bytes=${rate_bytes} workers=${worker_count} worker_rate_bytes=${worker_rate_bytes} proxy=${NODEBW_PROXY_URL} squid_pid=${NODEBW_SQUID_PID}"
}

nodebw_stop_proxy() {
  if [[ -n "${NODEBW_SQUID_PID:-}" ]] && kill -0 "${NODEBW_SQUID_PID}" 2>/dev/null; then
    kill -TERM "${NODEBW_SQUID_PID}" 2>/dev/null || true
    wait "${NODEBW_SQUID_PID}" 2>/dev/null || true
  fi
}
