# Colmat X Automation

Proyecto en Python para preparar, clasificar, revisar y publicar contenido institucional en X,
con control operativo por Telegram. Usa exclusivamente las API oficiales de X, Telegram y
MiniMax; nunca automatiza la interfaz web de X.

El proyecto nace en modo seguro: toda salida de IA es un borrador, una persona distinta del autor
debe aprobar el snapshot exacto y la publicación real exige dos seguros explícitos. El bot puede
consultar y registrar decisiones; no crea usuarios ni publica por sí mismo.

## Capacidades

- Publicaciones originales de texto, programadas en YAML.
- Plantillas Jinja reutilizables y vista previa del texto final.
- Aprobación humana auditada y ligada al hash exacto del texto y la hora revisados.
- Estimación conservadora de longitud ponderada y controles preventivos de URL, cashtags y
  duplicados exactos.
- Cola SQLite heredada para un host persistente y store SQLAlchemy para SQLite local o PostgreSQL
  en despliegues serverless.
- Autenticación OAuth 1.0a de usuario para una cuenta de Colmat.
- Modo simulación, diagnóstico, reintentos controlados y conciliación de resultados ambiguos.
- RBAC explícito para `owner`, `admin`, `editor`, `reviewer`, `publisher` y `auditor`, con
  separación entre autoría y aprobación.
- Webhook de Telegram autenticado, deduplicación de `update_id` y callbacks de un solo uso ligados
  a persona, chat, revisión y snapshot.
- MiniMax para proponer borradores e imágenes en memoria, con validación local cerrada y sin
  capacidad de aprobar, programar o publicar.
- Carga de hasta cuatro imágenes en X, texto alternativo obligatorio y marca `made_with_ai` para
  media generada con IA.
- FastAPI con `/api/health`, `/api/ready` y `/api/telegram/webhook`, desplegable en Vercel.

No automatiza respuestas, mensajes directos, likes, follows, tendencias, scraping ni segmentación
de personas. Tampoco promete viralidad: la rúbrica de engagement compara claridad, cifra temprana,
atribución y legibilidad, pero exige verificación editorial y no autoriza una publicación.

## Instalación

Requiere Python 3.11 o posterior. Desde la raíz del proyecto:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
install -m 0600 .env.example .env
colmat-x doctor
```

La base `.state/colmat.db` se crea al primer uso con permisos locales restrictivos y nunca se
incluye en Git. En sistemas sin `install`, copia el archivo y aplica permisos equivalentes para
que solo su propietario pueda leer `.env`.

## Manual canónico y política editorial

La fuente de control es el PDF de Drive con ID
`1S_870mC8iixpNRv2FYtnnLZauO0eDGww`. La política local fija tanto el ID como la huella SHA-256 del
texto extraído para contrastarla en cada nueva auditoría. Sus reglas principales están codificadas en
`config/editorial-policy.yaml`:

- `dato_semana`, `ficha_territorio`, `lamina` y `correccion_publica` son la taxonomía cerrada.
- Toda pieza incluye literalmente una cifra y una fuente que el equipo debe verificar.
- COLMAT es doctrina, la Escuela Colombiana de Filosofía es escuela y Tierra Firme es partido;
  el generador no puede confundir sus funciones.
- Las láminas exigen serie completa y eje no truncado; las correcciones no admiten atenuantes.
- La IA no reconstruye el emblema ni el Nudo del Macizo, no usa retratos de personas vivas y solo
  propone colores generativos de la paleta permitida. Los activos oficiales se incorporan después.

`colmat-x ai-draft` devuelve JSON validado con `status=draft`, una evaluación explicable y
`publication_authorized=false`:

```bash
colmat-x ai-draft "Cifra y fuente ya verificadas para el dato semanal" \
  --category dato_semana \
  --institution escuela_colombiana_de_filosofia
```

La clave se recibe solo por `MINIMAX_API_KEY`. Usa `MiniMax-M2.7` para texto e `image-01` para
imagen salvo que la documentación oficial y las pruebas justifiquen otro modelo. Nunca subas una
clave a Git; si apareció en un chat, log o captura, rótala antes de usar producción.

## Equipo, jerarquía y Telegram

El primer propietario se crea una sola vez y luego administra identidades explícitas. No inventes
cuentas compartidas: cada integrante debe tener email propio y, si usa el bot, su `from.id` real.

```bash
colmat-x team-bootstrap --email owner@example.org --display "Propietario"
colmat-x team-add --actor-id OWNER_ID --email admin@example.org \
  --display "Administración" --role admin
