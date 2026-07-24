# Plantilla de instancia (T28c)

Una **instancia** = un agente desplegado: su identidad (SOUL, settings, skills,
moods) + sus datos (DB, media, credenciales). El **motor** (este repo) corre
cualquier instancia; hay un motor y N instancias.

Crear una instancia nueva:

```
python src/init_instance.py <carpeta-destino>
```

Eso copia: esta plantilla (settings neutro + SOUL neutra + .env.example) y los
defaults del motor (`prompts/` y `moods/`, que son deliberadamente genéricos).
Después: completá `.env`, editá `context/SOUL.md` y `config/settings.json`, y
arrancá con:

```
python -m botata --instance <carpeta-destino>
```

(o `BOTATA_INSTANCE=<carpeta>` en el entorno). Sin `--instance`, el bot usa la
raíz del repo como instancia (layout histórico, back-compat).

Regla: **canal nuevo de la misma comunidad = misma instancia; comunidad nueva =
instancia nueva** (con su propia DB — las memorias no se comparten entre
comunidades).
