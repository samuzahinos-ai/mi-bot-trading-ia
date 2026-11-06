# Cómo conseguir que el motor funcione de verdad 24/7 (con el ordenador apagado)

## El problema

El motor corre en GitHub Actions, no en tu ordenador, así que apagarlo no le afecta. Pero el
calendario (`schedule`) de GitHub **no es fiable** en repositorios pequeños: GitHub omite o retrasa
muchas ejecuciones programadas. En este repositorio, en 11 horas solo se lanzaron 2 ciclos
programados en lugar de unos 130. Por eso el panel mostraba «Último ciclo hace 273 minutos».

## La solución (gratis, 10 minutos, una sola vez)

Un programador externo envía una señal a GitHub cada 5 minutos. El flujo ya está preparado para
recibirla (`repository_dispatch` con el tipo `tick`). El calendario de GitHub queda como respaldo.

### Paso 1: crear un token de GitHub limitado a este repositorio

1. Entra en GitHub → foto de perfil → **Settings** → **Developer settings** →
   **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
2. Nombre: `disparador-bot`. Caducidad: la más larga que permita (anótate la fecha para renovarlo).
3. **Repository access** → *Only select repositories* → elige `mi-bot-trading-ia`.
4. **Permissions** → **Repository permissions** → **Contents: Read and write**
   (GitHub lo exige para enviar la señal; no hace falta ningún otro permiso).
5. **Generate token** y copia el valor (empieza por `github_pat_`). Solo se muestra una vez.

### Paso 2: crear el programador gratuito en cron-job.org

1. Crea una cuenta gratuita en <https://cron-job.org>.
2. **Create cronjob** y rellena:
   - **URL:** `https://api.github.com/repos/samuzahinos-ai/mi-bot-trading-ia/dispatches`
   - **Execution schedule:** cada 5 minutos.
3. En **Advanced**:
   - **Request method:** `POST`
   - **Headers** (añade cuatro):

     | Nombre | Valor |
     |---|---|
     | `Accept` | `application/vnd.github+json` |
     | `Authorization` | `Bearer github_pat_...` (tu token) |
     | `X-GitHub-Api-Version` | `2022-11-28` |
     | `Content-Type` | `application/json` |

   - **Request body:** `{"event_type":"tick"}`
4. Guarda. Una respuesta correcta de GitHub es `204` (sin contenido); cron-job.org la marca como éxito.

### Paso 3: comprobar que funciona

- En GitHub → pestaña **Actions** deberían aparecer ejecuciones nuevas cada ~5 minutos con el
  evento `repository_dispatch`.
- En el panel, el aviso rojo desaparece y pasa a «Motor activo».

## Notas

- **Seguridad:** el token solo vive en cron-job.org; no lo escribas nunca en el repositorio. Permite
  escribir en este repositorio público, así que si sospechas que se ha filtrado, revócalo en GitHub.
- **Pausar nuevas entradas:** sigue haciéndose desde Actions → *Run workflow* → `allow_entries`.
  Las señales automáticas (`tick`) nunca cambian ese interruptor.
- **Huecos:** aunque falte alguna ejecución, el motor revisa todas las velas cerradas de 4 horas desde cada entrada, así que stops y salidas no se pierden.
- **Si prefieres no depender de GitHub:** el motor es un único `python worker.py`; puede ejecutarse
  en cualquier servidor pequeño (por ejemplo una máquina gratuita en la nube) con un cron cada 5 min.