colmat-x team-add --actor-id OWNER_ID --email editora@example.org \
  --display "Editora" --role editor
colmat-x team-list --actor-id OWNER_ID
colmat-x telegram-bind --actor-id OWNER_ID --user-id USER_ID \
  --telegram-user-id FROM_ID --chat-id CHAT_ID --purpose control
```

`owner` puede delegar cualquier rol. `admin` gestiona las funciones operativas, pero no owners ni
otros admins. `editor` crea y modifica; `reviewer` decide; `publisher` publica; `auditor` solo
consulta trazabilidad. Un autor no puede aprobar su propia revisión.

El bot expone `/start`, `/estado`, `/equipo` y `/ayuda`. Telegram autentica por `from.id`, nunca por
`username`, y además exige el chat autorizado. Los botones llevan nonces expirables y de un solo
uso; editar texto, fecha, evidencia o imagen invalida la aprobación anterior.

## Flujo editorial

1. Duplica `content/posts/001-bienvenida.yaml` y asigna un `id` único.
2. Escoge una plantilla de `content/templates/` y completa `data`.
3. Ejecuta `colmat-x validate` y `colmat-x preview --id TU_ID`.
4. Revisa texto, hora y el hash `snapshot de aprobación` que muestra `preview`.
5. Sincroniza con `colmat-x sync`.
6. Aprueba exactamente ese snapshot:

   ```bash
   colmat-x approve TU_ID --by "Equipo editorial" --snapshot HASH_COMPLETO
   ```

7. Comprueba lo vencido con `colmat-x run-due`; esto no llama a X.
8. Solo cuando todo esté listo, habilita y ejecuta la publicación real.

La aprobación vive en SQLite, no en el YAML. Cualquier cambio de texto o de hora invalida el
snapshot y devuelve la pieza a `draft`; hay que previsualizarla y aprobarla de nuevo. `--by` es
metadato de auditoría, no autentica por sí solo a una persona: restringe también el acceso al host
y a la cuenta de servicio.

Si eliminas de `content/posts/` un YAML que estaba en borrador o programado, la siguiente
sincronización lo cancela en SQLite para que no pueda publicarse desde estado antiguo. Si repones
un archivo con el mismo ID, ejecuta `colmat-x restore TU_ID` y vuelve a aprobarlo. Puede contener
texto u hora nuevos: `restore` carga ese snapshot como `draft` y nunca conserva la aprobación
anterior. Eliminar todos los YAML es válido y cancela toda pieza activa.

Ejemplo de contenido:

```yaml
id: colmat-pregunta-001
template: idea
publish_at: "2026-09-02T09:00:00-05:00"
data:
  titulo: "¿Qué pregunta necesitamos formular mejor?"
  desarrollo: "Las ideas ganan claridad cuando se contrastan con otras perspectivas."
  cierre: "Colmat: pensamiento que se convierte en conversación."
