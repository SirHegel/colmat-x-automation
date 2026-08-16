# Despliegue con systemd

Las unidades asumen Linux con `systemd`, Python 3.11 o posterior, el proyecto en
`/opt/colmat-x-automation` y un usuario de servicio sin privilegios llamado `colmat`.

Adapta rutas y nombres si el servidor usa otra convención. Un despliegue inicial típico es:

```bash
sudo useradd --system --user-group --home-dir /opt/colmat-x-automation \
  --shell /usr/sbin/nologin colmat
sudo install -d -o colmat -g colmat -m 0750 /opt/colmat-x-automation
# Copia o clona el proyecto en /opt/colmat-x-automation y asigna su propiedad a colmat.
sudo chown -R colmat:colmat /opt/colmat-x-automation
sudo -u colmat python3 -m venv /opt/colmat-x-automation/.venv
sudo -u colmat /opt/colmat-x-automation/.venv/bin/pip install \
  /opt/colmat-x-automation
sudo -u colmat install -m 0600 /opt/colmat-x-automation/.env.example \
  /opt/colmat-x-automation/.env
sudo install -d -o colmat -g colmat -m 0700 /var/lib/colmat-x
```

Edita `.env`, confirma la cuenta/app correctas en X Developer Console y deja
`COLMAT_LIVE_ENABLED=false` durante la primera revisión. Define también esta ruta para que los
comandos manuales y systemd compartan exactamente la misma cola y auditoría:

```dotenv
COLMAT_STATE_DB=/var/lib/colmat-x/colmat.db
```

Ejecuta el diagnóstico con el mismo usuario que operará el servicio:

```bash
sudo -u colmat /opt/colmat-x-automation/.venv/bin/colmat-x doctor \
  --config /opt/colmat-x-automation/config/colmat.yaml
sudo -u colmat /opt/colmat-x-automation/.venv/bin/colmat-x doctor --credentials \
  --config /opt/colmat-x-automation/config/colmat.yaml
```

Instala las unidades:

```bash
sudo install -m 0644 deploy/systemd/colmat-x.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/colmat-x.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

No arranques ni habilites todavía el timer: la unidad siempre solicita modo real y fallaría de
forma periódica mientras `COLMAT_LIVE_ENABLED=false`.

`StateDirectory=colmat-x` conserva `/var/lib/colmat-x` con dueño y permisos apropiados. La unidad
también fija allí la base SQLite mediante `COLMAT_STATE_DB`. El mismo valor en `.env` evita que
`doctor`, `approve`, la prueba manual y el timer operen colas diferentes.

Observa las ejecuciones y alertas:

```bash
systemctl status colmat-x.timer colmat-x.service
journalctl -u colmat-x.service --since today
journalctl -u colmat-x.service -f
```

Trata una salida `unknown` como alerta operativa: el publicador detiene toda la cola real hasta
que alguien comprueba X y concilia el ID o confirma que no se publicó.

Parada de emergencia:

```bash
sudo systemctl disable --now colmat-x.timer
sudo systemctl stop colmat-x.service
# Cambia COLMAT_LIVE_ENABLED=false en /opt/colmat-x-automation/.env antes de reactivar.
```

Haz copias de la base porque contiene la protección histórica contra duplicados. Con el servicio
detenido y `sqlite3` instalado:

```bash
sudo install -d -m 0700 /var/backups/colmat-x
sudo sqlite3 /var/lib/colmat-x/colmat.db \
  ".backup '/var/backups/colmat-x/colmat-$(date +%F).db'"
sudo chmod 0600 /var/backups/colmat-x/*.db
```

No copies solo `colmat.db` mientras haya una ejecución activa: SQLite puede tener datos en los
archivos WAL. Para restaurar, mantén timer y servicio detenidos, conserva una copia de la base
actual y reemplaza la base con dueño `colmat:colmat` y modo `0600`.

El timer sondea cada cinco minutos y puede añadir hasta unos segundos de dispersión. Al volver de
un apagado procesa la cola vencida; no promete publicar al segundo exacto.

`doctor --credentials` valida configuración y presencia de secretos, pero no llama a X ni confirma
la identidad de la cuenta, sus créditos o sus permisos. Antes de activar el timer, usa una cuenta
controlada, prepara un solo snapshot aprobado, cambia `COLMAT_LIVE_ENABLED=true` y ejecuta una
prueba manual. Si el resultado y la auditoría local son correctos, habilita la programación:

```bash
sudo -u colmat /opt/colmat-x-automation/.venv/bin/colmat-x run-due --live \
  --config /opt/colmat-x-automation/config/colmat.yaml
sudo systemctl enable --now colmat-x.timer
systemctl list-timers colmat-x.timer
```
