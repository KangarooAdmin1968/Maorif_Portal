#!/usr/bin/env bash
# Maorif Portal — тўлиқ миграция бэкапи.
#
# Бекзоджон учун: серверга SSH орқали кириб, бир команда ишга туширинг:
#     sudo bash backup_for_migration.sh
#
# Скрипт ўзи loyiҳани топади, маълумотлар базаси + media + созламаларни
# битта .tar.gz файлига жамлайди ва охирида файл номи + юклаш йўлини
# ёзади. Сайт ишлашда давом этади — ҳеч нарса тўхтамайди.
set -u

STAMP="$(date +%Y%m%d_%H%M)"
WORK="/tmp/maorif_mig_$STAMP"
OUT="/root/maorif_full_backup_$STAMP.tar.gz"

echo "=== Maorif Portal бэкапи: $STAMP ==="
mkdir -p "$WORK"

# --- 1) Лойиҳа папкасини топиш ------------------------------------------
APP_DIR=""
for d in /root/Maorif_Portal /root/maorif_portal /root/maorif \
         /var/www/maorif* /var/www/Maorif* /opt/maorif* /opt/Maorif* \
         /home/*/Maorif_Portal /home/*/maorif*; do
    [ -d "$d" ] && APP_DIR="$d" && break
done
# Agar topilmasa — db.sqlite3 бўйича қидирамиз (chuqurlik 4)
if [ -z "$APP_DIR" ]; then
    DBF="$(find /root /var/www /opt /home -maxdepth 4 -name db.sqlite3 2>/dev/null | head -1)"
    [ -n "$DBF" ] && APP_DIR="$(dirname "$DBF")"
fi
if [ -z "$APP_DIR" ]; then
    echo "!! Лойиҳа папкаси топилмади. Қўлда APP_DIR'ни кўрсатинг."
    exit 1
fi
echo "Лойиҳа: $APP_DIR"

# --- 2) Маълумотлар базаси (xavfsiz snapshot) ----------------------------
DB="$APP_DIR/db.sqlite3"
if [ -f "$DB" ]; then
    mkdir -p "$WORK/app"
    # sqlite3 .backup — jonli базадан izchil nusxa (yozuvlar davomida ham toza)
    if command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "$DB" ".backup '$WORK/app/db.sqlite3'"
    else
        cp "$DB" "$WORK/app/db.sqlite3"
    fi
    echo "✔ db.sqlite3 ($(du -h "$DB" | cut -f1))"
else
    echo "!! db.sqlite3 топилмади: $DB"
fi

# --- 3) media/ (arxivlar, yuklangan fayllar) -----------------------------
if [ -d "$APP_DIR/media" ]; then
    cp -a "$APP_DIR/media" "$WORK/app/media"
    echo "✔ media/ ($(du -sh "$APP_DIR/media" | cut -f1))"
else
    echo "… media/ йўқ"
fi

# --- 4) .env ва loyiҳа созламалари ---------------------------------------
for f in .env .env.production deploy requirements.txt; do
    [ -e "$APP_DIR/$f" ] && cp -a "$APP_DIR/$f" "$WORK/app/" && echo "✔ $f"
done
# Joriy git commit (qaysi versiya turibdi)
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" rev-parse HEAD > "$WORK/app/GIT_COMMIT" 2>/dev/null \
        && echo "✔ GIT_COMMIT: $(cat "$WORK/app/GIT_COMMIT")"
    git -C "$APP_DIR" status --porcelain > "$WORK/app/GIT_STATUS.txt" 2>/dev/null
fi

# --- 5) Тизим созламалари -------------------------------------------------
mkdir -p "$WORK/system"
# Cron — refresh_rankings va boshqalar
crontab -l > "$WORK/system/crontab.txt" 2>/dev/null \
    && echo "✔ crontab ($(wc -l < "$WORK/system/crontab.txt") qator)"
[ -f /var/spool/cron/crontabs/root ] \
    && cp /var/spool/cron/crontabs/root "$WORK/system/crontab_root.txt" 2>/dev/null
# nginx
for f in /etc/nginx/sites-enabled/* /etc/nginx/sites-available/*maorif* \
         /etc/nginx/conf.d/*maorif*; do
    [ -f "$f" ] && cp "$f" "$WORK/system/" 2>/dev/null
done
ls "$WORK/system" | grep -q . && echo "✔ nginx конфиглари"
# gunicorn / systemd
for f in /etc/systemd/system/*gunicorn* /etc/systemd/system/*maorif* \
         /etc/systemd/system/*daphne*; do
    [ -f "$f" ] && cp "$f" "$WORK/system/" 2>/dev/null
done
# SSL sertifikat sozlamasi (certbot renewal conf — kalitlarsiz)
[ -d /etc/letsencrypt/renewal ] \
    && cp -a /etc/letsencrypt/renewal "$WORK/system/letsencrypt_renewal" 2>/dev/null

# --- 6) Tar.gz ------------------------------------------------------------
tar -czf "$OUT" -C "$WORK" .
rm -rf "$WORK"
echo ""
echo "=========================================="
echo "✅ БЭКАП ТАЙЁР: $OUT"
echo "   Ҳажм: $(du -h "$OUT" | cut -f1)"
echo "=========================================="
echo ""
echo "Файлни юбориш йўллари (бирини танланг):"
echo ""
echo "  A) Ўз компьютерингизга юклаб олиш (PowerShell/GitBash):"
echo "     scp root@$(hostname -I | awk '{print $1}'):$OUT ."
echo ""
echo "  B) Бевосита file.io'га юклаш (24 соатлик ҳавола беради):"
echo "     curl -F \"file=@$OUT\" https://file.io"
echo ""
echo "  C) Google Drive / Telegram орқали юбориш — аввал A йўли билан"
echo "     компьютерга олинг."
echo ""
echo "Файлни Зафаробод маориф бошқармасига юборинг."