```

`publish_at` siempre debe incluir el desplazamiento horario. El proyecto guarda UTC y muestra
las horas en `America/Bogota`, configurada en `config/colmat.yaml`.

## Comandos

```bash
colmat-x validate
colmat-x preview
colmat-x preview --id colmat-pregunta-001
colmat-x sync
colmat-x approve colmat-pregunta-001 --by "Equipo editorial" --snapshot HASH
colmat-x withdraw colmat-pregunta-001
colmat-x restore colmat-pregunta-001
colmat-x status
colmat-x status --state scheduled
colmat-x run-due                 # simulación, nunca llama a X
colmat-x doctor
colmat-x doctor --credentials    # comprueba secretos sin habilitar publicación
colmat-x x-whoami --expected-username CUENTA_INSTITUCIONAL
colmat-x team-list --actor-id USER_ID
```

`run-due` en simulación no llama a X, pero sí sincroniza YAML y puede crear, actualizar o cancelar
filas de SQLite. Un YAML inválido detiene toda la sincronización y, por seguridad, no se publica
ninguna otra pieza en esa ejecución. Aprobar una fecha ya vencida la deja lista para el siguiente
sondeo.

Campos de `data` requeridos por las plantillas incluidas:

| Plantilla | Campos |
| --- | --- |
| `idea` | `titulo`, `desarrollo`, `cierre` |
| `anuncio` | `encabezado`, `detalle`, `llamado` |
| `reflexion` | `reflexion`, `etiquetas` |

Si un cierre, llamado o etiqueta no aplica, conserva la clave con texto vacío.

Los topes están en `config/colmat.yaml`. Por defecto se procesa una publicación por ejecución y
como máximo dos por día. Las URL también están desactivadas allí porque actualmente tienen un
costo de API mucho mayor que una publicación sin URL. El límite diario es atómico para procesos
que comparten esta SQLite, pero no conoce publicaciones manuales ni otra instalación con una base
distinta.

## Credenciales y publicación real

En [X Developer Console](https://console.x.com/) crea o configura una app con permisos de
lectura y escritura y genera las credenciales OAuth 1.0a de usuario para la cuenta de Colmat.
Si cambias los permisos de una app existente, vuelve a generar sus access tokens. Completa `.env`
sin compartirlo ni subirlo a Git y conserva inicialmente el bloqueo:

```dotenv
X_CONSUMER_KEY=...
X_CONSUMER_SECRET=...
X_ACCESS_TOKEN=...
X_ACCESS_TOKEN_SECRET=...
COLMAT_LIVE_ENABLED=false
```

Después ejecuta `colmat-x doctor --credentials` y `colmat-x x-whoami` con el usuario o ID esperado.
El primero valida presencia local; el segundo contacta X y bloquea una cuenta distinta. Ninguno
confirma saldo, precios o capacidad de escritura, que deben revisarse en la consola. Realiza la
primera prueba con una cuenta controlada y una sola pieza. Solo entonces cambia el seguro:

```bash
# En .env: COLMAT_LIVE_ENABLED=true
colmat-x run-due --live
```

El cliente envía `POST https://api.x.com/2/tweets` con contexto de usuario. Un Bearer Token
app-only no sirve para crear publicaciones. El proyecto usa OAuth 1.0a porque esta versión opera
una sola cuenta desatendida; X también permite OAuth 2.0 Authorization Code con PKCE.

Un `.env` con modo `0600` es la protección local mínima. Para producción, usa el gestor de
secretos del entorno y cifrado en reposo; rota de inmediato cualquier credencial expuesta.
Cuando se selecciona otro proyecto mediante `--config` o `COLMAT_CONFIG`, solo se carga el `.env`
de ese proyecto; un `.env` del directorio de trabajo no puede sustituir sus credenciales. La ruta
de configuración debe permanecer bajo el directorio de trabajo o el perfil del usuario. Las rutas
de contenido y plantillas permanecen dentro del proyecto; la base admite además el destino de
servicio dedicado `/var/lib/colmat-x/`. La normalización previa rechaza escapes con `..`, prefijos
hermanos y enlaces simbólicos.

Solo `.env.example`, con nombres y valores secretos vacíos, pertenece al repositorio. `.env`,
`.state/`, bases SQLite, logs, entornos virtuales, cobertura y artefactos de construcción están
excluidos por `.gitignore`. No fuerces su inclusión con `git add -f`.

## Ejecución programada

Se recomienda un host persistente para conservar la base SQLite. Hay unidades e instrucciones en
`deploy/`; ajusta rutas y usuario antes de instalarlas. El temporizador sondea cada cinco minutos,
pero los límites de Colmat siguen aplicando. Opera una sola base persistente y elige **systemd o
cron**, no ambos.

Una alternativa sencilla con cron es:

```cron
*/5 * * * * cd /ruta/colmat-x-automation && .venv/bin/colmat-x run-due --live >> .state/cron.log 2>&1
```

## FastAPI, PostgreSQL y Vercel

SQLite no es almacenamiento persistente válido en Vercel. Conecta un PostgreSQL serverless (por
ejemplo Neon) y define `DATABASE_URL`, `TELEGRAM_BOT_TOKEN` y `TELEGRAM_WEBHOOK_SECRET` como
secretos del proyecto. Despliega después de ejecutar la suite:

```bash
pytest -q
vercel link
vercel --prod
curl -fsS https://TU_DOMINIO/api/health
curl -fsS https://TU_DOMINIO/api/ready
```

Solo cuando `ready` responda `200`, registra en BotFather/API de Telegram la URL HTTPS
`https://TU_DOMINIO/api/telegram/webhook` con el mismo secreto. No configures un cron de
publicación hasta implementar un worker de cola protegido y probar idempotencia en producción. En
Vercel Hobby, además, los cron solo pueden ejecutarse una vez al día y sin precisión al minuto.

