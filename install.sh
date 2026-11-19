#!/usr/bin/env bash
# YAL VPN Bot - نصب‌کننده (Ubuntu / Debian، با دسترسی root)
#   sudo bash install.sh           نصب یا تنظیم مجدد
#   sudo bash install.sh update    به‌روزرسانی کد (git pull و ریستارت)
#   sudo bash install.sh status    وضعیت سرویس
# این اسکریپت به اوتلاین، رتهول و تانل‌ها دست نمی‌زند و فقط ربات و مینی‌اپ را نصب می‌کند.
set -Eeuo pipefail
trap 'echo "✖ خطا در خط $LINENO" >&2' ERR

APP_DIR="${APP_DIR:-/opt/vpnbot}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
SERVICE="vpnbot"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$APP_DIR/.env"
WEB_PORT=8080

G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'
say()  { printf '%s▶%s %s\n' "$G" "$N" "$*"; }
warn() { printf '%s⚠%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '%s✖ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

ask() {  # ask VAR "متن" [پیش‌فرض]
  local __var=$1 __prompt=$2 __def=${3:-} __ans
  if [[ -n $__def ]]; then read -rp "$__prompt [$__def]: " __ans </dev/tty; __ans=${__ans:-$__def}
  else read -rp "$__prompt: " __ans </dev/tty; fi
  printf -v "$__var" '%s' "$__ans"
}
ask_secret() { local __var=$1 __ans; read -rsp "$2: " __ans </dev/tty; echo; printf -v "$__var" '%s' "$__ans"; }
yesno() { local d=${2:-n} a; read -rp "$1 [$([[ $d == y ]] && echo Y/n || echo y/N)]: " a </dev/tty; a=${a:-$d}; [[ $a =~ ^[Yy]$ ]]; }

valid_ids()    { [[ $1 =~ ^[0-9]+(,[0-9]+)*$ ]]; }
valid_int()    { [[ $1 =~ ^[0-9]+$ ]]; }
valid_domain() { [[ $1 =~ ^([A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}$ ]]; }
valid_hosts()  { local h arr; IFS=',' read -ra arr <<<"$1"; [[ ${#arr[@]} -gt 0 ]] || return 1; for h in "${arr[@]}"; do valid_domain "$h" || return 1; done; }
# curl بدون نشان دادن آدرس (که شامل راز است) توی لیست پروسه‌ها
scurl() { local url=$1; shift; curl -s --max-time 15 "$@" -K - <<<"url = \"$url\""; }

preflight() {
  [[ $EUID -eq 0 ]] || die "با root اجرا کن: sudo bash install.sh"
  command -v apt-get >/dev/null || die "فقط Ubuntu/Debian پشتیبانی می‌شود."
  for f in bot.py index.html requirements.txt; do [[ -f $SRC/$f ]] || die "فایل $f کنار اسکریپت نیست."; done
  say "نصب بسته‌های لازم..."
  apt-get update -y >/dev/null
  DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv curl openssl ca-certificates >/dev/null
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die "پایتون ۳.۹ یا بالاتر لازم است."
}

collect_outline() {
  local acc=/opt/outline/access.txt
  API=""; CERT=""
  if [[ -r $acc ]] && grep -q '^apiUrl:' "$acc"; then
    API=$(grep '^apiUrl:' "$acc" | cut -d: -f2- | tr -d ' \r')
    CERT=$(grep '^certSha256:' "$acc" | cut -d: -f2- | tr -d ' \r')
    say "اوتلاین روی همین سرور پیدا شد."
    yesno "از همین اطلاعات اتصال استفاده شود؟" y || { API=""; CERT=""; }
  fi
  if [[ -z $API ]]; then
    ask_secret API "OUTLINE apiUrl (مثل https://IP:PORT/SECRET)"
    ask CERT "OUTLINE certSha256 (۶۴ کاراکتر)"
  fi
  [[ $API =~ ^https://[^/]+/[A-Za-z0-9_-]+$ ]] || die "فرمت apiUrl درست نیست."
  CERT=${CERT//:/}; CERT=${CERT,,}
  [[ $CERT =~ ^[0-9a-f]{64}$ ]] || die "certSha256 باید ۶۴ کاراکتر هگز باشد."
  verify_outline
}

verify_outline() {
  local hostport=${API#https://} got info n name
  hostport=${hostport%%/*}
  got=$(echo | timeout 15 openssl s_client -connect "$hostport" 2>/dev/null | openssl x509 -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2 | tr -d ':' | tr 'A-F' 'a-f') || true
  [[ -n $got ]] || die "به $hostport وصل نشدم؛ پورت مدیریت اوتلاین باز است؟"
  [[ $got == "$CERT" ]] || die "اثر انگشت گواهی با certSha256 نمی‌خواند."
  info=$(scurl "$API/server" -k) || die "API اوتلاین جواب نداد."
  name=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("name",""))' "$info") || die "پاسخ API اوتلاین معتبر نیست."
  n=$(scurl "$API/access-keys" -k | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["accessKeys"]))') || die "خواندن کلیدهای اوتلاین ممکن نشد."
  say "اتصال به اوتلاین موفق بود (نام سرور: ${name:-بدون نام}، تعداد کلیدها: $n)"
}

collect_telegram() {
  local r
  ask_secret TOKEN "توکن ربات (از BotFather)"
  [[ $TOKEN =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || die "فرمت توکن درست نیست."
  r=$(scurl "https://api.telegram.org/bot${TOKEN}/getMe") || die "به تلگرام وصل نشدم."
  BOT_USER=$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(d["result"]["username"] if d.get("ok") else "")' "$r" 2>/dev/null) || BOT_USER=""
  [[ -n $BOT_USER ]] || die "تلگرام توکن را قبول نکرد."
  say "ربات پیدا شد: @$BOT_USER"
}

collect_settings() {
  while :; do ask ADMINS "ADMIN_IDS: آیدی عددی ادمین اصلی (چند تا؟ با کاما)"; valid_ids "$ADMINS" && break; warn "فقط عدد و کاما."; done
  ask SUPPORT "یوزرنیم تلگرام پشتیبانی (بدون @، می‌تواند خالی باشد)" ""; SUPPORT=${SUPPORT#@}
  while :; do ask CARD "اطلاعات واریز (شماره کارت و نام صاحب کارت)"; [[ -n $CARD ]] && break; warn "خالی نباشد."; done
  while :; do ask HOSTS "دامنه‌های ورودی مشتری، با کاما (مثلا ir1.example.com,ir2.example.com)"; HOSTS=${HOSTS// /}; valid_hosts "$HOSTS" && break; warn "دامنه‌ها را درست و با کاما بنویس."; done
  while :; do ask PRICE "قیمت هر گیگ (تومان)" 5000; valid_int "$PRICE" && break; warn "فقط عدد."; done
  while :; do ask PLAN_SIZES "حجم پلن‌ها (گیگ، با کاما)" "20,40,60,80,100"; valid_ids "$PLAN_SIZES" && break; warn "فقط عدد و کاما."; done
  while :; do ask ADDON_SIZES "حجم‌های «افزایش حجم» (گیگ، با کاما)" "10,20,40,60,100"; valid_ids "$ADDON_SIZES" && break; warn "فقط عدد و کاما."; done
  while :; do ask FACTOR "ضریب حجم تانل (۲ یعنی خرید ۱۰ گیگ ← سقف ۵ گیگ روی اوتلاین)" 2; [[ $FACTOR =~ ^[0-9]+(\.[0-9]+)?$ ]] && break; warn "عدد وارد کن."; done
  while :; do ask GRACE "مهلت تمدید بعد از انقضا (روز، ۰ = حذف فوری)" 7; valid_int "$GRACE" && break; warn "فقط عدد."; done
  while :; do ask WDOMAIN "دامنه‌ی مینی‌اپ (مثلا app.example.com؛ خالی = بدون مینی‌اپ)" ""; [[ -z $WDOMAIN ]] || valid_domain "$WDOMAIN" && break; warn "دامنه نامعتبر است."; done
}

pick_port() {
  if ss -tln 2>/dev/null | awk '{print $4}' | grep -q ":$WEB_PORT\$" && ! systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    warn "پورت داخلی $WEB_PORT اشغال است."
    while :; do ask WEB_PORT "یک پورت داخلی دیگر" 8081; valid_int "$WEB_PORT" && break; done
  fi
}

setup_https() {
  [[ -n ${WDOMAIN:-} ]] || return 0
  local ip dns busy cf=/etc/caddy/Caddyfile
  ip=$(curl -s4 --max-time 10 https://api.ipify.org || true)
  dns=$(getent ahostsv4 "$WDOMAIN" 2>/dev/null | awk 'NR==1{print $1}' || true)
  if [[ -z $dns ]]; then warn "دامنه هنوز resolve نمی‌شود. رکورد A را به IP این سرور (${ip:-؟}) اشاره بده."; yesno "ادامه بدهم؟" n || die "اول DNS را درست کن و دوباره اجرا کن."
  elif [[ -n $ip && $dns != "$ip" ]]; then warn "دامنه به $dns اشاره می‌کند ولی IP این سرور $ip است."; yesno "ادامه بدهم؟" n || die "اول DNS را درست کن و دوباره اجرا کن."; fi
  busy=$(ss -tlnpH 2>/dev/null | awk '$4 ~ /:(80|443)$/ && $0 !~ /caddy/ {print $4}' | head -1 || true)
  if [[ -n $busy ]]; then
    warn "پورت 80/443 توسط سرویس دیگری گرفته شده ($busy)؛ نصب Caddy رد شد."
    warn "خودت reverse proxy را برای $WDOMAIN به 127.0.0.1:$WEB_PORT وصل کن."
    return 0
  fi
  if ! command -v caddy >/dev/null; then
    say "نصب Caddy (HTTPS خودکار)..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -y >/dev/null; DEBIAN_FRONTEND=noninteractive apt-get install -y caddy >/dev/null
  fi
  touch "$cf"
  grep -q 'root \* /usr/share/caddy' "$cf" && : > "$cf"      # Caddyfile پیش‌فرض بسته
  sed -i '/# >>> yal-vpn-bot/,/# <<< yal-vpn-bot/d' "$cf"
  if grep -qE "^[[:space:]]*${WDOMAIN//./\\.}([[:space:],{]|$)" "$cf"; then
    warn "در Caddyfile از قبل برای $WDOMAIN تنظیمی هست؛ دستش نمی‌زنم. مطمئن شو به 127.0.0.1:$WEB_PORT پروکسی می‌شود."
  else
    printf '\n# >>> yal-vpn-bot\n%s {\n    encode gzip\n    reverse_proxy 127.0.0.1:%s\n}\n# <<< yal-vpn-bot\n' "$WDOMAIN" "$WEB_PORT" >> "$cf"
  fi
  systemctl enable --now caddy >/dev/null 2>&1 || true
  systemctl reload caddy || systemctl restart caddy || warn "Caddy بالا نیامد: journalctl -u caddy -n 20 --no-pager"
}

deploy_files() {
  install -d "$APP_DIR" "$APP_DIR/webapp"
  install -m 644 "$SRC/bot.py" "$APP_DIR/bot.py"
  install -m 644 "$SRC/index.html" "$APP_DIR/webapp/index.html"
  install -m 644 "$SRC/requirements.txt" "$APP_DIR/requirements.txt"
  [[ -x $APP_DIR/venv/bin/python ]] || python3 -m venv "$APP_DIR/venv"
  say "نصب وابستگی‌های پایتون..."
  "$APP_DIR/venv/bin/pip" install -q --upgrade pip
  "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
  curl -fsSL https://telegram.org/js/telegram-web-app.js -o "$APP_DIR/webapp/telegram-web-app.js.new" \
    && mv "$APP_DIR/webapp/telegram-web-app.js.new" "$APP_DIR/webapp/telegram-web-app.js" \
    || { rm -f "$APP_DIR/webapp/telegram-web-app.js.new"; [[ -f $APP_DIR/webapp/telegram-web-app.js ]] || warn "دانلود telegram-web-app.js ممکن نشد؛ مینی‌اپ کار نمی‌کند."; }
}

write_env() {
  install -d "$APP_DIR"
  [[ -f $ENV_FILE ]] && cp -p "$ENV_FILE" "$ENV_FILE.bak-$(date +%s)"
  ( umask 077
    {
      printf 'BOT_TOKEN=%s\n' "$TOKEN"
      printf 'ADMIN_IDS=%s\n' "$ADMINS"
      printf 'OUTLINE_API_URL=%s\n' "$API"
      printf 'OUTLINE_CERT_SHA256=%s\n' "$CERT"
      printf 'HOSTS=%s\n' "$HOSTS"
      printf 'CARD_INFO=%s\n' "$CARD"
      printf 'SUPPORT_USERNAME=%s\n' "$SUPPORT"
      printf 'WEBAPP_URL=%s\n' "${WDOMAIN:+https://$WDOMAIN}"
      printf 'WEB_PORT=%s\n' "$WEB_PORT"
      printf 'PRICE_PER_GB=%s\n' "$PRICE"
      printf 'PLAN_SIZES=%s\n' "$PLAN_SIZES"
      printf 'ADDON_SIZES=%s\n' "$ADDON_SIZES"
      printf 'TRAFFIC_FACTOR=%s\n' "$FACTOR"
      printf 'GRACE_DAYS=%s\n' "$GRACE"
    } > "$ENV_FILE" )
  chmod 600 "$ENV_FILE"
}

write_service() {
  cat > "$UNIT_DIR/$SERVICE.service" <<EOF
[Unit]
Description=YAL VPN sales bot
After=network-online.target docker.service
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null 2>&1
  systemctl restart "$SERVICE"
}

setup_backup() {
  yesno "بکاپ روزانه‌ی دیتابیس (نگهداری ۱۴ روز) فعال شود؟" y || return 0
  cat > /etc/cron.daily/vpnbot-backup <<EOF
#!/bin/sh
mkdir -p /root/vpnbot-backups
$APP_DIR/venv/bin/python -c "import sqlite3; s=sqlite3.connect('$APP_DIR/bot.db'); d=sqlite3.connect('/root/vpnbot-backups/bot-\$(date +%F).db'); s.backup(d)" 2>/dev/null
find /root/vpnbot-backups -name 'bot-*.db' -mtime +14 -delete
EOF
  chmod +x /etc/cron.daily/vpnbot-backup
}

health() {
  local i
  say "بررسی راه‌افتادن ربات..."
  for i in $(seq 1 15); do
    if systemctl is-active --quiet "$SERVICE" && journalctl -u "$SERVICE" -n 40 --no-pager 2>/dev/null | grep -q "Run polling"; then say "ربات بالا آمد ✅"; return 0; fi
    sleep 2
  done
  journalctl -u "$SERVICE" -n 25 --no-pager || true
  die "ربات درست بالا نیامد؛ لاگ بالا را ببین."
}

check_https() {
  local i code domain
  domain=$(grep '^WEBAPP_URL=' "$ENV_FILE" | cut -d= -f2- | sed 's#https://##')
  [[ -n $domain ]] || return 0
  say "بررسی HTTPS مینی‌اپ (گرفتن گواهی ممکن است تا یک دقیقه طول بکشد)..."
  for i in $(seq 1 12); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "https://$domain/api/me" || true)
    [[ $code == 401 ]] && { say "مینی‌اپ آماده است: https://$domain ✅"; return 0; }
    sleep 5
  done
  warn "HTTPS هنوز جواب نداد. DNS و پورت‌های 80 و 443 را چک کن: journalctl -u caddy -n 20 --no-pager"
}

cmd_install() {
  preflight
  if [[ -f $ENV_FILE ]] && ! yesno "تنظیمات قبلی پیدا شد. دوباره پرسیده و عوض شود؟ (نه = فقط کد به‌روز شود)" n; then
    deploy_files; systemctl restart "$SERVICE"; health; check_https; return 0
  fi
  collect_outline
  collect_telegram
  collect_settings
  pick_port
  setup_https
  write_env
  deploy_files
  write_service
  setup_backup
  health
  check_https
  echo
  say "تمام شد. ربات: https://t.me/${BOT_USER}"
  echo "  1) توی تلگرام /start بزن؛ با حساب ادمین اصلی دکمه‌های مدیریت میاد."
  echo "  2) وضعیت: sudo bash install.sh status"
  echo "  3) به‌روزرسانی: git pull && sudo bash install.sh update"
}

cmd_update() {
  [[ $EUID -eq 0 ]] || die "با root اجرا کن."
  [[ -f $ENV_FILE ]] || die "ابتدا نصب کن: sudo bash install.sh"
  if [[ -d $SRC/.git ]]; then say "git pull"; git -C "$SRC" pull --ff-only; fi
  deploy_files; systemctl restart "$SERVICE"; health
}

cmd_status() {
  systemctl status "$SERVICE" --no-pager | head -8 || true
  journalctl -u "$SERVICE" -n 15 --no-pager || true
}

main() {
  case "${1:-install}" in
    install) cmd_install ;;
    update)  cmd_update ;;
    status)  cmd_status ;;
    *) echo "استفاده: sudo bash install.sh [install|update|status]"; exit 1 ;;
  esac
}
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