Para desarrollo local:

```bash
uvicorn colmat_x.web:create_app --factory --host 127.0.0.1 --port 8000
```

El endpoint de salud no inicializa integraciones ni devuelve secretos. Readiness solo informa
`ok`, `missing` o `error`. El webhook limita el cuerpo a 1 MiB, exige JSON y el header
`X-Telegram-Bot-Api-Secret-Token`, y responde sin exponer errores internos.

El workflow de GitHub incluido ejecuta calidad y pruebas, pero no publica. Una tarea programada
de GitHub Actions no conserva SQLite de forma fiable entre ejecuciones y podría romper la
protección local contra duplicados.

## Estados y recuperación

La cola usa estos estados:

```text
draft --approve--> scheduled --claim--> publishing --éxito--> published
  ^                   |                    |----error claro--> failed
  |                   |                    +----ambiguo------> unknown
  +------withdraw-----+

draft/scheduled --archivo ausente--> cancelled --restore--> draft
```

`failed` significa que X rechazó claramente la solicitud; puede revisarse y reencolarse:

```bash
colmat-x retry TU_ID
```

`unknown` significa que la conexión terminó sin confirmar si X publicó. Nunca se reintenta de
forma automática y bloquea toda publicación real hasta su conciliación. Primero revisa
manualmente la cuenta:

```bash
# Si sí aparece en X:
colmat-x reconcile-published TU_ID ID_DEL_POST_EN_X

# Si comprobaste que no aparece:
colmat-x retry TU_ID --confirm-not-published
```

Si un lease vence, `run-due` convierte primero ese intento a `unknown` y termina sin sincronizar
ni publicar nada más. Antes de confirmar que no se publicó, verifica también que el proceso o
servicio anterior ya terminó: el control local impide que un callback tardío cambie un intento
nuevo, pero no puede cancelar una solicitud externa que todavía esté en vuelo.

Esta precaución existe porque el endpoint de creación no documenta una clave de idempotencia:
repetir a ciegas una solicitud ambigua puede duplicar el contenido.

## Reglas, límites y costo de X

Información verificada el 15 de agosto de 2026; revisa siempre la consola antes de activar el
servicio:

Configura créditos prepago, límite de gasto y alertas en la consola. El control de duplicados del
proyecto compara texto normalizado dentro de su propia SQLite; la revisión editorial todavía debe
detectar contenido sustancialmente similar o ya publicado fuera de esta instalación.

- [Crear una publicación](https://docs.x.com/x-api/posts/create-post): endpoint, cuerpo y
  respuesta oficial.
- [Autenticación de API v2](https://docs.x.com/fundamentals/authentication/guides/v2-authentication-mapping):
  crear contenido requiere contexto de usuario.
- [Conteo de caracteres](https://docs.x.com/fundamentals/counting-characters): 280 caracteres
  ponderados para una publicación estándar; las URL cuentan como 23.
- [Límites de uso](https://docs.x.com/x-api/fundamentals/rate-limits): consulta también los
  encabezados `x-rate-limit-*` de cada respuesta.
- [Precios](https://docs.x.com/x-api/getting-started/pricing): en la fecha indicada, crear una
  publicación costaba USD 0,015 y hacerlo con URL USD 0,200. Los precios pueden cambiar.
- [Reglas de automatización](https://help.x.com/en/rules-and-policies/x-automation): prohíben,
  entre otros comportamientos, spam, duplicados y automatizar la interfaz web.
- [Etiqueta de cuenta automatizada](https://help.x.com/es/using-x/automated-account-labels):
  Colmat debe evaluar si la naturaleza de su operación requiere identificar y vincular la cuenta
  automatizada.

La validación Python normaliza Unicode y aplica una estimación segura para texto latino y los
casos habituales, pero no es la implementación oficial completa de `twitter-text`: algunas
secuencias complejas de emoji y rutas de URL pueden contarse de más. El detector cubre esquemas,
`www`, dominios comunes e internacionales, pero X conserva la validación definitiva. Si el equipo
necesita coincidencia exacta antes del envío, debe integrar el paquete oficial `twitter-text` de
JavaScript y su suite de conformidad.

## Desarrollo

```bash
ruff format --check .
ruff check .
pytest --cov --cov-report=term-missing
```

La integración real nunca corre en CI. Haz la primera prueba con una cuenta controlada, un solo
snapshot aprobado y el límite diario en `1`.
